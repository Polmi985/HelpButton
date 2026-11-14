"""Převod textu na řeč (TTS) pro Raspberry Pi 5 s větným streamováním, diskovou cache a čtyřúrovňovou zálohou.

Pořadí priorit v režimu `auto`:
  1. `piper`  – 100% offline lokální Piper TTS (`cs_CZ-jirka-medium.onnx`, odezva < 150 ms na Pi 5),
  2. `edge`   – přirozený český ženský hlas `cs-CZ-VlastaNeural` (přes `edge-tts` + `ffmpeg`),
  3. `server` – stažení WAV ze serveru HelpButton (`GET /api/tts`),
  4. `espeak` – nouzová offline záloha (`espeak-ng -v cs` na Linuxu / SAPI na Windows).

Optimalizace rychlosti a spolehlivosti:
  * Dělí delší odpovědi na jednotlivé věty (`split_into_clauses`), takže první věta hraje ihned,
    zatímco druhá věta se syntetizuje na pozadí.
  * Piper model se načte jednou a drží v RAM (podporuje API piper-tts 1.2 i 1.3+); CLI je jen záloha.
  * Engine, který právě selhal (výpadek internetu pro Edge, nedostupný server), se 60 s přeskakuje,
    aby každá věta nečekala na timeout.
  * Na Linuxu / Raspberry Pi 5 využívá `/dev/shm` (RAM disk) místo zápisu dočasných souborů na SD kartu.
  * Podporuje `rate_override` pro zpomalené zopakování (0.68x), když senior řekne "Cože? / Zopakuj to".
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.util
import io
import logging
import os
import platform
import re
import shutil
import tempfile
import time
import wave
from pathlib import Path

import httpx

from .config import RpiConfig

log = logging.getLogger("helpbutton.rpi.tts")

PREWARM_PHRASES_CS = (
    "Poslouchám.",
    "Zastavila jsem. Nic dalšího nedělám.",
    "Dobře, nic neměním.",
    "Rozuměla jsem. Hned se podívám.",
    "Neslyšela jsem nic. Zkuste prosím zmáčknout tlačítko a promluvit.",
    "Se serverem HelpButton se teď nemohu spojit.",
)

RX_SENTENCE_SPLIT = re.compile(r"(?<=[\.\!\?])\s+")
ENGINE_COOLDOWN_S = 60.0


def split_into_clauses(text: str, min_clause_chars: int = 22) -> list[str]:
    """Rozdělí delší odstavec na větné úseky pro okamžitý nástup přehrávání první věty."""
    cleaned = (text or "").strip()
    if not cleaned:
        return []
    parts = [p.strip() for p in RX_SENTENCE_SPLIT.split(cleaned) if p.strip()]
    if len(parts) <= 1:
        return [cleaned]
    merged: list[str] = []
    buf = ""
    for p in parts:
        if not buf:
            buf = p
        elif len(buf) < min_clause_chars:
            buf = f"{buf} {p}"
        else:
            merged.append(buf)
            buf = p
    if buf:
        if merged and len(buf) < 14:
            merged[-1] = f"{merged[-1]} {buf}"
        else:
            merged.append(buf)
    return merged


def _temp_dir() -> str | None:
    return "/dev/shm" if Path("/dev/shm").is_dir() and os.access("/dev/shm", os.W_OK) else None


def _valid_wav(data: bytes | None) -> bool:
    return bool(data) and len(data) > 100 and data[:4] == b"RIFF"


class TextToSpeech:
    """Syntéza české (a volitelně anglické) řeči do WAV bajtů s trvalou diskovou cache."""

    def __init__(self, cfg: RpiConfig, server_url_getter=None):
        self.cfg = cfg
        self._server_url_getter = server_url_getter
        self._piper_voice = None
        self._piper_py_failed = False
        self._piper_lock = asyncio.Lock()
        self._engine_down_until: dict[str, float] = {}
        try:
            self.cfg.cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.warning("TTS cache %s nelze vytvořit (%s) – cache vypnuta.", self.cfg.cache_dir, e)

    @property
    def piper_available(self) -> bool:
        model_ok = bool(self.cfg.piper_model) and Path(self.cfg.piper_model).exists()
        if not model_ok:
            return False
        bin_ok = Path(self.cfg.piper_bin).exists() or shutil.which(self.cfg.piper_bin) is not None
        py_ok = importlib.util.find_spec("piper") is not None and not self._piper_py_failed
        return bin_ok or py_ok

    @property
    def edge_available(self) -> bool:
        return importlib.util.find_spec("edge_tts") is not None and shutil.which("ffmpeg") is not None

    @property
    def espeak_available(self) -> bool:
        return shutil.which("espeak-ng") is not None or shutil.which("espeak") is not None

    def active_engine_name(self) -> str:
        eng = self.cfg.tts_engine.lower()
        if eng != "auto":
            return eng
        if self.piper_available:
            return f"piper ({Path(self.cfg.piper_model).stem})"
        if self.edge_available:
            return f"edge-tts ({self.cfg.edge_voice})"
        if self.espeak_available:
            return "espeak-ng (cs)"
        if platform.system().lower() == "windows":
            return "windows-sapi"
        return "server-tts"

    async def warmup(self, senior_name: str = "Paní Jano") -> None:
        """Předgeneruje nejčastější věty do diskové cache, aby tlačítko mluvilo okamžitě."""
        if not self.cfg.prewarm_phrases:
            return
        phrases = list(PREWARM_PHRASES_CS)
        if getattr(self.cfg, "speak_listening_greeting", False):
            phrases.append(f"Poslouchám, {senior_name}.")
        for phrase in phrases:
            try:
                if not await self.synthesize(phrase):
                    log.warning(
                        "Předgenerování řeči se nepodařilo (%r) – žádný TTS engine nefunguje.", phrase
                    )
                    break
            except Exception as e:
                log.debug("Prewarm selhal pro %r: %s", phrase, e)
                break

    def _engine_tag(self, eng: str, lang: str) -> str:
        """Do klíče cache patří i konkrétní hlas – po změně modelu/hlasu se nepřehrává starý hlas."""
        if eng == "piper":
            return f"piper:{Path(self.cfg.piper_model).name}"
        if eng == "edge":
            return f"edge:{self.cfg.edge_en_voice if lang == 'en' else self.cfg.edge_voice}"
        return eng

    def _cache_path(self, text: str, lang: str, engine_tag: str, rate: float) -> Path:
        key = f"{engine_tag}|{lang}|{rate:.2f}|{text.strip()}"
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
        return self.cfg.cache_dir / f"{digest}.wav"

    def _engine_down(self, eng: str) -> bool:
        return time.monotonic() < self._engine_down_until.get(eng, 0.0)

    def _mark_down(self, eng: str) -> None:
        if not self._engine_down(eng):
            log.info("TTS engine %s teď nefunguje – %d s ho přeskakuji.", eng, int(ENGINE_COOLDOWN_S))
        self._engine_down_until[eng] = time.monotonic() + ENGINE_COOLDOWN_S

    async def synthesize(
        self, text: str, lang: str = "cs", *, rate_override: float | None = None
    ) -> bytes | None:
        """Převede text na WAV (16-bit PCM). Vrací bajty WAV souboru nebo None. Nikdy nevyhodí výjimku."""
        cleaned = (text or "").strip()
        if not cleaned:
            return None

        rate = rate_override if rate_override is not None else self.cfg.speech_rate
        pref = self.cfg.tts_engine.lower()
        engines = [pref] if pref != "auto" else ["piper", "edge", "server", "espeak"]

        for eng in engines:
            cache_file = self._cache_path(cleaned, lang, self._engine_tag(eng, lang), rate)
            try:
                if cache_file.exists() and cache_file.stat().st_size > 100:
                    data = cache_file.read_bytes()
                    if _valid_wav(data):
                        return data
            except OSError:
                pass
            if self._engine_down(eng):
                continue

            wav: bytes | None = None
            try:
                if eng == "piper" and lang == "cs" and self.piper_available:
                    wav = await self._synth_piper(cleaned, rate)
                elif eng == "edge" and self.edge_available:
                    wav = await self._synth_edge(cleaned, lang, rate)
                    if not wav:
                        self._mark_down("edge")
                elif eng == "server" and lang == "cs" and rate_override is None:
                    wav = await self._synth_server(cleaned)
                elif eng == "espeak":
                    wav = await self._synth_fallback_os(cleaned, lang, rate)
            except Exception as e:
                log.warning("TTS %s selhal: %s", eng, e)
                wav = None

            if _valid_wav(wav):
                try:
                    cache_file.write_bytes(wav)  # type: ignore[arg-type]
                except OSError:
                    pass
                return wav

        try:
            return await self._synth_fallback_os(cleaned, lang, rate)
        except Exception as e:
            log.error("Žádný TTS engine nefunguje (%s).", e)
            return None

    def _piper_render(self, text: str, length_scale: float) -> bytes:
        """Syntéza načteným modelem Piper – API piper-tts 1.3+ (`synthesize_wav`) i 1.2 (`synthesize`)."""
        voice = self._piper_voice
        buf = io.BytesIO()
        if hasattr(voice, "synthesize_wav"):
            syn_cfg = None
            for mod in ("piper", "piper.config"):
                try:
                    syn_cls = getattr(importlib.import_module(mod), "SynthesisConfig")
                    syn_cfg = syn_cls(length_scale=length_scale)
                    break
                except (ImportError, AttributeError, TypeError):
                    continue
            with wave.open(buf, "wb") as w:
                if syn_cfg is not None:
                    voice.synthesize_wav(text, w, syn_config=syn_cfg)
                else:
                    voice.synthesize_wav(text, w)
        else:
            with wave.open(buf, "wb") as w:
                voice.synthesize(text, w, length_scale=length_scale, sentence_silence=0.35)
        data = buf.getvalue()
        if not _valid_wav(data):
            raise RuntimeError("Piper nevrátil žádný zvuk")
        return data

    async def _synth_piper(self, text: str, rate: float) -> bytes | None:
        """Syntéza přes lokální Piper (Python knihovna `piper-tts` nebo binárka `piper`)."""
        length_scale = 1.0 / max(rate, 0.45)

        if importlib.util.find_spec("piper") is not None and not self._piper_py_failed:
            try:
                async with self._piper_lock:
                    if self._piper_voice is None:
                        from piper import PiperVoice  # type: ignore[import-untyped]

                        self._piper_voice = await asyncio.to_thread(PiperVoice.load, self.cfg.piper_model)
                    return await asyncio.to_thread(self._piper_render, text, length_scale)
            except Exception as e:
                self._piper_py_failed = True
                log.warning("Python modul piper selhal (%s) – dál používám binárku piper.", e)

        return await self._synth_piper_cli(text, length_scale)

    async def _synth_piper_cli(self, text: str, length_scale: float) -> bytes | None:
        piper_exe = (
            self.cfg.piper_bin if Path(self.cfg.piper_bin).exists() else shutil.which(self.cfg.piper_bin)
        )
        if not piper_exe:
            return None
        variants = (
            ["--length_scale", f"{length_scale:.2f}", "--sentence_silence", "0.35"],
            ["--length-scale", f"{length_scale:.2f}"],
            [],
        )
        for extra in variants:
            fd, out_path = tempfile.mkstemp(suffix=".wav", dir=_temp_dir())
            os.close(fd)
            try:
                proc = await asyncio.create_subprocess_exec(
                    piper_exe,
                    "-m",
                    self.cfg.piper_model,
                    "-f",
                    out_path,
                    *extra,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    _, err = await asyncio.wait_for(proc.communicate(text.encode("utf-8")), timeout=20.0)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                    log.warning("Piper CLI nestihl syntézu do 20 s.")
                    return None
                data = Path(out_path).read_bytes() if Path(out_path).exists() else b""
                if proc.returncode == 0 and _valid_wav(data):
                    return data
                if proc.returncode != 2:
                    log.warning("Piper CLI selhal: %s", err.decode(errors="ignore")[-200:])
                    return None
            except Exception as e:
                log.warning("Piper CLI chyba: %s", e)
                return None
            finally:
                Path(out_path).unlink(missing_ok=True)
        return None

    async def _synth_edge(self, text: str, lang: str, rate: float) -> bytes | None:
        """Syntéza přes Microsoft Edge Neural TTS (`cs-CZ-VlastaNeural`) převedená do WAV."""
        try:
            import edge_tts  # type: ignore[import-untyped]
        except ImportError:
            return None

        voice = self.cfg.edge_en_voice if lang == "en" else self.cfg.edge_voice
        pct = int(round((rate - 1.0) * 100))
        rate_str = f"{pct:+d}%"

        tdir = _temp_dir()
        fd_mp3, mp3_path = tempfile.mkstemp(suffix=".mp3", dir=tdir)
        fd_wav, wav_path = tempfile.mkstemp(suffix=".wav", dir=tdir)
        os.close(fd_mp3)
        os.close(fd_wav)
        proc = None
        try:
            comm = edge_tts.Communicate(text, voice, rate=rate_str)
            await asyncio.wait_for(comm.save(mp3_path), timeout=8.0)
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-i",
                mp3_path,
                "-ar",
                "22050",
                "-ac",
                "1",
                "-f",
                "wav",
                wav_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=10.0)
            if proc.returncode == 0 and Path(wav_path).exists():
                return Path(wav_path).read_bytes()
            return None
        except Exception as e:
            log.debug("Edge-TTS selhalo: %s", e)
            if proc is not None and proc.returncode is None:
                proc.kill()
            return None
        finally:
            Path(mp3_path).unlink(missing_ok=True)
            Path(wav_path).unlink(missing_ok=True)

    async def _synth_server(self, text: str) -> bytes | None:
        """Stáhne WAV ze serveru HelpButton (`GET /api/tts`), pokud má server nakonfigurovaný Piper."""
        base_url = self._server_url_getter() if self._server_url_getter else None
        if not base_url or base_url == "auto":
            return None
        try:
            from .config import auth_headers

            async with httpx.AsyncClient(
                base_url=base_url,
                timeout=httpx.Timeout(10.0, connect=2.5),
                trust_env=False,
                headers=auth_headers(self.cfg.token),
            ) as client:
                r = await client.get("/api/tts", params={"text": text[:500]})
                if r.status_code == 200 and r.headers.get("content-type", "").startswith("audio/"):
                    return r.content
                if r.status_code in (404, 501):
                    self._mark_down("server")
        except (httpx.ConnectError, httpx.ConnectTimeout):
            self._mark_down("server")
        except Exception as e:
            log.debug("Server TTS selhalo: %s", e)
        return None

    async def _synth_fallback_os(self, text: str, lang: str, rate: float) -> bytes | None:
        """Nouzová offline syntéza přes `espeak-ng` (Raspberry Pi / Linux) nebo SAPI (Windows)."""
        espeak_bin = shutil.which("espeak-ng") or shutil.which("espeak")
        if espeak_bin:
            wpm = max(85, int(155 * rate))
            voice = "en" if lang == "en" else "cs"
            try:
                proc = await asyncio.create_subprocess_exec(
                    espeak_bin,
                    "-v",
                    voice,
                    "-s",
                    str(wpm),
                    "--stdout",
                    text,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                try:
                    out, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                    out = b""
                if proc.returncode == 0 and out.startswith(b"RIFF"):
                    return _fix_streamed_wav(out)
            except Exception as e:
                log.debug("espeak-ng selhal: %s", e)

        if platform.system().lower() == "windows":
            fd, wav_path = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            try:
                sapi_rate = max(-5, min(2, int(round((rate - 1.0) * 10))))
                escaped = text.replace("'", "''")
                ps_cmd = (
                    "Add-Type -AssemblyName System.Speech; "
                    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                    f"$s.Rate = {sapi_rate}; "
                    f"$s.SetOutputToWaveFile('{wav_path}'); "
                    f"$s.Speak('{escaped}'); "
                    "$s.Dispose();"
                )
                proc = await asyncio.create_subprocess_exec(
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    ps_cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(proc.wait(), timeout=12.0)
                p = Path(wav_path)
                if p.exists() and p.stat().st_size > 100:
                    return p.read_bytes()
            except Exception as e:
                log.debug("Windows SAPI TTS selhalo: %s", e)
            finally:
                Path(wav_path).unlink(missing_ok=True)
        return None


def _fix_streamed_wav(data: bytes) -> bytes:
    """espeak-ng `--stdout` píše WAV s neznámou délkou (0x7FFFFFFF) – přepíše hlavičku na skutečnou délku,
    jinak by DSP / přehrávače počítaly s nesmyslnou délkou nahrávky."""
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            params = w.getparams()
            frames = w.readframes(w.getnframes())
        out = io.BytesIO()
        with wave.open(out, "wb") as w:
            w.setnchannels(params.nchannels)
            w.setsampwidth(params.sampwidth)
            w.setframerate(params.framerate)
            w.writeframes(frames)
        return out.getvalue()
    except Exception:
        return data
