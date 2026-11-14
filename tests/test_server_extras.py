"""Důkaz identity serveru pro tlačítko, limity požadavků a rychlé čištění zvuku před Whisperem."""

from __future__ import annotations

import io
import math
import wave

import pytest
from fastapi.testclient import TestClient

from helpbutton.common.protocol import api_key, auth_proof
from helpbutton.server import voice as V
from helpbutton.server.app import create_app
from helpbutton.server.config import Config
from helpbutton.server.orchestrator import Orchestrator
from helpbutton.server.voice import Voice

TOKEN = "tajne-heslo"
OTHER_PC = ("192.168.1.20", 50000)


def _client(tmp_path, token: str = TOKEN, client: tuple[str, int] = OTHER_PC) -> TestClient:
    cfg = Config(
        llm_provider="keywords",
        vlm_provider="none",
        data_dir=tmp_path,
        mdns=False,
        adb_watchdog=False,
        token=token,
        whisper_warmup=False,
    )
    return TestClient(create_app(cfg), client=client)


def _key() -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key(TOKEN)}"}


def test_hello_proves_token_without_revealing_it(tmp_path):
    nonce = "ab" * 16
    with _client(tmp_path) as c:
        r = c.get("/api/hello", params={"nonce": nonce})
        assert r.status_code == 200
        body = r.json()
        assert body["proof"] == auth_proof(TOKEN, "server-api", nonce, "")
        assert TOKEN not in r.text and api_key(TOKEN) not in r.text
        for bad in ("", "xyz", "ab" * 100):
            assert c.get("/api/hello", params={"nonce": bad}).status_code == 400


def test_pi_and_server_compute_the_same_proof():
    from raspberry_pi.config import server_proof

    nonce = "0123456789abcdef" * 2
    assert server_proof(TOKEN, nonce) == auth_proof(TOKEN, "server-api", nonce, "")


def test_hello_without_token(tmp_path):
    with _client(tmp_path, token="", client=("127.0.0.1", 50000)) as c:
        assert c.get("/api/hello", params={"nonce": "ab" * 16}).json() == {"helpbutton": True, "auth": "none"}


def test_too_long_sentence_is_rejected(tmp_path):
    with _client(tmp_path) as c:
        assert c.post("/api/utterance", json={"text": "a" * 2001}, headers=_key()).status_code == 422
        assert c.post("/api/utterance", json={"text": "ano"}, headers=_key()).status_code == 200


def test_audio_size_and_rate_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(Voice, "stt_available", property(lambda self: True))

    async def no_stt(self, data, content_type="audio/wav", rate=16000):  # pragma: no cover
        raise AssertionError("přepis se u odmítnutého požadavku spouštět nemá")

    monkeypatch.setattr(Voice, "transcribe", no_stt)
    with _client(tmp_path) as c:
        big = b"\0" * 8_000_001
        assert c.post("/api/audio", content=big, headers=_key()).status_code == 413
        assert c.post("/api/button/audio", content=big, headers=_key()).status_code == 413
        assert (
            c.post("/api/audio", params={"rate": 1000}, content=b"\0" * 4000, headers=_key()).status_code
            == 400
        )


def test_button_second_opinion_is_handled_as_button(tmp_path, monkeypatch):
    monkeypatch.setattr(Voice, "stt_available", property(lambda self: True))

    async def fake_stt(self, data, content_type="audio/wav", rate=16000):
        return ""

    sources: list[str] = []
    orig = Orchestrator.handle

    async def spy(self, text, source="button"):
        sources.append(source)
        return await orig(self, text, source)

    monkeypatch.setattr(Voice, "transcribe", fake_stt)
    monkeypatch.setattr(Orchestrator, "handle", spy)
    with _client(tmp_path) as c:
        r = c.post(
            "/api/audio", params={"rate": 16000, "source": "button"}, content=b"\0" * 4000, headers=_key()
        )
        assert r.status_code == 200 and r.json()["screen"] == "nerozumi"
        c.post("/api/audio", params={"source": "cokoli"}, content=b"\0" * 4000, headers=_key())
    assert sources == ["button", "web-audio"]


def _reference_highpass(x, alpha: float, px: float):
    import numpy as np

    out, py = np.empty_like(x), 0.0
    for i in range(x.size):
        v = float(x[i])
        py = alpha * (py + v - px)
        px = v
        out[i] = py
    return out


@pytest.mark.parametrize("use_scipy", [True, False])
def test_vectorized_highpass_equals_sample_loop(use_scipy, monkeypatch):
    np = pytest.importorskip("numpy")
    if use_scipy:
        pytest.importorskip("scipy")
    else:
        import builtins

        real_import = builtins.__import__

        def no_scipy(name, *a, **k):
            if name.startswith("scipy"):
                raise ImportError("scipy vypnuté pro test")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", no_scipy)
    x = np.random.default_rng(1).standard_normal(5000) * 3000 + 400
    alpha = (1 / (2 * math.pi * 75)) / (1 / (2 * math.pi * 75) + 1 / 16000)
    for px in (float(x[0]), 0.0):
        assert np.max(np.abs(V._one_pole_highpass(x, alpha, px) - _reference_highpass(x, alpha, px))) < 1e-6


def _wav(samples, rate: int) -> bytes:
    import numpy as np

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(np.clip(samples, -32767, 32767).astype("<i2").tobytes())
    return buf.getvalue()


def test_48k_recording_has_no_aliasing_in_speech_band():
    np = pytest.importorskip("numpy")
    t = np.arange(48000 * 2) / 48000
    sig = np.sin(2 * math.pi * 500 * t) * 8000 + np.sin(2 * math.pi * 12000 * t) * 8000
    audio = V.prepare_mic_float32(_wav(sig, 48000))
    spec = np.abs(np.fft.rfft(audio * np.hanning(audio.size)))
    f = np.fft.rfftfreq(audio.size, 1 / 16000)
    alias = spec[(f > 3800) & (f < 4200)].max()
    speech = spec[(f > 480) & (f < 520)].max()
    assert 20 * math.log10(alias / speech) < -60


async def test_server_voice_skips_silence_blowup_uses_local_cache_and_fast_decode(tmp_path, monkeypatch):
    import importlib.machinery
    import sys
    import types

    np = pytest.importorskip("numpy")

    rng = np.random.default_rng(7)
    flat_noise = rng.standard_normal(16000) * 15.0
    out_noise = V.prepare_mic_float32(_wav(flat_noise, 16000))
    assert out_noise is not None and float(np.max(np.abs(out_noise))) < 0.01

    t = np.arange(16000) / 16000.0
    quiet_speech = np.sin(2.0 * math.pi * 450.0 * t) * 400.0
    out_speech = V.prepare_mic_float32(_wav(quiet_speech, 16000))
    assert out_speech is not None and float(np.max(np.abs(out_speech))) > 0.05

    init_calls: list[dict] = []
    transcribe_calls: list[dict] = []

    class FakeServerWhisper:
        def __init__(self, model_name, **kw):
            init_calls.append({"model": model_name, **kw})

        def transcribe(self, audio, **kw):
            transcribe_calls.append(dict(kw))
            txt = "" if len(transcribe_calls) == 1 else "jůtub a fejsbuk"
            info = types.SimpleNamespace(language="cs", language_probability=1.0)
            return ([types.SimpleNamespace(text=txt)] if txt else []), info

    fake_model_holder: list[FakeServerWhisper] = []

    def _ctor(model_name, **kw):
        m = FakeServerWhisper(model_name, **kw)
        fake_model_holder.append(m)
        return m

    fake_mod = types.ModuleType("faster_whisper")
    fake_mod.__spec__ = importlib.machinery.ModuleSpec("faster_whisper", loader=None)
    fake_mod.WhisperModel = _ctor
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_mod)

    cfg = Config(
        llm_provider="keywords",
        vlm_provider="none",
        data_dir=tmp_path,
        mdns=False,
        adb_watchdog=False,
        whisper_device="cpu",
        whisper_warmup=False,
    )
    voice = V.Voice(cfg)
    res_silence = await voice.transcribe(_wav(flat_noise, 16000))
    assert res_silence == ""
    assert len(init_calls) == 1
    assert init_calls[0]["local_files_only"] is True
    assert init_calls[0]["compute_type"] == "int8"
    assert init_calls[0]["num_workers"] == 1
    assert len(transcribe_calls) == 1
    assert transcribe_calls[0]["max_new_tokens"] == 128
    assert transcribe_calls[0]["language_detection_segments"] == 1

    res_speech = await voice.transcribe(_wav(quiet_speech, 16000))
    assert "YouTube" in res_speech and "Facebook" in res_speech


def test_atom_echo_firmware_stt_latency_optimizations():
    from pathlib import Path

    ino = (Path(__file__).resolve().parents[1] / "firmware" / "atom_echo" / "atom_echo.ino").read_text(
        encoding="utf-8"
    )
    assert "WiFi.setSleep(false)" in ino
    assert "c.setNoDelay(true)" in ino
    assert "PCM_CHUNK_SAMPLES" in ino
    assert "RELEASE_DEBOUNCE_MS" in ino
    assert "setConnectTimeout" in ino
    assert "txFrame" in ino
