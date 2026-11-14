"""Celý řetězec tlačítko (Pi) ↔ server ↔ GUI agent ↔ zařízení, ověření serveru tokenem, druhý pokus
o přepis na serveru, rychlé numpy cesty zpracování zvuku a nastavení mixéru WM8731."""

from __future__ import annotations

import array
import math
import random
import subprocess

import httpx
import pytest

from helpbutton.agents.mock import MockPc, MockPhone
from helpbutton.server.app import create_app
from helpbutton.server.config import Config
from raspberry_pi import audio_io
from raspberry_pi.client import RpiVoiceButtonClient
from raspberry_pi.config import auth_headers
from raspberry_pi.test_rpi_button import (
    SILENCE_FRAME,
    DummyTts,
    FakePlayer,
    ScriptedStt,
    _connect_mock_device,
    _fast_cfg,
    _scripted_client,
    _speech_frames,
)
from tests.test_agent import ScriptedVlm, act

TOKEN = "parovaci-token-pro-test"


def _smart(name: str, **args) -> dict:
    import json

    return act("tool", tool=name, tool_args_json=json.dumps(args, ensure_ascii=False))


async def test_e2e_button_drives_gui_agent_with_question_and_token(tmp_path):
    """Senior → tlačítko → server (z jiného počítače, s klíčem) → GUI agent se zeptá → Pi samo poslouchá
    odpověď → agent dokončí úkol na telefonu. Pi se nejdřív ujistí, že mluví s pravým serverem."""
    srv_cfg = Config(
        llm_provider="keywords",
        vlm_provider="none",
        data_dir=tmp_path,
        mdns=False,
        adb_watchdog=False,
        token=TOKEN,
    )
    app = create_app(srv_cfg)
    vlm = ScriptedVlm(
        lambda p: act("ask_user", text="Mám otevřít Fotky, nebo Obchod Play?"),
        lambda p: _smart("click_text", text="Zavřít"),
        lambda p: _smart("click_text", text="Fotky"),
        lambda p: act("done", text="Fotky jsou otevřené."),
    )
    orch = app.state.orch
    orch.vlm, orch._vlm_checked = vlm, True
    phone = MockPhone("Telefon babičky", delay=0)
    await _connect_mock_device(app, phone)
    await _connect_mock_device(app, MockPc("Počítač", delay=0))

    cfg = _fast_cfg(auto_listen_followups=True, token=TOKEN)
    stt = ScriptedStt(cfg, [("Něco mi tam svítí a nevím co s tím", "cs"), ("ano", "cs"), ("Fotky", "cs")])
    paths: list[str] = []

    async def log_request(request: httpx.Request) -> None:
        paths.append(request.url.path)

    transport = httpx.ASGITransport(app=app, client=("10.0.0.7", 50000))
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://192.168.1.10:8765",
        headers=auth_headers(TOKEN),
        event_hooks={"request": [log_request]},
    ) as http:
        client = RpiVoiceButtonClient(
            cfg,
            http_client=http,
            stt=stt,
            tts=DummyTts(cfg),
            player=FakePlayer(cfg),
            frame_source=lambda _s: _speech_frames(),
        )
        reply = await client.run_interaction()

    assert reply is not None and reply["screen"] == "hotovo", reply
    assert phone.screen == "photos"
    assert paths[0] == "/api/hello"
    assert paths.count("/api/utterance") == 3
    assert any("Fotky, nebo Obchod Play" in s for s in client.spoken_history)
    assert any("Fotky jsou otevřené" in s for s in client.spoken_history)
    assert "senior answered: Fotky" in vlm.prompts[1]


async def test_pi_never_talks_to_server_that_cannot_prove_token():
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/api/hello":
            return httpx.Response(200, json={"helpbutton": True, "auth": "hmac-sha256", "proof": "0" * 64})
        return httpx.Response(200, json={"say": "Podvržená odpověď", "ts": 1.0})

    cfg = _fast_cfg(token=TOKEN)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://192.168.1.66:8765")
    client, _player, _tts = _scripted_client(cfg, http_client=http, phrases=[("Zmizel mi WhatsApp", "cs")])
    reply = await client.run_interaction()
    await http.aclose()
    assert reply is None
    assert set(paths) == {"/api/hello"}
    assert any("odmítl" in s for s in client.spoken_history)


def _second_opinion_server(server_stt: bool, seen: list[tuple[str, dict, str]]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, dict(request.url.params), request.headers.get("content-type", "")))
        if request.url.path == "/api/state":
            return httpx.Response(
                200, json={"senior": "Paní Jano", "voice": {"stt": server_stt, "tts": False}}
            )
        if request.url.path == "/api/audio":
            return httpx.Response(
                200,
                json={
                    "say": "Otevírám Seznam.",
                    "screen": "hotovo",
                    "led": "green",
                    "tone": "double",
                    "expects_answer": False,
                    "ts": 5.0,
                },
            )
        return httpx.Response(200, json={"say": "", "ts": 1.0})

    return handler


async def test_server_tries_to_understand_when_pi_did_not():
    seen: list[tuple[str, dict, str]] = []
    cfg = _fast_cfg()
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(_second_opinion_server(True, seen)), base_url="http://192.168.1.10:8765"
    )
    client, _player, _tts = _scripted_client(cfg, http_client=http, phrases=[("", "cs")])
    await client.resolve_server()
    reply = await client.run_interaction()
    await http.aclose()
    assert reply is not None and reply["say"] == "Otevírám Seznam."
    path, params, ctype = next(r for r in seen if r[0] == "/api/audio")
    assert params == {"rate": "16000", "source": "button"} and ctype == "audio/wav"
    assert any("Otevírám Seznam" in s for s in client.spoken_history)


async def test_no_second_opinion_when_server_has_no_stt():
    seen: list[tuple[str, dict, str]] = []
    cfg = _fast_cfg()
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(_second_opinion_server(False, seen)),
        base_url="http://192.168.1.10:8765",
    )
    client, _player, _tts = _scripted_client(cfg, http_client=http, phrases=[("", "cs"), ("ne", "cs")])
    await client.resolve_server()
    await client.run_interaction()
    await http.aclose()
    assert any("nerozuměla" in s for s in client.spoken_history)
    assert not any(r[0] == "/api/audio" for r in seen)


@pytest.mark.skipif(audio_io._np is None, reason="numpy není nainstalované")
def test_numpy_fast_paths_match_pure_python(monkeypatch):
    rng = random.Random(3)
    cases = []
    for n in (0, 1, 31, 32, 480, 1441, 2880):
        for amp in (0, 60, 3000, 32767):
            cases.append(
                array.array("h", (rng.randint(-amp, amp) for _ in range(n))).tobytes() + b"\x01" * (n % 2)
            )
    mono = [int(8000 * math.sin(i / 7)) for i in range(2400)]
    dead = array.array("h")
    inverted = array.array("h")
    for v in mono:
        dead.extend((v, 0))
        inverted.extend((v, -v))
    cases += [dead.tobytes(), inverted.tobytes()]
    pop = array.array("h", [0] * 480)
    pop[100], pop[101] = 20000, -18000
    cases.append(pop.tobytes())

    calls = [
        (audio_io.pcm16_rms, ()),
        (audio_io.pcm16_zcr, ()),
        (audio_io.is_transient_pop_frame, ()),
        (audio_io.stereo_to_mono_pcm16, ()),
        (audio_io.downmix_to_mono_pcm16, (3,)),
        (audio_io.resample_pcm16_mono, (48000, 16000)),
        (audio_io.resample_pcm16_mono, (44100, 16000)),
        (audio_io.resample_pcm16_mono, (22050, 48000)),
    ]
    for pcm in cases:
        for fn, extra in calls:
            fast = fn(pcm, *extra)
            with monkeypatch.context() as m:
                m.setattr(audio_io, "_np", None)
                slow = fn(pcm, *extra)
            if isinstance(slow, float):
                assert fast == pytest.approx(slow, rel=1e-9, abs=1e-9), fn.__name__
            else:
                assert fast == slow, (fn.__name__, len(pcm), extra)


def _fake_linux(monkeypatch, run):
    monkeypatch.setattr(audio_io.platform, "system", lambda: "Linux")
    monkeypatch.setattr(audio_io.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(audio_io.subprocess, "run", run)
    monkeypatch.setattr(audio_io, "pipewire_running", lambda: False)


def test_wm8731_mixer_targets_exact_controls_and_falls_back(monkeypatch):
    calls: list[list[str]] = []

    def run(cmd, **_kw):
        args = list(cmd[4:])
        calls.append(args)
        if args[:2] == ["cset", "name=Mic Boost Volume"]:
            return subprocess.CompletedProcess(cmd, 1, "", "amixer: Cannot find the given element")
        if args == ["sset", "Master", "unmute"]:
            return subprocess.CompletedProcess(cmd, 1, "", "Invalid command!")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    _fake_linux(monkeypatch, run)
    assert audio_io.ensure_wm8731_mixer("audioinjectorpi", 100) is True
    assert ["cset", "name=Input Mux", "Mic"] in calls
    assert ["cset", "name=Mic Capture Switch", "on"] in calls
    assert ["sset", "Mic Boost", "100%"] in calls
    assert ["cset", "name=Playback Deemphasis Switch", "off"] in calls
    assert ["sset", "Master", "0dB"] in calls
    assert not any(c[:2] == ["sset", "Master"] and "100%" in c for c in calls)


def test_wm8731_mixer_stops_when_card_is_missing(monkeypatch):
    calls: list[list[str]] = []

    def run(cmd, **_kw):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 1, "", "amixer: Invalid card number.")

    _fake_linux(monkeypatch, run)
    assert audio_io.ensure_wm8731_mixer("audioinjectorpi", 100) is False
    assert len(calls) == 1


def test_lower_volume_is_set_in_percent(monkeypatch):
    calls: list[list[str]] = []

    def run(cmd, **_kw):
        calls.append(list(cmd[4:]))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    _fake_linux(monkeypatch, run)
    audio_io.set_output_volume("audioinjectorpi", 70)
    assert ["sset", "Master", "70%"] in calls


def test_heavily_clipped_mic_turns_mic_boost_off_once(monkeypatch):
    reduced: list[str] = []
    monkeypatch.setattr(
        audio_io, "reduce_mic_gain", lambda card="audioinjectorpi": reduced.append(card) or True
    )
    monkeypatch.setattr(audio_io, "_mic_gain_reduced", False)
    clipped = array.array(
        "h",
        (max(-32768, min(32767, int(60000 * math.sin(2 * math.pi * 210 * i / 16000)))) for i in range(480)),
    ).tobytes()
    frames = [SILENCE_FRAME] * 5 + [clipped] * 12 + [SILENCE_FRAME] * 15
    cfg = _fast_cfg()
    for _ in range(2):
        monkeypatch.setattr(audio_io, "_last_clip_warning", 0.0)
        audio_io.process_vad_stream(iter(frames), cfg)
    assert reduced == ["audioinjectorpi"]


async def test_cli_token_flag_and_401_rejected_state_reporting(tmp_path, caplog):
    """Chybějící token hlásí jasně požadavek na token (místo 'zatím neodpovídá') a s tokenem projde."""
    import logging

    srv_cfg = Config(
        llm_provider="keywords",
        vlm_provider="none",
        data_dir=tmp_path,
        mdns=False,
        adb_watchdog=False,
        token=TOKEN,
    )
    app = create_app(srv_cfg)
    transport = httpx.ASGITransport(app=app, client=("10.0.0.7", 50000))

    cfg_no_tok = _fast_cfg(server_url="http://192.168.1.30:8765", token="")
    async with httpx.AsyncClient(transport=transport, base_url="http://192.168.1.30:8765") as http_no:
        c_no = RpiVoiceButtonClient(
            cfg_no_tok, http_client=http_no, tts=DummyTts(cfg_no_tok), player=FakePlayer(cfg_no_tok)
        )
        with caplog.at_level(logging.WARNING, logger="helpbutton.rpi.client"):
            await c_no.resolve_server()
        assert c_no._last_error_kind == "rejected"
        assert c_no._server_ok is False
        assert any("token" in rec.message.lower() for rec in caplog.records)
        assert not any("zatím neodpovídá" in rec.message for rec in caplog.records)

    cfg_tok = _fast_cfg(server_url="http://192.168.1.30:8765", token=TOKEN)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://192.168.1.30:8765", headers=auth_headers(TOKEN)
    ) as http_ok:
        c_ok = RpiVoiceButtonClient(
            cfg_tok, http_client=http_ok, tts=DummyTts(cfg_tok), player=FakePlayer(cfg_tok)
        )
        await c_ok.resolve_server()
        assert c_ok._server_ok is True
        assert c_ok._last_error_kind == ""
