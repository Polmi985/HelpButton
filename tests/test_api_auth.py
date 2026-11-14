"""API serveru z jiných počítačů jen s klíčem (odvozeným z párovacího tokenu).

Dřív: kdokoli ve stejné Wi-Fi si přečetl záznam pro rodinu a poslal HelpButton „ano“."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from helpbutton.common.protocol import api_key
from helpbutton.server.app import create_app
from helpbutton.server.config import Config

TOKEN = "tajne-heslo"


def _app(tmp_path, token: str = TOKEN, client: tuple[str, int] = ("192.168.1.20", 50000)) -> TestClient:
    cfg = Config(
        llm_provider="keywords",
        vlm_provider="none",
        data_dir=tmp_path,
        mdns=False,
        adb_watchdog=False,
        token=token,
    )
    return TestClient(create_app(cfg), client=client)


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_other_computer_needs_key(tmp_path):
    with _app(tmp_path) as c:
        r = c.get("/api/state")
        assert r.status_code == 401 and "klíč" in r.json()["error"]
        assert c.post("/api/utterance", json={"text": "ano"}).status_code == 401
        assert c.post("/api/press").status_code == 401
        assert c.get("/api/tts", params={"text": "x"}).status_code == 401
        assert c.get("/api/state", headers=_bearer(api_key(TOKEN))).status_code == 200
        assert c.get("/api/state", headers=_bearer(TOKEN)).status_code == 401


def test_pages_redirect_to_login_and_static_stays_open(tmp_path):
    with _app(tmp_path) as c:
        r = c.get("/button", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login?next=/button"
        assert c.get("/login").status_code == 200
        assert c.get("/static/helpbutton.css").status_code == 200


def test_browser_login_sets_cookie(tmp_path):
    with _app(tmp_path) as c:
        assert c.post("/api/login", json={"key": "spatne"}).status_code == 401
        r = c.post("/api/login", json={"key": api_key(TOKEN)})
        assert r.status_code == 200
        cookie = r.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie and TOKEN not in cookie
        assert c.get("/api/state").status_code == 200
        assert c.get("/button").status_code == 200


def test_live_websocket_needs_key(tmp_path):
    with _app(tmp_path) as c:
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/ws/ui") as ws:
                ws.receive_text()
        with c.websocket_connect("/ws/ui", headers=_bearer(api_key(TOKEN))) as ws:
            assert json.loads(ws.receive_text())["type"] == "snapshot"


def test_wrong_keys_lock_out_the_address(tmp_path):
    with _app(tmp_path) as c:
        for _ in range(10):
            assert c.get("/api/state", headers=_bearer("x" * 64)).status_code == 401
        assert c.get("/api/state", headers=_bearer(api_key(TOKEN))).status_code == 429
        assert c.post("/api/login", json={"key": api_key(TOKEN)}).status_code == 429


def test_page_visits_without_key_do_not_lock_out(tmp_path):
    with _app(tmp_path) as c:
        for _ in range(20):
            c.get("/button", follow_redirects=False)
        assert c.get("/api/state", headers=_bearer(api_key(TOKEN))).status_code == 200


def test_same_computer_and_no_token_stay_open(tmp_path):
    with _app(tmp_path, client=("127.0.0.1", 50000)) as c:
        assert c.get("/api/state").status_code == 200
        with c.websocket_connect("/ws/ui") as ws:
            assert json.loads(ws.receive_text())["type"] == "snapshot"
    with _app(tmp_path, token="") as c:
        assert c.get("/api/state").status_code == 200


def test_cli_and_rpi_send_same_key():
    from helpbutton.cli import auth_headers as cli_headers
    from raspberry_pi.config import auth_headers as rpi_headers

    assert cli_headers(TOKEN) == rpi_headers(TOKEN) == _bearer(api_key(TOKEN))
    assert cli_headers("") == rpi_headers("") == {}
