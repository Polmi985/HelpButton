"""Jeden server pro tlačítko HelpButton i chat Jiřík: cesty, přístup z jiných počítačů, „Tento počítač“."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from helpbutton.common.protocol import api_key
from helpbutton.server.app import create_app
from helpbutton.server.config import Config, chat_config, load_config

TOKEN = "tajny-token-pro-testy-123"
OTHER_PC = ("192.168.1.50", 50000)
LOCAL = ("127.0.0.1", 50000)


def _cfg(tmp_path, token: str = "") -> Config:
    return Config(
        llm_provider="keywords",
        vlm_provider="none",
        data_dir=tmp_path,
        mdns=False,
        adb_watchdog=False,
        whisper_warmup=False,
        token=token,
        ollama_url="http://127.0.0.1:1",
        link_port=0,
        pair_port=0,
    )


def _client(tmp_path, token: str = "", client: tuple[str, int] = LOCAL) -> TestClient:
    return TestClient(create_app(_cfg(tmp_path, token)), client=client)


def test_local_pages_and_both_states(tmp_path):
    with _client(tmp_path) as c:
        assert c.get("/api/ping").json() == {"ok": True, "helpbutton": True, "jirik": True}
        assert c.get("/").status_code == 200
        assert c.get("/button").status_code == 200
        r = c.get("/chat")
        assert r.status_code == 200 and "/ws/chat" in r.text
        hb = c.get("/api/state").json()
        assert "state" in hb and "devices" in hb
        jr = c.get("/api/chat/state").json()
        assert jr["type"] == "snapshot" and "chat" in jr["spaces"]
        assert c.post("/api/chat/stop", json={}).json() == {"ok": True}


def test_chat_is_closed_to_other_computers_without_token(tmp_path):
    with _client(tmp_path, client=OTHER_PC) as c:
        assert c.get("/api/state").status_code == 200  # tlačítko bez tokenu funguje jako dřív
        assert c.get("/chat", follow_redirects=False).status_code == 403
        assert c.post("/api/chat", json={"text": "ahoj"}).status_code == 401
        assert c.get("/api/chat/state").status_code == 401
        assert c.post("/api/settings", json={"voice": False}).status_code == 401
        assert c.post("/api/link/pair/open").status_code == 401


def test_chat_with_token_needs_key_from_other_computers(tmp_path):
    with _client(tmp_path, token=TOKEN, client=OTHER_PC) as c:
        r = c.get("/chat", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login?next=/chat"
        auth = {"Authorization": f"Bearer {api_key(TOKEN)}"}
        assert c.get("/chat", headers=auth).status_code == 200
        assert c.get("/api/chat/state", headers=auth).json()["type"] == "snapshot"


def test_local_daemon_is_this_computer_for_jirik(tmp_path):
    app = create_app(_cfg(tmp_path))
    hello = {"type": "hello", "device_id": "pc-test", "name": "Tento počítač", "kind": "pc", "platform": "Windows",
             "tools": [{"name": "desktop_view"}], "local": False}
    with TestClient(app, client=LOCAL) as c:
        with c.websocket_connect("/ws/device") as ws:
            ws.send_text(json.dumps(hello))
            ws.send_text(json.dumps({"type": "status", "status": {}}))
            st = c.get("/api/chat/state").json()
            assert [d["id"] for d in st["devices"]] == ["pc-test"]
            assert app.state.chat._local().device_id == "pc-test"
        with c.websocket_connect("/ws/device") as ws:  # jen Jiřík Link smí připojit „link-…“
            ws.send_text(json.dumps({**hello, "device_id": "link-abc"}))
            assert "link-abc" not in app.state.registry.devices


def test_remote_lan_daemon_is_not_this_computer(tmp_path):
    app = create_app(_cfg(tmp_path))
    hello = {"type": "hello", "device_id": "pc-babicka", "name": "Počítač babičky", "kind": "pc",
             "platform": "Windows", "tools": [], "local": True}
    with TestClient(app, client=OTHER_PC) as c:
        with c.websocket_connect("/ws/device") as ws:
            ws.send_text(json.dumps(hello))
            ws.send_text(json.dumps({"type": "status", "status": {}}))
            dev = app.state.registry.devices["pc-babicka"]
            assert dev.local is False  # zařízení si „místnost“ samo neurčí
            assert app.state.chat._local() is None


def test_chat_config_is_a_separate_copy(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.ollama_vlm_model = "qwen3-vl:2b-instruct"
    c = chat_config(cfg)
    c.ollama_vlm_model = "qwen3.5:9b"
    c.num_ctx = 16384
    assert cfg.ollama_vlm_model == "qwen3-vl:2b-instruct" and cfg.num_ctx == 8192
    assert chat_config(cfg).ollama_vlm_model == cfg.model


def test_jirik_sections_in_config(tmp_path):
    toml = tmp_path / "helpbutton.toml"
    toml.write_text(
        '[helpbutton]\nsenior_name = "Paní Věro"\n[jirik]\nmodel = "qwen3.5:9b"\nnum_ctx = 16384\n'
        '[link]\nport = 9771\n',
        encoding="utf-8",
    )
    (tmp_path / "helpbutton.user.toml").write_text('[jirik]\nuser_name = "Adam"\ngreeting = "Adame"\n',
                                                     encoding="utf-8")
    cfg = load_config(toml)
    assert cfg.senior_name == "Paní Věro"
    assert (cfg.model, cfg.num_ctx, cfg.link_port) == ("qwen3.5:9b", 16384, 9771)
    assert (cfg.user_name, cfg.greeting) == ("Adam", "Adame")
