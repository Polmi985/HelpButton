"""Zvukový vstup/výstup pro Raspberry Pi 5: Audio Injector Zero (WM8731) HAT, PipeWire, převzorkování, DSP a VAD.

Cílový hardware: Raspberry Pi 5 + Audio Injector Zero (I2S HAT, kodek WM8731, overlay
`audioinjector-wm8731-audio`, ALSA karta `audioinjectorpi`), PipeWire 1.4 + WirePlumber.

Řeší reálné problémy hardwaru a akustiky v domácnosti seniora (čistě softwarově):
  1. Automaticky vybere zvukovou cestu v pořadí: PipeWire (`pipewire` / `pulse` / `default`,
     převzorkování 48 kHz -> 16 kHz mono řeší PipeWire) -> přímo HAT přes ALSA (`audioinjectorpi`,
     nativně 48 kHz / 2 kanály, převod na 16 kHz mono v Pythonu) -> USB zvukovka -> cokoliv mimo HDMI,
     včetně automatického zotavení (hot-plug self-healing) při výpadku zařízení.
  2. Převzorkuje zvuk z nativní frekvence (48 kHz stereo u WM8731) na 16 kHz mono
     v čistém Pythonu (modul `array`, nezávislý na odstraněném `audioop` v Pythonu 3.13+).
  3. Poskytuje "Senior Clarity" DSP filtr pro USB reproduktor (140 Hz High-Pass filtr proti dunění,
     +3.8 dB zesílení souhlásek nad 2.2 kHz pro kompenzaci stařecké nedoslýchavosti a soft-knee kompresor).
  4. Poskytuje "Grandpa-Proof++" detekci řeči (VAD):
     - 500 ms pre-roll buffer (nikdy neuřízne první slabiku slova),
     - odfiltrování vzdálené televize v obýváku (Near-Field SNR Gate – ignoruje hlasy >13 dB slabší než hlas u mikrofonu),
     - 3-úrovňová dynamická detekce konce řeči (Semantic EoT):
       * 0.65 s pro rychlé "Ano" / "Ne" při odpovědi na otázku (ušetří >1 sekundu čekání),
       * 2.2 s standardní tolerance pomlky na přemýšlení uprostřed věty,
       * 2.8 s prodloužená tolerance, pokud věta končí nerozhodně ("a...", "že...", "počkej..."),
       * okamžité ukončení po průběžném Whisper přepisu (`on_pause_check`), jakmile je příkaz kompletní.
"""

from __future__ import annotations

import array
import collections
import io
import logging
import math
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .config import RpiConfig

log = logging.getLogger("helpbutton.rpi.audio")

USB_KEYWORDS = (
    "usb",
    "pnp",
    "zvukov",
    "sound",
    "mic",
    "audio",
    "headset",
    "c-media",
    "jabra",
    "retekess",
    "tonor",
    "fifine",
)
HDMI_KEYWORDS = ("hdmi", "vc4", "bcm2835 hdmi")
HAT_KEYWORDS = ("audioinjector", "audio injector", "wm8731")
PIPEWIRE_DEVICE_NAMES = ("pipewire", "pulse", "default")

HAT_NATIVE_RATE = 48000
HAT_NATIVE_CHANNELS = 2


@dataclass
class AudioDeviceInfo:
    index: int | str
    name: str
    is_input: bool
    is_output: bool
    default_samplerate: int = 16000
    is_usb: bool = False
    is_hat: bool = False
    is_pipewire: bool = False
    max_input_channels: int = 1
    max_output_channels: int = 2


def _classify_device_name(name: str) -> tuple[bool, bool, bool]:
    """Vrátí (is_usb, is_hat, is_pipewire) podle názvu zařízení."""
    low = name.lower().strip()
    is_hdmi = any(h in low for h in HDMI_KEYWORDS)
    is_hat = any(k in low for k in HAT_KEYWORDS)
    is_pw = low in PIPEWIRE_DEVICE_NAMES or low.startswith("pipewire")
    is_usb = (not is_hat) and (not is_pw) and (not is_hdmi) and any(k in low for k in USB_KEYWORDS)
    return is_usb, is_hat, is_pw


def pipewire_running() -> bool:
    """Zjistí, zda v uživatelské relaci běží zvukový server PipeWire (socket `pipewire-0`).

    Pozor: systemd služba spuštěná jako root / bez `XDG_RUNTIME_DIR` PipeWire nevidí –
    v tom případě se automaticky použije přímý přístup k HATu přes ALSA.
    """
    if platform.system().lower() != "linux":
        return False
    override = os.environ.get("HELPBUTTON_PIPEWIRE")
    if override is not None:
        return override.strip().lower() in ("1", "true", "yes", "on")
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime:
        try:
            runtime = f"/run/user/{os.getuid()}"  # type: ignore[attr-defined]
        except AttributeError:
            return False
    return Path(runtime, "pipewire-0").exists()


@dataclass
class RecordedUtterance:
    wav_bytes: bytes
    pcm_bytes: bytes
    duration_s: float
    speech_duration_s: float
    peak_rms: float
    timed_out: bool = False
    canceled: bool = False
    pre_transcribed_text: str = ""
    pre_transcribed_lang: str = "cs"
    pre_transcription_pending: bool = False
    speech_end_bytes: int = 0
    sample_rate: int = 16000

    @property
    def has_speech(self) -> bool:
        return (
            len(self.pcm_bytes) > 0
            and self.speech_duration_s > 0
            and not self.timed_out
            and not self.canceled
        )

    @property
    def stt_pcm_bytes(self) -> bytes:
        if 0 < self.speech_end_bytes < len(self.pcm_bytes):
            return self.pcm_bytes[: self.speech_end_bytes]
        return self.pcm_bytes

    @property
    def stt_wav_bytes(self) -> bytes:
        pcm = self.stt_pcm_bytes
        if len(pcm) == len(self.pcm_bytes):
            return self.wav_bytes
        return pcm_to_wav(pcm, self.sample_rate)


def pcm16_samples(pcm: bytes) -> array.array:
    """Převede surové 16-bit signed little-endian bajty na pole shortů."""
    arr = array.array("h")
    n = len(pcm) - (len(pcm) % 2)
    arr.frombytes(pcm[:n])
    if sys.byteorder != "little":
        arr.byteswap()
    return arr


def samples_to_pcm16(samples: array.array) -> bytes:
    """Převede pole shortů zpět na 16-bit little-endian bajty."""
    if sys.byteorder != "little":
        cp = array.array("h", samples)
        cp.byteswap()
        return cp.tobytes()
    return samples.tobytes()


def _i16(pcm: bytes):
    """16-bit PCM jako numpy pole int16 (rychlá cesta; volat jen když `_np` není None)."""
    return _np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype="<i2")


def pcm16_rms(pcm: bytes) -> float:
    """Spočítá RMS energii (hlasitost) 16-bit PCM rámce."""
    if len(pcm) < 2:
        return 0.0
    if _np is not None:
        x = _i16(pcm).astype(_np.float64)
        return math.sqrt(float(_np.dot(x, x)) / x.size) if x.size else 0.0
    samples = pcm16_samples(pcm)
    if not samples:
        return 0.0
    total = sum(s * s for s in samples)
    return math.sqrt(total / len(samples))


def pcm16_zcr(pcm: bytes) -> float:
    """Spočítá Zero-Crossing Rate (poměr průchodů nulou) pro rozlišení hlasu od dunění."""
    if _np is not None:
        x = _i16(pcm)
        if x.size < 2:
            return 0.0
        neg = x < 0
        return float(_np.count_nonzero(neg[1:] != neg[:-1])) / (x.size - 1)
    samples = pcm16_samples(pcm)
    if len(samples) < 2:
        return 0.0
    crossings = 0
    prev = samples[0]
    for s in samples[1:]:
        if (prev >= 0 > s) or (prev < 0 <= s):
            crossings += 1
        prev = s
    return crossings / (len(samples) - 1)


def resample_pcm16_mono(pcm: bytes, src_rate: int, dst_rate: int = 16000) -> bytes:
    """Převzorkuje 16-bit mono PCM z `src_rate` (např. 48000 Hz) na `dst_rate` (16000 Hz)."""
    if src_rate == dst_rate or len(pcm) < 4:
        return pcm
    if _np is not None:
        return _resample_np(pcm, src_rate, dst_rate)
    src = pcm16_samples(pcm)
    n_src = len(src)
    if n_src == 0:
        return b""
    if src_rate % dst_rate == 0:
        step = src_rate // dst_rate
        out = array.array(
            "h",
            (int(sum(src[i : i + step]) / len(src[i : i + step])) for i in range(0, n_src - step + 1, step)),
        )
    else:
        n_dst = max(1, int(round(n_src * dst_rate / src_rate)))
        ratio = (n_src - 1) / max(1, n_dst - 1)
        out = array.array("h")
        for i in range(n_dst):
            pos = i * ratio
            idx = int(pos)
            frac = pos - idx
            if idx + 1 < n_src:
                val = int(src[idx] * (1.0 - frac) + src[idx + 1] * frac)
            else:
                val = src[idx]
            out.append(max(-32768, min(32767, val)))
    return samples_to_pcm16(out)


def _resample_np(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """numpy verze `resample_pcm16_mono` se shodným výsledkem (průměr bloků / lineární interpolace)."""
    x = _i16(pcm).astype(_np.float64)
    n_src = x.size
    if n_src == 0:
        return b""
    if src_rate % dst_rate == 0:
        step = src_rate // dst_rate
        k = n_src // step
        out = _np.trunc(x[: k * step].reshape(k, step).mean(axis=1)) if k else x[:0]
    else:
        n_dst = max(1, int(round(n_src * dst_rate / src_rate)))
        ratio = (n_src - 1) / max(1, n_dst - 1)
        pos = _np.arange(n_dst) * ratio
        idx = _np.minimum(pos.astype(_np.int64), n_src - 1)
        frac = pos - idx
        has_next = idx + 1 < n_src
        nxt = _np.where(has_next, idx + 1, idx)
        out = _np.where(has_next, _np.trunc(x[idx] * (1.0 - frac) + x[nxt] * frac), x[idx])
    return _np.clip(out, -32768, 32767).astype("<i2").tobytes()


def _stereo_to_mono_np(pcm: bytes) -> bytes:
    x = _i16(pcm)
    n_pairs = x.size // 2
    if n_pairs == 0:
        return b""
    st = x[: n_pairs * 2].reshape(n_pairs, 2).astype(_np.int64)
    left, right = st[:, 0], st[:, 1]
    sum_l2 = int(_np.dot(left, left))
    sum_r2 = int(_np.dot(right, right))
    rms_l = math.sqrt(sum_l2 / n_pairs)
    rms_r = math.sqrt(sum_r2 / n_pairs)
    max_rms, min_rms = max(rms_l, rms_r), min(rms_l, rms_r)
    if max_rms > 120.0 and min_rms < max_rms * 0.15:
        return (left if rms_l >= rms_r else right).astype("<i2").tobytes()
    if min_rms > 80.0:
        denom = math.sqrt(float(sum_l2) * float(sum_r2))
        if denom > 0 and (int(_np.dot(left, right)) / denom) < -0.55:
            return _np.clip((left - right) // 2, -32768, 32767).astype("<i2").tobytes()
    return ((left + right) // 2).astype("<i2").tobytes()


def stereo_to_mono_pcm16(pcm: bytes) -> bytes:
    """Převede 2-kanálové 16-bit PCM na 1-kanálové mono.

    Ošetřuje tři reálné situace u mikrofonních vstupů:
      1. Oba kanály nesou stejný signál (např. WM8731 Input Mux = Mic) -> průměr (L + R) // 2.
      2. Jeden kanál je hluchý/odpojený (časté u TRS redukcí a USB karet) -> vezme aktivní kanál
         bez ztráty −6 dB.
      3. Kanály jsou v protifázi (symetrické zapojení, L ≈ −R) -> odečte (L − R) // 2 místo vyrušení.
    """
    if _np is not None:
        return _stereo_to_mono_np(pcm)
    samples = pcm16_samples(pcm)
    n_pairs = len(samples) // 2
    if n_pairs == 0:
        return b""
    left = samples[0 : n_pairs * 2 : 2]
    right = samples[1 : n_pairs * 2 : 2]
    sum_l2 = sum(int(v) * int(v) for v in left)
    sum_r2 = sum(int(v) * int(v) for v in right)
    rms_l = math.sqrt(sum_l2 / n_pairs)
    rms_r = math.sqrt(sum_r2 / n_pairs)
    max_rms = max(rms_l, rms_r)
    min_rms = min(rms_l, rms_r)
    if max_rms > 120.0 and min_rms < max_rms * 0.15:
        active = left if rms_l >= rms_r else right
        return samples_to_pcm16(array.array("h", active))
    if min_rms > 80.0:
        dot = sum(int(l) * int(r) for l, r in zip(left, right))
        denom = math.sqrt(float(sum_l2) * float(sum_r2))
        if denom > 0 and (dot / denom) < -0.55:
            out_diff = array.array(
                "h", (max(-32768, min(32767, (int(l) - int(r)) // 2)) for l, r in zip(left, right))
            )
            return samples_to_pcm16(out_diff)
    out = array.array("h", ((int(l) + int(r)) // 2 for l, r in zip(left, right)))
    return samples_to_pcm16(out)


def mono_to_stereo_pcm16(pcm: bytes) -> bytes:
    """Zdvojí 16-bit mono PCM do obou kanálů (WM8731 bez plughw vyžaduje 2 kanály)."""
    samples = pcm16_samples(pcm)
    out = array.array("h", bytes(len(samples) * 4))
    out[0::2] = samples
    out[1::2] = samples
    return samples_to_pcm16(out)


def downmix_to_mono_pcm16(pcm: bytes, channels: int) -> bytes:
    """Převede N-kanálové 16-bit PCM na mono (WM8731 posílá mono mikrofon na oba kanály)."""
    if channels <= 1:
        return pcm
    if channels == 2:
        return stereo_to_mono_pcm16(pcm)
    if _np is not None:
        x = _i16(pcm)
        k = x.size // channels
        if k == 0:
            return b""
        return (
            _np.trunc(x[: k * channels].reshape(k, channels).astype(_np.float64).mean(axis=1))
            .astype("<i2")
            .tobytes()
        )
    samples = pcm16_samples(pcm)
    out = array.array(
        "h",
        (
            int(sum(samples[i : i + channels]) / channels)
            for i in range(0, len(samples) - channels + 1, channels)
        ),
    )
    return samples_to_pcm16(out)


def pcm_to_wav(pcm: bytes, rate: int = 16000, channels: int = 1, width: int = 2) -> bytes:
    """Zabalí surové PCM do WAV kontejneru v paměti."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


try:
    import numpy as _np
except Exception:  # pragma: no cover
    _np = None
try:
    from scipy.signal import lfilter as _lfilter
except Exception:
    _lfilter = None

OUTPUT_PEAK = 26800.0
LOUDNESS_THRESHOLD_DB = -24.0
LOUDNESS_RATIO = 3.0
LIMITER_DRIVE_DB = 7.0
CLEANUP_PEAK = 24000.0
GATE_MIN_GAIN = 0.72
CLICK_RATIO = 7.5
CLICK_MIN_PEAK = 8000


def _alpha_hp(fc: float, rate: int) -> float:
    rc = 1.0 / (2.0 * math.pi * fc)
    dt = 1.0 / max(8000, rate)
    return rc / (rc + dt)


def _alpha_lp(fc: float, rate: int) -> float:
    rc = 1.0 / (2.0 * math.pi * fc)
    dt = 1.0 / max(8000, rate)
    return dt / (rc + dt)


def _py_hp1(xs: list[float], alpha: float, x_prev: float) -> list[float]:
    out = [0.0] * len(xs)
    py, px = 0.0, float(x_prev)
    for i, v in enumerate(xs):
        py = alpha * (py + v - px)
        px = v
        out[i] = py
    return out


def _py_lp1(xs: list[float], alpha: float) -> list[float]:
    out = [0.0] * len(xs)
    y = 0.0
    for i, v in enumerate(xs):
        y = y + alpha * (v - y)
        out[i] = y
    return out


def _np_hp1(x, alpha: float, x_prev: float):
    if _lfilter is not None:
        return _lfilter([alpha, -alpha], [1.0, -alpha], x, zi=_np.array([-alpha * x_prev]))[0]
    return _np.asarray(_py_hp1(x.tolist(), alpha, x_prev), dtype=_np.float64)


def _np_lp1(x, alpha: float):
    if _lfilter is not None:
        return _lfilter([alpha], [1.0, alpha - 1.0], x, zi=_np.array([0.0]))[0]
    return _np.asarray(_py_lp1(x.tolist(), alpha), dtype=_np.float64)


def _np_samples(pcm: bytes):
    n = len(pcm) - (len(pcm) % 2)
    return _np.frombuffer(pcm[:n], dtype="<i2").astype(_np.float64)


def _np_to_pcm16(y) -> bytes:
    return _np.clip(_np.rint(y), -32767, 32767).astype("<i2").tobytes()


def _py_to_pcm16(ys: list[float]) -> bytes:
    return samples_to_pcm16(array.array("h", (max(-32767, min(32767, round(v))) for v in ys)))


def _frame_bounds(n: int, frame_len: int) -> list[tuple[int, int]]:
    return [(off, min(off + frame_len, n)) for off in range(0, n, frame_len)]


def _py_interp(n: int, centers: list[float], gains: list[float]) -> list[float]:
    """Stejné jako numpy.interp(range(n), centers, gains) – plynulá křivka zisku mezi středy rámců."""
    out = [0.0] * n
    if not centers:
        return [1.0] * n
    j = 0
    last = len(centers) - 1
    for i in range(n):
        if i <= centers[0]:
            out[i] = gains[0]
        elif i >= centers[last]:
            out[i] = gains[last]
        else:
            while centers[j + 1] < i:
                j += 1
            c0, c1 = centers[j], centers[j + 1]
            out[i] = gains[j] + (gains[j + 1] - gains[j]) * ((i - c0) / (c1 - c0))
    return out


def _loudness_block_gains(block_rms: list[float], block_s: float, threshold_db: float) -> list[float]:
    """Kompresor (náběh 12 ms, uvolnění 140 ms, 3:1): plynulé vyrovnání dynamiky bez dýchání."""
    att = 1.0 - math.exp(-block_s / 0.012)
    rel = 1.0 - math.exp(-block_s / 0.140)
    state = 0.0
    gains: list[float] = []
    for r in block_rms:
        state += (r - state) * (att if r > state else rel)
        level_db = 20.0 * math.log10(max(state, 1e-6) / 32768.0)
        over = level_db - threshold_db
        g_db = -over * (1.0 - 1.0 / LOUDNESS_RATIO) if over > 0 else 0.0
        gains.append(10.0 ** (g_db / 20.0))
    return gains


def _limiter_block_gains(block_peaks: list[float], ceiling: float, block_s: float) -> list[float]:
    """Lookahead limiter po 2ms blocích (±4 ms předstih, uvolnění 90 ms): zamezí inter-sample ořezu."""
    req = [min(1.0, ceiling / p) if p > 0 else 1.0 for p in block_peaks]
    n = len(req)
    need = [min(req[max(0, k - 2) : k + 3]) for k in range(n)]
    rel = 1.0 - math.exp(-block_s / 0.090)
    out: list[float] = []
    state = 1.0
    for v in need:
        state = min(v, state + (1.0 - state) * rel)
        out.append(state)
    return out


def _np_lookahead_limit(y, rate: int, ceiling: float, drive_db: float):
    y = y * (10.0 ** (drive_db / 20.0))
    n = y.size
    blk = max(16, int(rate * 0.002))
    nb = -(-n // blk)
    padded = _np.zeros(nb * blk)
    padded[:n] = _np.abs(y)
    peaks = padded.reshape(nb, blk).max(axis=1).tolist()
    gains = _limiter_block_gains(peaks, ceiling, blk / rate)
    centers = _np.arange(nb) * blk + (blk - 1) / 2.0
    return y * _np.interp(_np.arange(n), centers, gains)


def _py_lookahead_limit(ys: list[float], rate: int, ceiling: float, drive_db: float) -> list[float]:
    k = 10.0 ** (drive_db / 20.0)
    ys = [v * k for v in ys]
    n = len(ys)
    blk = max(16, int(rate * 0.002))
    nb = -(-n // blk)
    peaks = [max(abs(v) for v in ys[b * blk : (b + 1) * blk]) for b in range(nb)]
    gains = _limiter_block_gains(peaks, ceiling, blk / rate)
    curve = _py_interp(n, [b * blk + (blk - 1) / 2.0 for b in range(nb)], gains)
    return [v * g for v, g in zip(ys, curve)]


def apply_senior_clarity_dsp_pcm16(
    pcm: bytes,
    rate: int = 22050,
    presence_gain: float = 1.55,
    extra_gain: float = 1.0,
    *,
    eq: bool = True,
    loudness: bool = False,
) -> bytes:
    """Úprava řeči HelpButton před přehráním:
    1. `eq`: 140 Hz High-Pass (bez dunění) + pásmové zvýraznění souhlásek 2,2–5,6 kHz bez syčivého vocoder šumu,
    2. `loudness`: kompresor 3:1 + lookahead limiter na ≈ −1,75 dBFS s rezervou pro převzorkování na 48 kHz,
    3. vždy: lineární bezpečnostní strop proti ořezu (žádné chrčení).
    """
    if len(pcm) < 4:
        return pcm
    hf_boost = max(0.0, presence_gain - 1.0)
    a_hp, a_lp_lo, a_lp_hi = _alpha_hp(140.0, rate), _alpha_lp(1900.0, rate), _alpha_lp(6200.0, rate)
    threshold = LOUDNESS_THRESHOLD_DB - 3.0 * 20.0 * math.log10(max(1.0, extra_gain))
    drive_db = LIMITER_DRIVE_DB + 20.0 * math.log10(max(1.0, extra_gain))
    block = max(32, int(rate * 0.010))

    if _np is not None:
        x = _np_samples(pcm)
        if x.size == 0:
            return pcm
        if eq:
            hp = _np_hp1(x, a_hp, float(x[0]))
            lp_lo = _np_lp1(hp, a_lp_lo)
            lp_hi = _np_lp1(hp, a_lp_hi)
            y = hp + hf_boost * (lp_hi - lp_lo)
        else:
            y = x.copy()
        peak = float(_np.max(_np.abs(y))) if y.size else 0.0
        if loudness and peak > 1.0:
            y *= OUTPUT_PEAK / peak
            n = y.size
            bounds = _frame_bounds(n, block)
            rms = [float(_np.sqrt(_np.mean(y[a:b] ** 2))) for a, b in bounds]
            gains = _loudness_block_gains(rms, block / rate, threshold)
            centers = [(a + b - 1) / 2.0 for a, b in bounds]
            y *= _np.interp(_np.arange(n), centers, gains)
            peak = float(_np.max(_np.abs(y)))
            if peak > 1.0:
                y *= OUTPUT_PEAK / peak
            y = _np_lookahead_limit(y, rate, OUTPUT_PEAK, drive_db)
            peak = float(_np.max(_np.abs(y)))
            if peak > OUTPUT_PEAK:
                y *= OUTPUT_PEAK / peak
        else:
            y *= extra_gain
            peak = float(_np.max(_np.abs(y))) if y.size else 0.0
            if peak > OUTPUT_PEAK:
                y *= OUTPUT_PEAK / peak
        return _np_to_pcm16(y)

    xs = [float(s) for s in pcm16_samples(pcm)]
    if not xs:
        return pcm
    if eq:
        hp = _py_hp1(xs, a_hp, xs[0])
        lp_lo = _py_lp1(hp, a_lp_lo)
        lp_hi = _py_lp1(hp, a_lp_hi)
        ys = [h + hf_boost * (hi - lo) for h, lo, hi in zip(hp, lp_lo, lp_hi)]
    else:
        ys = list(xs)
    peak = max((abs(v) for v in ys), default=0.0)
    if loudness and peak > 1.0:
        k = OUTPUT_PEAK / peak
        ys = [v * k for v in ys]
        n = len(ys)
        bounds = _frame_bounds(n, block)
        rms = [math.sqrt(sum(v * v for v in ys[a:b]) / (b - a)) for a, b in bounds]
        gains = _loudness_block_gains(rms, block / rate, threshold)
        curve = _py_interp(n, [(a + b - 1) / 2.0 for a, b in bounds], gains)
        ys = [v * g for v, g in zip(ys, curve)]
        peak = max(abs(v) for v in ys)
        if peak > 1.0:
            k = OUTPUT_PEAK / peak
            ys = [v * k for v in ys]
        ys = _py_lookahead_limit(ys, rate, OUTPUT_PEAK, drive_db)
        peak = max(abs(v) for v in ys)
        if peak > OUTPUT_PEAK:
            k = OUTPUT_PEAK / peak
            ys = [v * k for v in ys]
    else:
        ys = [v * extra_gain for v in ys]
        peak = max((abs(v) for v in ys), default=0.0)
        if peak > OUTPUT_PEAK:
            k = OUTPUT_PEAK / peak
            ys = [v * k for v in ys]
    return _py_to_pcm16(ys)


def apply_senior_clarity_dsp_wav(
    wav_bytes: bytes,
    presence_gain: float = 1.55,
    extra_gain: float = 1.0,
    *,
    eq: bool = True,
    loudness: bool = False,
) -> bytes:
    """Aplikuje Senior Clarity DSP (a volitelně zesílení hlasitosti) přímo na WAV soubor v paměti."""
    if not wav_bytes or len(wav_bytes) <= 44:
        return wav_bytes
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as w:
            channels = w.getnchannels()
            width = w.getsampwidth()
            rate = w.getframerate()
            raw = w.readframes(w.getnframes())
        if width != 2:
            return wav_bytes
        if channels > 1:
            raw = downmix_to_mono_pcm16(raw, channels)
            channels = 1
        processed = apply_senior_clarity_dsp_pcm16(
            raw,
            rate=rate,
            presence_gain=presence_gain,
            extra_gain=extra_gain,
            eq=eq,
            loudness=loudness,
        )
        return pcm_to_wav(processed, rate=rate, channels=channels, width=2)
    except Exception as e:
        log.debug("Senior Clarity DSP přeskočeno: %s", e)
        return wav_bytes


def is_transient_pop_frame(pcm_frame: bytes) -> bool:
    """Detekuje krátké mechanické lupnutí / ťuknutí hrnkem do stolu v jednom 30ms rámci.
    Lidská hláska včetně souhlásek (p, t, k, č) a zvuků úst trvá >2 ms, zatímco mechanické
    lupnutí má extrémní špičku v ≤5,5 % vzorků (<1,6 ms) a vysoký činitel výkyvu (Crest Factor).
    """
    if _np is not None:
        a = _np.abs(_i16(pcm_frame).astype(_np.float64))
        if a.size < 32:
            return False
        max_abs = float(a.max())
        if max_abs < 2400:
            return False
        rms = math.sqrt(float(_np.dot(a, a)) / a.size)
        if rms <= 1.0:
            return False
        active_ratio = float(_np.count_nonzero(a >= max_abs * 0.35)) / a.size
        return max_abs / rms >= 5.5 and active_ratio <= 0.055
    samples = pcm16_samples(pcm_frame)
    n = len(samples)
    if n < 32:
        return False
    max_abs = max(abs(s) for s in samples)
    if max_abs < 2400:
        return False
    rms = pcm16_rms(pcm_frame)
    if rms <= 1.0:
        return False
    crest_factor = max_abs / rms
    high_samples = sum(1 for s in samples if abs(s) >= max_abs * 0.35)
    active_ratio = high_samples / n
    return crest_factor >= 5.5 and active_ratio <= 0.055


def _click_gain_curve(peaks: list[int], n: int, win: int) -> list[tuple[int, int, float]]:
    """Najde izolovaná mechanická/elektrická ťuknutí: 4ms okno ≥ 8 000 a ≥7,5× silnější než
    celé široké okolí (±24 ms = ±6 oken). Souhlásky (p, t, k, c, č) na začátku i na konci slov
    (včetně 15–25ms VOT mezery před samohláskou) i přirozené zvuky úst tak zůstanou 100% nedotčené."""
    num = len(peaks)
    hits: list[tuple[int, int, float]] = []
    for w in range(num):
        if peaks[w] < CLICK_MIN_PEAK:
            continue
        left_slice = peaks[max(0, w - 6) : w]
        right_slice = peaks[w + 1 : min(num, w + 7)]
        neighbors = left_slice + right_slice
        ref = max([250, *neighbors]) if neighbors else 250
        if peaks[w] > ref * CLICK_RATIO:
            hits.append((w * win, w * win + win, ref / float(peaks[w])))
    return hits


def suppress_transients_pcm16(pcm: bytes, rate: int = 16000) -> bytes:
    """Ztlumí izolovaná ťuknutí do stolu / lupnutí. Zeslabení má plynulé náběhy (žádné cvaknutí)."""
    if len(pcm) < 128:
        return pcm
    win = max(16, int(rate * 0.004))
    ramp = max(1, win // 2)

    if _np is not None:
        x = _np_samples(pcm)
        n = x.size
        num = n // win
        if num < 4:
            return pcm
        peaks = _np.abs(x[: num * win]).reshape(num, win).max(axis=1).astype(int).tolist()
        hits = _click_gain_curve(peaks, n, win)
        if not hits:
            return pcm
        gain = _np.ones(n)
        steps = _np.arange(1, ramp + 1) / (ramp + 1.0)
        for a, b, scale in hits:
            gain[a:b] = _np.minimum(gain[a:b], scale)
            lo = max(0, a - ramp)
            pre = 1.0 - (1.0 - scale) * steps[ramp - (a - lo) :]
            gain[lo:a] = _np.minimum(gain[lo:a], pre)
            hi = min(n, b + ramp)
            post = scale + (1.0 - scale) * steps[: hi - b]
            gain[b:hi] = _np.minimum(gain[b:hi], post)
        return _np_to_pcm16(x * gain)

    samples = pcm16_samples(pcm)
    n = len(samples)
    num = n // win
    if num < 4:
        return pcm
    peaks = [max(abs(samples[w * win + i]) for i in range(win)) for w in range(num)]
    hits = _click_gain_curve(peaks, n, win)
    if not hits:
        return pcm
    gain = [1.0] * n
    for a, b, scale in hits:
        for i in range(a, b):
            gain[i] = min(gain[i], scale)
        for k in range(1, ramp + 1):
            t = k / (ramp + 1.0)
            if a - ramp - 1 + k >= 0:
                i = a - ramp - 1 + k
                gain[i] = min(gain[i], 1.0 - (1.0 - scale) * t)
            if b + k - 1 < n:
                i = b + k - 1
                gain[i] = min(gain[i], scale + (1.0 - scale) * t)
    return _py_to_pcm16([float(s) * g for s, g in zip(samples, gain)])


def _estimate_noise_floor(frame_rms: list[float]) -> float:
    sorted_rms = sorted(frame_rms)
    if sorted_rms[-1] >= sorted_rms[0] * 2.5:
        return min(140.0, sorted_rms[max(0, len(sorted_rms) // 6)])
    return min(50.0, sorted_rms[0] * 0.4)


def _agc_and_gate_gains(
    frame_rms: list[float],
    peak: float,
    *,
    frame_peaks: list[float] | None = None,
    noise_floor_rms: float,
    agc_enabled: bool,
    target_rms: float,
    max_gain: float,
    noise_subtraction: bool,
) -> tuple[float, list[float]]:
    """Vrátí (jeden zisk pro CELOU nahrávku, zisky šumové brány a ochrany špiček po rámcích).

    Odolnost proti lupnutí zblízka (Pop-Immune AGC):
      - Zisk řeči se počítá z aktivních řečových rámců a omezuje podle trvalé špičky řeči
        (85. percentil rámcových špiček), nikoliv podle jediného vybočujícího vzorku.
      - Pokud některý rámec obsahuje hlasité fouknutí/lupnutí úst, lokální 3-rámcový lookahead
        strop plynule sníží křivku `gate` v místě špičky na `CLEANUP_PEAK` (24 000), takže
        zbytek tiché věty zůstane plně zesílený a přitom nikde nevznikne ořez.
    """
    gain = 1.0
    speech_gate = max(120.0, noise_floor_rms * 1.75)
    if agc_enabled and frame_rms:
        active = sorted(r for r in frame_rms if r >= speech_gate) or [max(frame_rms)]
        ref_rms = active[min(len(active) - 1, int(len(active) * 0.75))]
        if 40.0 < ref_rms < target_rms:
            gain = min(max_gain, target_rms / ref_rms)
    if frame_peaks and len(frame_peaks) >= 4:
        active_peaks = sorted(p for r, p in zip(frame_rms, frame_peaks) if r >= speech_gate) or sorted(
            frame_peaks
        )
        sustained_peak = active_peaks[min(len(active_peaks) - 1, int(len(active_peaks) * 0.85))]
    else:
        sustained_peak = peak
    if sustained_peak > 0 and sustained_peak * gain > CLEANUP_PEAK:
        gain = CLEANUP_PEAK / sustained_peak
    gate = [1.0] * len(frame_rms)
    if noise_subtraction:
        thresh = max(60.0, noise_floor_rms * 1.35)
        gate = [1.0 if r >= thresh else max(GATE_MIN_GAIN, r / thresh) for r in frame_rms]
    if frame_peaks and gain > 0:
        n_frames = len(frame_peaks)
        for k in range(n_frames):
            local_peak = max(frame_peaks[max(0, k - 1) : min(n_frames, k + 2)])
            if local_peak * gain * gate[k] > CLEANUP_PEAK:
                gate[k] = min(gate[k], CLEANUP_PEAK / (local_peak * gain))
    elif peak > 0 and peak * gain > CLEANUP_PEAK:
        gain = CLEANUP_PEAK / peak
    return gain, gate


def apply_mic_speech_cleanup_pcm16(
    pcm: bytes,
    rate: int = 16000,
    *,
    bandpass: bool = True,
    transient_filter: bool = True,
    noise_subtraction: bool = True,
    noise_floor_rms: float = 0.0,
    agc_enabled: bool = True,
    target_rms: float = 4800.0,
    max_gain: float = 6.0,
) -> bytes:
    """Čisté zpracování zvuku z mikrofonu před STT se zachováním artikulace a zvuků úst:
    1. ztlumení pouze izolovaných mechanických ťuknutí (souhlásky p/t/k/č i dech zůstávají),
    2. 2-stupňová horní propust 75 Hz odstraní DC posun a 50Hz síťový brum; při 16 kHz se
       NEPOUŽÍVÁ 1. řád dolní propusti 7,2 kHz (který dříve tlumil sykavky s/š/č už od 1,5 kHz)
       ani mezikrokové zaokrouhlování před AGC,
    3. Pop-Immune AGC zesílí i tichou řeč na cílovou úroveň, aniž by jediné lupnutí na začátku
       umlčelo zbytek věty,
    4. velmi jemná šumová brána v dlouhém tichu (max −2,8 dB, plynulá křivka bez skoků).
    """
    if len(pcm) < 64:
        return pcm

    if transient_filter:
        pcm = suppress_transients_pcm16(pcm, rate=rate)

    frame_len = max(160, int(rate * 0.020))
    a_hp = _alpha_hp(75.0, rate)
    use_lp = rate > 20000
    a_lp = _alpha_lp(7500.0, rate) if use_lp else 1.0

    if _np is not None:
        x = _np_samples(pcm)
        n = x.size
        if n == 0:
            return pcm
        if bandpass:
            h1 = _np_hp1(x, a_hp, float(x[0]))
            h2 = _np_hp1(h1, a_hp, 0.0)
            x = _np_lp1(h2, a_lp) if use_lp else h2
        bounds = _frame_bounds(n, frame_len)
        frame_rms = [float(_np.sqrt(_np.mean(x[a:b] ** 2))) for a, b in bounds]
        frame_peaks = [float(_np.max(_np.abs(x[a:b]))) for a, b in bounds]
        floor = noise_floor_rms if noise_floor_rms > 0.0 else _estimate_noise_floor(frame_rms)
        gain, gate = _agc_and_gate_gains(
            frame_rms,
            max(frame_peaks, default=0.0),
            frame_peaks=frame_peaks,
            noise_floor_rms=floor,
            agc_enabled=agc_enabled,
            target_rms=target_rms,
            max_gain=max_gain,
            noise_subtraction=noise_subtraction,
        )
        curve = _np.interp(_np.arange(n), [(a + b - 1) / 2.0 for a, b in bounds], gate)
        return _np_to_pcm16(_np.clip(x * (gain * curve), -CLEANUP_PEAK, CLEANUP_PEAK))

    samples = [float(s) for s in pcm16_samples(pcm)]
    n = len(samples)
    if n == 0:
        return pcm
    if bandpass:
        h1 = _py_hp1(samples, a_hp, samples[0])
        h2 = _py_hp1(h1, a_hp, 0.0)
        samples = _py_lp1(h2, a_lp) if use_lp else h2
    bounds = _frame_bounds(n, frame_len)
    frame_rms = [math.sqrt(sum(v * v for v in samples[a:b]) / (b - a)) for a, b in bounds]
    frame_peaks = [max(abs(v) for v in samples[a:b]) for a, b in bounds]
    floor = noise_floor_rms if noise_floor_rms > 0.0 else _estimate_noise_floor(frame_rms)
    gain, gate = _agc_and_gate_gains(
        frame_rms,
        max(frame_peaks, default=0.0),
        frame_peaks=frame_peaks,
        noise_floor_rms=floor,
        agc_enabled=agc_enabled,
        target_rms=target_rms,
        max_gain=max_gain,
        noise_subtraction=noise_subtraction,
    )
    curve = _py_interp(n, [(a + b - 1) / 2.0 for a, b in bounds], gate)
    return _py_to_pcm16([max(-CLEANUP_PEAK, min(CLEANUP_PEAK, v * gain * c)) for v, c in zip(samples, curve)])


def apply_mic_speech_cleanup_wav(
    wav_bytes: bytes,
    cfg: RpiConfig | None = None,
    noise_floor_rms: float = 0.0,
) -> bytes:
    """Vyčistí a normalizuje WAV nahrávku z mikrofonu před předáním do Whisper STT."""
    if not wav_bytes or len(wav_bytes) <= 44:
        return wav_bytes
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as w:
            channels = w.getnchannels()
            width = w.getsampwidth()
            rate = w.getframerate()
            raw = w.readframes(w.getnframes())
        if width != 2:
            return wav_bytes
        if channels > 1:
            raw = downmix_to_mono_pcm16(raw, channels)
            channels = 1
        cleaned = apply_mic_speech_cleanup_pcm16(
            raw,
            rate=rate,
            bandpass=cfg.mic_bandpass_filter if cfg else True,
            transient_filter=cfg.mic_transient_filter if cfg else True,
            noise_subtraction=cfg.mic_noise_subtraction if cfg else True,
            noise_floor_rms=noise_floor_rms,
            agc_enabled=cfg.mic_agc_enabled if cfg else True,
            target_rms=cfg.mic_agc_target_rms if cfg else 4800.0,
            max_gain=cfg.mic_agc_max_gain if cfg else 6.0,
        )
        return pcm_to_wav(cleaned, rate=rate, channels=channels, width=2)
    except Exception as e:
        log.debug("Čištění mikrofonního WAV přeskočeno: %s", e)
        return wav_bytes


def clipped_ratio(pcm: bytes) -> float:
    """Podíl vzorků na hraně rozsahu (přebuzený mikrofon = zkreslení, které už žádný filtr neopraví)."""
    if len(pcm) < 2:
        return 0.0
    if _np is not None:
        x = _np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype="<i2")
        return float(_np.count_nonzero(_np.abs(x.astype(_np.int32)) >= 32000)) / max(1, x.size)
    s = pcm16_samples(pcm)
    return sum(1 for v in s if v >= 32000 or v <= -32000) / max(1, len(s))


class Fir3Decimator:
    """48 kHz -> 16 kHz pro přímý přístup k HATu: pořádný anti-alias filtr (63 koeficientů, 7,2 kHz)
    místo průměru 3 vzorků, který propouštěl syčení nad 8 kHz zpět do pásma řeči.
    Pamatuje si stav mezi bloky, takže na hranách bloků nevznikají cvaknutí."""

    TAPS = 63

    def __init__(self) -> None:
        n = _np.arange(self.TAPS) - (self.TAPS - 1) / 2.0
        fc = 7200.0 / 48000.0
        h = 2.0 * fc * _np.sinc(2.0 * fc * n) * _np.blackman(self.TAPS)
        self.h = h / h.sum()
        self.hist = _np.zeros(self.TAPS - 1)
        self.phase = 0

    def process(self, pcm: bytes) -> bytes:
        x = _np_samples(pcm)
        if x.size == 0:
            return b""
        buf = _np.concatenate([self.hist, x])
        y = _np.convolve(buf, self.h, mode="valid")
        self.hist = buf[-(self.TAPS - 1) :]
        out = y[self.phase :: 3]
        self.phase = (self.phase - y.size) % 3
        return _np.clip(_np.round(out), -32767, 32767).astype("<i2").tobytes()


def _soft_headroom_pcm16(y, ceiling: float = 31800.0) -> bytes:
    """Lineárně ztlumí špičky převzorkování pod `ceiling`, aby nikdy nevznikl ořez."""
    peak = float(_np.max(_np.abs(y))) if y.size else 0.0
    if peak > ceiling:
        y = y * (ceiling / peak)
    return _np_to_pcm16(y)


def resample_pcm16_hq(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Převzorkování celé věty (např. 22 050 -> 48 000 Hz) se zrcadlovým doplněním okrajů:
    eliminuje lupnutí přechodu začátek/konec z kruhové FFT a drží špičky bez digitálního ořezu."""
    if src_rate == dst_rate or len(pcm) < 4:
        return pcm
    if _np is None:
        return resample_pcm16_mono(pcm, src_rate, dst_rate)
    x = _np_samples(pcm)
    n = x.size
    m = max(1, int(round(n * dst_rate / src_rate)))
    if _lfilter is not None and n >= 8:
        try:
            from scipy.signal import resample_poly  # type: ignore[import-untyped]

            g = math.gcd(int(dst_rate), int(src_rate))
            y = resample_poly(x, int(dst_rate) // g, int(src_rate) // g, padtype="line")
            if y.size >= m:
                y = y[:m]
            else:
                y = _np.pad(y, (0, m - y.size))
            return _soft_headroom_pcm16(y)
        except Exception:
            pass
    pad = min(n, max(16, int(round(src_rate * 0.025))))
    x_ext = _np.pad(x, pad, mode="symmetric")
    n_ext = x_ext.size
    m_ext = max(1, int(round(n_ext * dst_rate / src_rate)))
    spec = _np.fft.rfft(x_ext)
    out_spec = _np.zeros(m_ext // 2 + 1, dtype=complex)
    k = min(spec.size, out_spec.size)
    out_spec[:k] = spec[:k]
    y_ext = _np.fft.irfft(out_spec, m_ext) * (m_ext / n_ext)
    start = int(round(pad * dst_rate / src_rate))
    end = start + m
    if end > y_ext.size:
        y_ext = _np.pad(y_ext, (0, end - y_ext.size), mode="edge")
    return _soft_headroom_pcm16(y_ext[start:end])


TONE_AMPLITUDE = 16000
TICK_AMPLITUDE = 4400


def synthesize_tone_wav(kind: str, rate: int = 22050) -> bytes:
    """Vygeneruje příjemný WAV tón bez lupnutí na začátku/konci (kosinová obálka)."""
    amplitude = TONE_AMPLITUDE
    if kind == "beep":
        notes = [(587.33, 0.08), (880.0, 0.10)]
    elif kind == "listen_followup":
        notes = [(783.99, 0.09)]
    elif kind == "double":
        notes = [(659.25, 0.08), (0.0, 0.04), (880.0, 0.11)]
    elif kind == "low":
        notes = [(329.63, 0.16), (261.63, 0.22)]
    elif kind == "working_tick":
        amplitude = TICK_AMPLITUDE
        notes = [(392.00, 0.035), (0.0, 0.02), (523.25, 0.045)]
    else:
        return b""

    samples = array.array("h")
    fade_samples = max(1, int(rate * 0.012))
    for freq, dur in notes:
        total = max(1, int(rate * dur))
        for i in range(total):
            if freq <= 0.0:
                samples.append(0)
                continue
            env = 1.0
            if i < fade_samples:
                env = 0.5 * (1.0 - math.cos(math.pi * i / fade_samples))
            elif total - i < fade_samples:
                env = 0.5 * (1.0 - math.cos(math.pi * (total - i) / fade_samples))
            val = int(amplitude * env * math.sin(2.0 * math.pi * freq * i / rate))
            samples.append(val)
    return pcm_to_wav(samples_to_pcm16(samples), rate=rate)


def list_audio_devices() -> list[AudioDeviceInfo]:
    """Vrátí seznam dostupných zvukových zařízení z PortAudio (`sounddevice`) nebo ALSA."""
    devices: list[AudioDeviceInfo] = []
    pw_up = pipewire_running()
    try:
        import sounddevice as sd

        for idx, d in enumerate(sd.query_devices()):
            name = str(d.get("name", f"Device {idx}"))
            max_in = int(d.get("max_input_channels", 0))
            max_out = int(d.get("max_output_channels", 0))
            sr = int(d.get("default_samplerate", 16000) or 16000)
            is_usb, is_hat, is_pw = _classify_device_name(name)
            devices.append(
                AudioDeviceInfo(
                    idx,
                    name,
                    max_in > 0,
                    max_out > 0,
                    sr,
                    is_usb,
                    is_hat=is_hat,
                    is_pipewire=is_pw and pw_up,
                    max_input_channels=max_in,
                    max_output_channels=max_out,
                )
            )
        if devices:
            return devices
    except Exception as e:
        log.debug("sounddevice není dostupný, zkusím ALSA: %s", e)

    if platform.system().lower() == "linux":
        if pw_up:
            devices.append(
                AudioDeviceInfo(
                    "default",
                    "PipeWire (default)",
                    True,
                    True,
                    HAT_NATIVE_RATE,
                    False,
                    is_pipewire=True,
                    max_input_channels=2,
                    max_output_channels=2,
                )
            )
        for cmd, is_in in (("arecord", True), ("aplay", False)):
            if not shutil.which(cmd):
                continue
            try:
                out = subprocess.check_output([cmd, "-l"], text=True, stderr=subprocess.DEVNULL)
                for m in re.finditer(r"card\s+(\d+):\s*([^\[]+)\[([^\]]+)\],\s*device\s+(\d+):", out):
                    card, short, desc, dev = m.group(1), m.group(2).strip(), m.group(3).strip(), m.group(4)
                    hw_id = f"plughw:CARD={short},DEV={dev}" if short else f"plughw:{card},{dev}"
                    full_name = f"{desc} ({short}) [{hw_id}]"
                    is_usb, is_hat, _ = _classify_device_name(full_name)
                    sr = HAT_NATIVE_RATE if is_hat else 16000
                    devices.append(
                        AudioDeviceInfo(
                            hw_id,
                            full_name,
                            is_in,
                            not is_in,
                            sr,
                            is_usb,
                            is_hat=is_hat,
                            max_input_channels=HAT_NATIVE_CHANNELS if is_in else 0,
                            max_output_channels=0 if is_in else HAT_NATIVE_CHANNELS,
                        )
                    )
            except Exception as e:
                log.debug("Nelze vypsat %s -l: %s", cmd, e)
    return devices


def select_best_device(
    devices: list[AudioDeviceInfo],
    want_input: bool,
    configured: str | int | None = "auto",
    backend: str = "auto",
) -> AudioDeviceInfo | None:
    """Vybere nejlepší mikrofon nebo reproduktor.

    Pořadí v režimu `auto`: PipeWire (`pipewire` > `pulse` > `default`) -> Audio Injector Zero HAT
    -> USB zařízení -> cokoliv mimo HDMI. `backend="alsa"` obchází PipeWire a míří přímo na HAT.
    """
    candidates = [d for d in devices if (d.is_input if want_input else d.is_output)]
    if not candidates:
        return None
    if configured is not None and str(configured).lower() not in ("auto", ""):
        target = str(configured).lower()
        for d in candidates:
            if str(d.index).lower() == target or target in d.name.lower():
                return d
    backend = (backend or "auto").lower()
    if backend in ("auto", "pipewire"):
        pw = [d for d in candidates if d.is_pipewire]
        if pw:
            order = {n: i for i, n in enumerate(PIPEWIRE_DEVICE_NAMES)}
            pw.sort(
                key=lambda d: order.get(d.name.lower().strip(), 0 if "pipewire" in d.name.lower() else 99)
            )
            return pw[0]
    else:
        candidates = [d for d in candidates if not d.is_pipewire] or candidates
    hat = [d for d in candidates if d.is_hat]
    if hat:
        return hat[0]
    usb = [d for d in candidates if d.is_usb]
    if usb:
        return usb[0]
    non_hdmi = [d for d in candidates if not any(h in d.name.lower() for h in HDMI_KEYWORDS)]
    return non_hdmi[0] if non_hdmi else candidates[0]


def alsa_pcm_name(dev: AudioDeviceInfo | None, alsa_card: str = "audioinjectorpi") -> str | None:
    """Vrátí ALSA PCM jméno pro `arecord` / `aplay -D` (None = výchozí ALSA zařízení)."""
    if dev is None:
        return None
    if isinstance(dev.index, str):
        return dev.index
    if dev.is_pipewire:
        low = dev.name.lower().strip()
        return low if low in PIPEWIRE_DEVICE_NAMES else "default"
    if dev.is_hat:
        return f"plughw:CARD={alsa_card},DEV=0"
    m = re.search(r"\(hw:(\d+),(\d+)\)", dev.name)
    if m:
        return f"plughw:{m.group(1)},{m.group(2)}"
    return None


INPUT_MUX = "Input Mux=Mic"
WM8731_MIXER_ROUTING: tuple[tuple[str, tuple[tuple[str, ...], ...]], ...] = (
    (INPUT_MUX, (("cset", "name=Input Mux", "Mic"), ("sset", "Input Mux", "Mic"))),
    ("Mic zapnutý", (("cset", "name=Mic Capture Switch", "on"), ("sset", "Mic", "cap"))),
    (
        "Mic Boost +20 dB",
        (("cset", "name=Mic Boost Volume", "1"), ("sset", "Mic Boost", "100%"), ("sset", "Mic Boost", "on")),
    ),
    (
        "ADC High Pass Filter",
        (("cset", "name=ADC High Pass Filter Switch", "on"), ("sset", "ADC High Pass Filter", "on")),
    ),
    (
        "Output Mixer HiFi",
        (("cset", "name=Output Mixer HiFi Playback Switch", "on"), ("sset", "Output Mixer HiFi", "on")),
    ),
    (
        "bez příposlechu mikrofonu",
        (
            ("cset", "name=Output Mixer Mic Sidetone Switch", "off"),
            ("sset", "Output Mixer Mic Sidetone", "off"),
        ),
    ),
    (
        "bez Line Bypass",
        (
            ("cset", "name=Output Mixer Line Bypass Switch", "off"),
            ("sset", "Output Mixer Line Bypass", "off"),
        ),
    ),
    (
        "Store DC Offset off",
        (("cset", "name=Store DC Offset Switch", "off"), ("sset", "Store DC Offset", "off")),
    ),
    (
        "Playback Deemphasis off",
        (("cset", "name=Playback Deemphasis Switch", "off"), ("sset", "Playback Deemphasis", "off")),
    ),
    (
        "Master ZC Switch",
        (("cset", "name=Master Playback ZC Switch", "on"), ("sset", "Master Playback ZC", "on")),
    ),
    ("Sidetone 0", (("cset", "name=Sidetone Playback Volume", "0"), ("sset", "Sidetone", "0"))),
)
MIC_BOOST_OFF: tuple[tuple[str, ...], ...] = (
    ("cset", "name=Mic Boost Volume", "0"),
    ("sset", "Mic Boost", "0%"),
    ("sset", "Mic Boost", "off"),
)


def _card_missing(err: str) -> bool:
    return "Invalid card" in err or "No such file" in err or "Cannot open" in err


def _amixer(card: str, variants: Iterable[tuple[str, ...]], timeout: float = 3.0) -> tuple[bool, str]:
    """Zkusí varianty příkazu `amixer -c card …`; vrátí (povedlo se, poslední chybová hláška)."""
    err = ""
    for args in variants:
        try:
            res = subprocess.run(
                ["amixer", "-q", "-c", card, *args],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
            )
        except Exception as e:
            err = str(e)
            continue
        if res.returncode == 0:
            return True, ""
        err = (res.stderr or "").strip() or f"návratový kód {res.returncode}"
        if _card_missing(err):
            break
    return False, err


def reduce_mic_gain(alsa_card: str = "audioinjectorpi") -> bool:
    """Přebuzený mikrofon: vypne Mic Boost (−20 dB). Chybějící hlasitost dorovná AGC bez zkreslení."""
    if platform.system().lower() != "linux" or not shutil.which("amixer"):
        return False
    ok, err = _amixer(alsa_card, MIC_BOOST_OFF, timeout=2.0)
    if ok:
        log.warning("Mikrofon byl přebuzený – vypínám Mic Boost (−20 dB), řeč se dorovná digitálně.")
    else:
        log.debug("Mic Boost nelze vypnout: %s", err)
    return ok


HAT_MAX_CLEAN_DB = 0


def set_output_volume(alsa_card: str = "audioinjectorpi", percent: int = 100) -> None:
    """Nastaví hlasitost výstupu na `percent` % (sluchátkový výstup HATu pohání reproduktor přímo).

    Nastaví ALSA `Master` na HATu i výchozí PipeWire sink (`wpctl`), aby výsledná hlasitost
    nebyla omezena žádnou z vrstev. `percent <= 0` = nic neměnit.

    100 % = nejhlasitější ČISTÝ zvuk: `Master` na 0 dB (ne +6 dB, kde se plný signál ořezává).
    Hlasitost navíc dodá zpracování řeči (kompresor + limiter) bez zkreslení.
    """
    if percent <= 0 or platform.system().lower() != "linux":
        return
    percent = min(100, percent)
    level = f"{HAT_MAX_CLEAN_DB}dB" if percent >= 95 else f"{percent}%"
    if shutil.which("amixer"):
        for control in ("Master", "Headphone"):
            ok, err = _amixer(alsa_card, (("sset", control, level),))
            if not ok:
                log.debug("amixer %s=%s: %s", control, level, err)
                if _card_missing(err):
                    break
                continue
            _amixer(alsa_card, (("sset", control, "unmute"),))
    if shutil.which("wpctl") and pipewire_running():
        try:
            subprocess.run(
                ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{percent / 100:.2f}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3.0,
            )
            subprocess.run(
                ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "0"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3.0,
            )
        except Exception as e:
            log.debug("wpctl selhal: %s", e)
    log.info(
        "Hlasitost výstupu nastavena na %s.",
        "maximum bez zkreslení (Master 0 dB)" if percent >= 95 else f"{percent} %",
    )


def ensure_wm8731_mixer(alsa_card: str = "audioinjectorpi", output_volume_pct: int = 0) -> bool:
    """Nastaví směrování mixéru WM8731 (Input Mux = Mic, bez sidetone/bypass), pokud je HAT přítomen.

    Idempotentní a bezpečné: chybějící karta nebo prvek se jen zaloguje. Vrací True, pokud
    se podařilo nastavit alespoň `Input Mux`. Při `output_volume_pct > 0` nastaví i hlasitost výstupu.
    """
    if platform.system().lower() != "linux" or not shutil.which("amixer"):
        return False
    ok_mux = False
    failed: list[str] = []
    for label, variants in WM8731_MIXER_ROUTING:
        ok, err = _amixer(alsa_card, variants)
        if ok:
            ok_mux = ok_mux or label == INPUT_MUX
            continue
        if _card_missing(err):
            log.debug("Karta %s nenalezena – přeskakuji nastavení mixéru WM8731.", alsa_card)
            return False
        failed.append(label)
        log.debug("amixer %s: %s", label, err)
    if ok_mux:
        log.info(
            "Mixér WM8731 (%s): mikrofon zapnutý (+20 dB), bez příposlechu, výstup HiFi bez de-emphasis.%s",
            alsa_card,
            f" Nepodařilo se nastavit: {', '.join(failed)}." if failed else "",
        )
    set_output_volume(alsa_card, output_volume_pct)
    return ok_mux


class VadDetector:
    """Kombinuje WebRTC VAD s 85 Hz DC/brum filtrem, adaptivním prahem šumu a Near-Field filtrem."""

    def __init__(self, cfg: RpiConfig):
        self.cfg = cfg
        self.noise_floor: float = cfg.min_rms_threshold * 0.65
        self.near_field_peak_rms: float = 0.0
        self._webrtc = None
        try:
            import webrtcvad  # type: ignore[import-untyped]

            self._webrtc = webrtcvad.Vad(max(0, min(3, cfg.vad_aggressiveness)))
        except ImportError:
            pass

    def _filter_frame(self, pcm_frame: bytes, sample_rate: int = 16000) -> tuple[bytes, float]:
        """Odstraní DC posun a nízkofrekvenční brum (< 85 Hz) uvnitř 30ms rámce pro přesné VAD.

        Používá bezstavovou lokální inicializaci (bez přelévání energie z konce řeči do ticha).
        """
        samples = pcm16_samples(pcm_frame)
        n = len(samples)
        if n == 0:
            return pcm_frame, 0.0
        mean = sum(samples) / n
        alpha = _alpha_hp(85.0, sample_rate)
        px = samples[0] - mean
        py = 0.0
        out = array.array("h")
        acc_hp = 0.0
        acc_ac = 0.0
        for s in samples:
            ac = s - mean
            acc_ac += ac * ac
            py = alpha * (py + ac - px)
            px = ac
            iv = max(-32767, min(32767, round(py)))
            out.append(iv)
            acc_hp += iv * iv
        hp_rms = math.sqrt(acc_hp / n)
        ac_rms = math.sqrt(acc_ac / n)
        return out.tobytes(), min(hp_rms, ac_rms)

    def calibrate_frame(self, pcm_frame: bytes, sample_rate: int = 16000) -> None:
        """Průběžně upravuje odhad klidového šumu místnosti z tichých rámců (bez zamrznutí na nízkém prahu)."""
        _, rms = self._filter_frame(pcm_frame, sample_rate)
        if rms <= 0:
            return
        if rms < self.noise_floor:
            self.noise_floor = 0.80 * self.noise_floor + 0.20 * rms
        elif rms < self.noise_floor * 1.6:
            self.noise_floor = 0.96 * self.noise_floor + 0.04 * rms
        elif rms < self.cfg.min_rms_threshold * 2.5:
            self.noise_floor = 0.94 * self.noise_floor + 0.06 * rms

    @property
    def speech_rms_threshold(self) -> float:
        base = max(self.cfg.min_rms_threshold, self.noise_floor * self.cfg.energy_multiplier)
        if self.cfg.near_field_tv_rejection and self.near_field_peak_rms > base * 2.2:
            near_gate = min(
                self.near_field_peak_rms * self.cfg.near_field_drop_ratio,
                max(base * 3.0, 550.0),
            )
            return max(base, near_gate)
        return base

    def is_speech(self, pcm_frame: bytes, sample_rate: int = 16000) -> tuple[bool, float]:
        """Rozhodne, zda 30ms rámec obsahuje blízkou lidskou řeč, a vrátí (is_speech, rms)."""
        vad_pcm, rms = self._filter_frame(pcm_frame, sample_rate)
        threshold = self.speech_rms_threshold
        if rms < threshold:
            if rms > 0 and rms < self.noise_floor:
                self.noise_floor = 0.80 * self.noise_floor + 0.20 * rms
            elif rms > 0 and rms < self.noise_floor * 1.6:
                self.noise_floor = 0.96 * self.noise_floor + 0.04 * rms
            elif rms > 0 and rms < self.cfg.min_rms_threshold * 2.5:
                self.noise_floor = 0.94 * self.noise_floor + 0.06 * rms
            if self.near_field_peak_rms > 0.0:
                self.near_field_peak_rms *= 0.985
            return False, rms

        if self.cfg.mic_transient_filter and is_transient_pop_frame(vad_pcm):
            return False, rms

        detected = False
        if self._webrtc is not None and len(vad_pcm) in (320, 640, 960):
            try:
                if self._webrtc.is_speech(vad_pcm, sample_rate) or rms >= threshold * 1.45:
                    detected = True
            except Exception:
                pass
        else:
            zcr = pcm16_zcr(vad_pcm)
            if 0.01 <= zcr <= 0.55 or rms >= threshold * 1.6:
                detected = True

        if detected:
            base = max(self.cfg.min_rms_threshold, self.noise_floor * self.cfg.energy_multiplier)
            capped_rms = min(rms, max(base * 12.0, 3200.0))
            if capped_rms > self.near_field_peak_rms:
                self.near_field_peak_rms = (
                    0.60 * self.near_field_peak_rms + 0.40 * capped_rms
                    if self.near_field_peak_rms
                    else capped_rms
                )
            return True, rms
        return False, rms


PauseCheckResult = tuple[str, str, str]


class _PausePending:
    """Návratová hodnota `on_pause_check`: přepis běží na pozadí, výsledek přinese `poll_pause_check`."""

    def __repr__(self) -> str:
        return "PAUSE_PENDING"


PAUSE_PENDING = _PausePending()
_last_clip_warning = 0.0
_mic_gain_reduced = False


def process_vad_stream(
    frames: Iterable[bytes],
    cfg: RpiConfig,
    *,
    is_button_held: Callable[[], bool] | None = None,
    is_canceled: Callable[[], bool] | None = None,
    finish_early: Callable[[], bool] | None = None,
    on_button_pressed_during_followup: Callable[[], bool] | None = None,
    on_pause_check: Callable[[bytes], PauseCheckResult | _PausePending | None] | None = None,
    poll_pause_check: Callable[[], PauseCheckResult | None] | None = None,
    on_pause_cancel: Callable[[], None] | None = None,
    is_speaker_active: Callable[[], bool] | None = None,
    is_followup: bool = False,
) -> RecordedUtterance:
    """Zpracuje proud 30ms PCM rámců s adaptivní délkou ticha a průběžnou kontrolou ukončení věty.

    Průběžný přepis: `on_pause_check(pcm)` může vrátit výsledek hned, nebo `PAUSE_PENDING` – pak
    nahrávání NEČEKÁ, běží dál a každý další tichý rámec se zeptá `poll_pause_check()`. Když senior
    znovu promluví, rozpracovaný přepis se zahodí (`on_pause_cancel`).
    """
    global _last_clip_warning, _mic_gain_reduced
    vad = VadDetector(cfg)
    frame_s = cfg.frame_ms / 1000.0
    pre_roll_len = max(1, int(round(cfg.pre_roll_ms / cfg.frame_ms)))
    min_speech_frames = max(1, int(round(cfg.min_speech_s / frame_s)))
    if is_followup:
        min_speech_frames = min(min_speech_frames, max(3, int(round(0.15 / frame_s))))
    max_frames = max(10, int(round(cfg.max_record_s / frame_s)))
    start_timeout_s = (
        min(cfg.start_timeout_s, getattr(cfg, "followup_start_timeout_s", cfg.start_timeout_s))
        if is_followup
        else cfg.start_timeout_s
    )
    min_listen_frames = 0 if is_followup else max(0, math.ceil(getattr(cfg, "min_listen_s", 4.0) / frame_s))
    start_timeout_frames = max(5, int(round(start_timeout_s / frame_s)))
    if not is_followup:
        start_timeout_frames = max(start_timeout_frames, min_listen_frames)

    base_tail_s = cfg.followup_silence_tail_s if is_followup else cfg.silence_tail_s
    silence_tail_frames = max(3, int(round(base_tail_s / frame_s)))
    short_burst_tail_frames = max(3, int(round(cfg.short_burst_silence_tail_s / frame_s)))
    rolling_check_frames = max(4, int(round(cfg.rolling_stt_pause_s / frame_s)))
    likely_tail_frames = max(3, int(round(getattr(cfg, "likely_complete_tail_s", 1.3) / frame_s)))
    trim_keep_frames = max(0, int(round(getattr(cfg, "speech_trim_tail_ms", 300) / cfg.frame_ms)))

    pre_roll: collections.deque[bytes] = collections.deque(maxlen=pre_roll_len)
    recorded: list[bytes] = []
    speech_started = False
    speech_frames = 0
    silence_frames = 0
    frames_since_start = 0
    listen_frames = 0
    peak_rms = 0.0
    ever_held = False
    pause_checked_at_speech_frames = -1
    pause_pending = False
    pause_complete = False
    pause_live = False
    last_speech_len = 0
    cached_transcript = ""
    cached_lang = "cs"

    calibration_frames = 4

    def _apply_pause_result(res) -> bool:  # noqa: ANN001
        """Zpracuje výsledek průběžného přepisu. Vrací True = věta je kompletní."""
        nonlocal cached_transcript, cached_lang, silence_tail_frames, pause_complete
        if len(res) == 2:
            decision, tr_text = res  # type: ignore[misc]
            tr_lang = "cs"
        else:
            decision, tr_text, tr_lang = res
        if tr_text:
            cached_transcript, cached_lang = tr_text, tr_lang
        if decision == "complete" and tr_text:
            pause_complete = True
            return True
        if decision == "hesitation":
            silence_tail_frames = max(
                silence_tail_frames, int(round(cfg.hesitation_silence_tail_s / frame_s))
            )
        elif decision == "likely_complete" and tr_text:
            silence_tail_frames = min(silence_tail_frames, likely_tail_frames)
        return False

    for idx, frame in enumerate(frames):
        if is_canceled and is_canceled():
            return RecordedUtterance(
                b"", b"", idx * frame_s, speech_frames * frame_s, peak_rms, canceled=True
            )

        if is_speaker_active and is_speaker_active():
            continue

        listen_frames += 1
        past_min_listen = listen_frames >= min_listen_frames

        if speech_started and speech_frames >= min_speech_frames and finish_early and finish_early():
            break

        if (
            is_followup
            and not speech_started
            and on_button_pressed_during_followup
            and on_button_pressed_during_followup()
        ):
            is_followup = False
            silence_tail_frames = max(3, int(round(cfg.silence_tail_s / frame_s)))
            min_listen_frames = max(0, math.ceil(getattr(cfg, "min_listen_s", 4.0) / frame_s))
            start_timeout_frames = max(start_timeout_frames, min_listen_frames)
            frames_since_start = 0
            past_min_listen = listen_frames >= min_listen_frames

        held = bool(is_button_held and is_button_held())
        if held:
            ever_held = True

        if idx < calibration_frames and not held:
            vad.calibrate_frame(frame)

        speaking, rms = vad.is_speech(frame, cfg.sample_rate)
        if rms > peak_rms:
            peak_rms = rms

        if not speech_started:
            pre_roll.append(frame)
            if speaking:
                speech_started = True
                speech_frames = 1
                silence_frames = 0
                recorded.extend(pre_roll)
                pre_roll.clear()
                last_speech_len = len(recorded)
            elif held:
                frames_since_start = 0
            else:
                frames_since_start += 1
                if frames_since_start >= start_timeout_frames:
                    if ever_held and len(pre_roll) > 0:
                        raw = b"".join(pre_roll)
                        return RecordedUtterance(
                            pcm_to_wav(raw, cfg.sample_rate),
                            raw,
                            len(pre_roll) * frame_s,
                            len(pre_roll) * frame_s,
                            peak_rms,
                        )
                    return RecordedUtterance(
                        b"", b"", frames_since_start * frame_s, 0.0, peak_rms, timed_out=True
                    )
        else:
            recorded.append(frame)
            if speaking:
                speech_frames += 1
                silence_frames = 0
                last_speech_len = len(recorded)
                cached_transcript = ""
                pause_pending = False
                pause_complete = False
                if pause_live:
                    pause_live = False
                    if on_pause_cancel is not None:
                        on_pause_cancel()
            else:
                silence_frames += 1
                if silence_frames <= 8 and rms >= max(vad.noise_floor * 2.2, cfg.min_rms_threshold * 0.55):
                    last_speech_len = len(recorded)

                speech_dur = speech_frames * frame_s
                if (
                    not held
                    and is_followup
                    and speech_frames >= min_speech_frames
                    and speech_dur <= 1.05
                    and silence_frames >= short_burst_tail_frames
                    and past_min_listen
                ):
                    break

                if (
                    on_pause_check is not None
                    and not held
                    and speech_frames >= min_speech_frames
                    and silence_frames == rolling_check_frames
                    and pause_checked_at_speech_frames != speech_frames
                ):
                    pause_checked_at_speech_frames = speech_frames
                    res = on_pause_check(b"".join(recorded[: last_speech_len + trim_keep_frames]))
                    pause_live = res is not None
                    if res is PAUSE_PENDING:
                        pause_pending = True
                    elif res is not None and _apply_pause_result(res) and past_min_listen:
                        break
                elif pause_pending and poll_pause_check is not None and not held:
                    res = poll_pause_check()
                    if res is not None:
                        pause_pending = False
                        if _apply_pause_result(res) and past_min_listen:
                            break

            if (
                not held
                and speech_frames >= min_speech_frames
                and past_min_listen
                and (silence_frames >= silence_tail_frames or pause_complete)
            ):
                break

            if not held and speech_frames < min_speech_frames and silence_frames >= int(1.0 / frame_s):
                speech_started = False
                speech_frames = 0
                silence_frames = 0
                last_speech_len = 0
                for f in recorded[-pre_roll_len:]:
                    pre_roll.append(f)
                recorded.clear()

        if len(recorded) >= max_frames:
            break

    if not recorded and ever_held and pre_roll:
        recorded.extend(pre_roll)
        speech_frames = max(speech_frames, len(recorded))

    if speech_frames < min_speech_frames and not ever_held:
        return RecordedUtterance(b"", b"", len(recorded) * frame_s, 0.0, peak_rms, timed_out=True)

    pcm_data = b"".join(recorded)
    if last_speech_len > 0:
        speech_end_bytes = min(
            len(pcm_data), sum(len(f) for f in recorded[: last_speech_len + trim_keep_frames])
        )
    else:
        speech_end_bytes = len(pcm_data)
    clip = clipped_ratio(pcm_data)
    if clip > 0.002 and time.monotonic() - _last_clip_warning > 60.0:
        _last_clip_warning = time.monotonic()
        log.warning(
            "Mikrofon je přebuzený (%.1f %% vzorků na maximu) – řeč je zkreslená ještě před "
            "zpracováním. Mluvte dál od mikrofonu, nebo vypněte `Mic Boost` v "
            "`alsamixer -c audioinjectorpi`.",
            clip * 100,
        )
        if clip > 0.015 and not _mic_gain_reduced:
            _mic_gain_reduced = reduce_mic_gain(getattr(cfg, "alsa_card", "audioinjectorpi"))
    if cfg.mic_bandpass_filter or cfg.mic_agc_enabled or cfg.mic_transient_filter:
        pcm_data = apply_mic_speech_cleanup_pcm16(
            pcm_data,
            rate=cfg.sample_rate,
            bandpass=cfg.mic_bandpass_filter,
            transient_filter=cfg.mic_transient_filter,
            noise_subtraction=cfg.mic_noise_subtraction,
            noise_floor_rms=vad.noise_floor,
            agc_enabled=cfg.mic_agc_enabled,
            target_rms=cfg.mic_agc_target_rms,
            max_gain=cfg.mic_agc_max_gain,
        )
    wav_data = pcm_to_wav(pcm_data, cfg.sample_rate)
    return RecordedUtterance(
        wav_bytes=wav_data,
        pcm_bytes=pcm_data,
        duration_s=len(recorded) * frame_s,
        speech_duration_s=speech_frames * frame_s,
        peak_rms=peak_rms,
        pre_transcribed_text=cached_transcript,
        pre_transcribed_lang=cached_lang,
        pre_transcription_pending=pause_pending and not cached_transcript,
        speech_end_bytes=speech_end_bytes,
        sample_rate=cfg.sample_rate,
    )


def _stream_format_candidates(
    cfg: RpiConfig, dev: AudioDeviceInfo | None, want_input: bool, preferred_rate: int
) -> list[tuple[int, int]]:
    """Seznam (samplerate, channels) k vyzkoušení – od nejlevnějšího po nativní formát HATu.

    PipeWire zvládne 16 kHz mono přímo; WM8731 přes surové `hw:` vyžaduje 48 kHz / 2 kanály.
    """
    max_ch = (dev.max_input_channels if want_input else dev.max_output_channels) if dev else 2
    max_ch = max(1, max_ch or 1)
    native_rate = HAT_NATIVE_RATE
    if dev and not dev.is_hat and dev.default_samplerate >= 16000:
        native_rate = dev.default_samplerate
    out: list[tuple[int, int]] = []
    if dev is not None and dev.is_hat and (cfg.audio_backend or "auto").lower() == "alsa":
        out.append((HAT_NATIVE_RATE, min(HAT_NATIVE_CHANNELS, max_ch)))
    for rate, ch in (
        (preferred_rate, 1),
        (native_rate, 1),
        (native_rate, min(2, max_ch)),
        (HAT_NATIVE_RATE, min(2, max_ch)),
    ):
        if (rate, ch) not in out:
            out.append((rate, ch))
    return out


class MicrophoneStream:
    """Čte 30ms rámce (16 kHz, 16-bit mono) z mikrofonu HATu (WM8731) / PipeWire / USB s automatickým zotavením.

    Záznam běží přes callback PortAudio do fronty: když je čtecí vlákno chvíli zaneprázdněné
    (průběžný přepis Whisperem během pomlky), žádný zvuk se neztratí. Pokud zařízení přestane
    posílat data (výpadek PipeWire / HATu), čtení nezamrzne – stream se po `STALL_TIMEOUT_S` znovu otevře.
    """

    STALL_TIMEOUT_S = 2.5
    QUEUE_MAX_BLOCKS = 600

    def __init__(self, cfg: RpiConfig):
        self.cfg = cfg
        self.frame_bytes = int(cfg.sample_rate * (cfg.frame_ms / 1000.0)) * 2
        self.active_format: tuple[int, int] | None = None
        self._logged_device: str | None = None

    def iter_frames(self, stop_check: Callable[[], bool] | None = None) -> Iterable[bytes]:
        attempts = 0
        last_err: Exception | None = None
        while not (stop_check and stop_check()):
            devices = list_audio_devices()
            dev = select_best_device(
                devices, want_input=True, configured=self.cfg.mic_device, backend=self.cfg.audio_backend
            )
            dev_label = f"{dev.name} (index={dev.index})" if dev else "výchozí zařízení"
            if dev_label != self._logged_device:
                log.info("Mikrofon: %s", dev_label)
                self._logged_device = dev_label

            sd = None
            try:
                import sounddevice as sd  # type: ignore[no-redef]
            except Exception as e:
                log.debug("sounddevice nelze použít: %s", e)

            if sd is not None:
                try:
                    yield from self._iter_sounddevice(sd, dev, stop_check)
                    return
                except Exception as e:
                    last_err = e
                    log.warning("Výpadek mikrofonu přes sounddevice (%s), zkouším zotavení…", e)

            if shutil.which("arecord"):
                try:
                    yield from self._iter_arecord(dev, stop_check)
                    return
                except Exception as e:
                    last_err = e
                    log.warning("Výpadek arecord (%s), čekám na znovupřipojení mikrofonu…", e)

            attempts += 1
            if attempts >= 3:
                raise RuntimeError(
                    "Nebyl nalezen žádný funkční zvukový vstup (zkontrolujte Audio Injector Zero HAT: "
                    "`arecord -l`, overlay `audioinjector-wm8731-audio` a `Input Mux = Mic` v alsamixeru)"
                    + (f": {last_err}" if last_err else ".")
                )
            time.sleep(self.cfg.usb_hotplug_retry_s)

    def _open_input_stream(self, sd, dev: AudioDeviceInfo | None, callback=None):
        """Otevře vstupní stream v prvním formátu, který zařízení přijme. Vrací (stream, rate, channels, blocksize)."""
        dev_idx = dev.index if dev and isinstance(dev.index, int) else None
        last_err: Exception | None = None
        for rate, channels in _stream_format_candidates(self.cfg, dev, True, self.cfg.sample_rate):
            blocksize = int(rate * (self.cfg.frame_ms / 1000.0))
            try:
                kwargs = dict(
                    samplerate=rate, blocksize=blocksize, device=dev_idx, channels=channels, dtype="int16"
                )
                if callback is not None:
                    kwargs["callback"] = callback
                stream = sd.RawInputStream(**kwargs)
                return stream, rate, channels, blocksize
            except Exception as e:
                last_err = e
                log.debug("Mikrofon odmítl %d Hz / %d kan.: %s", rate, channels, e)
        raise last_err or RuntimeError("Nelze otevřít mikrofon")

    def _iter_sounddevice(
        self, sd, dev: AudioDeviceInfo | None, stop_check: Callable[[], bool] | None
    ) -> Iterable[bytes]:
        blocks: queue.Queue[bytes] = queue.Queue(maxsize=self.QUEUE_MAX_BLOCKS)
        dropped = 0

        def _callback(indata, _frames, _time_info, _status) -> None:
            nonlocal dropped
            try:
                blocks.put_nowait(bytes(indata))
            except queue.Full:
                dropped += 1

        stream, hw_rate, channels, _blocksize = self._open_input_stream(sd, dev, callback=_callback)
        if self.active_format != (hw_rate, channels):
            log.info(
                "Mikrofon otevřen: %d Hz / %d kan. -> %d Hz mono", hw_rate, channels, self.cfg.sample_rate
            )
        self.active_format = (hw_rate, channels)

        decimator = (
            Fir3Decimator() if _np is not None and hw_rate == 3 * self.cfg.sample_rate == 48000 else None
        )
        with stream:
            buf = bytearray()
            while not (stop_check and stop_check()):
                try:
                    chunk = blocks.get(timeout=self.STALL_TIMEOUT_S)
                except queue.Empty:
                    raise RuntimeError(f"mikrofon {self.STALL_TIMEOUT_S:.1f} s neposlal žádná data") from None
                if channels > 1:
                    chunk = downmix_to_mono_pcm16(chunk, channels)
                if decimator is not None:
                    chunk = decimator.process(chunk)
                elif hw_rate != self.cfg.sample_rate:
                    chunk = resample_pcm16_mono(chunk, hw_rate, self.cfg.sample_rate)
                buf.extend(chunk)
                while len(buf) >= self.frame_bytes:
                    frame = bytes(buf[: self.frame_bytes])
                    del buf[: self.frame_bytes]
                    yield frame
        if dropped:
            log.debug("Mikrofon: zahozeno %d bloků (přetečení fronty).", dropped)

    def _iter_arecord(
        self, dev: AudioDeviceInfo | None, stop_check: Callable[[], bool] | None
    ) -> Iterable[bytes]:
        cmd = ["arecord", "-q", "-t", "raw", "-f", "S16_LE", "-c", "1", "-r", str(self.cfg.sample_rate)]
        pcm = alsa_pcm_name(dev, self.cfg.alsa_card)
        if pcm:
            cmd.extend(["-D", pcm])
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stopped = False
        try:
            assert proc.stdout is not None
            while True:
                if stop_check and stop_check():
                    stopped = True
                    break
                frame = proc.stdout.read(self.frame_bytes)
                if not frame or len(frame) < self.frame_bytes:
                    break
                yield frame
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        if not stopped and not (stop_check and stop_check()):
            err = b""
            try:
                err = proc.stderr.read() if proc.stderr else b""
            except Exception:
                pass
            raise RuntimeError(
                f"arecord skončil (kód {proc.returncode}): {err.decode(errors='ignore').strip()[-200:]}"
            )


class AudioPlayer:
    """Přehrává WAV na výstupu HATu (PipeWire / ALSA) se Senior Clarity DSP a okamžitým přerušením.

    Přehrávání je serializované (nikdy nehrají dva zvuky přes sebe) a `stop()` zruší
    jak právě hrající zvuk, tak zvuky, které na přehrání teprve čekají.
    """

    def __init__(self, cfg: RpiConfig):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._play_lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._playing = False
        self._last_play_ended_at: float = 0.0
        self._stop_event = threading.Event()
        self._generation = 0
        self._aplay_broken_until = 0.0
        self.last_error: str | None = None

    @property
    def is_playing(self) -> bool:
        return self._playing

    def is_ducking_active(self, echo_guard_s: float | None = None) -> bool:
        """Vrací True, pokud reproduktor právě hraje nebo ještě neuplynula ochranná doba dozvuku místnosti."""
        if self._playing:
            return True
        guard = self.cfg.echo_guard_s if echo_guard_s is None else echo_guard_s
        if guard > 0 and self._last_play_ended_at > 0:
            return (time.monotonic() - self._last_play_ended_at) < guard
        return False

    def stop(self) -> None:
        """Okamžitě zastaví právě hrající řeč nebo tón (< 50 ms) a zahodí zvuky čekající ve frontě."""
        self._generation += 1
        self._stop_event.set()
        with self._lock:
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=0.3)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            self._proc = None
        if platform.system().lower() == "windows":
            try:
                import winsound

                winsound.PlaySound(None, winsound.SND_PURGE)
            except Exception:
                pass

    def play_wav_bytes(
        self, wav_bytes: bytes, *, apply_clarity: bool = False, extra_gain: float = 1.0
    ) -> None:
        """Synchronně přehraje WAV bajty (volitelně se Senior Clarity DSP ekvalizérem)."""
        if not wav_bytes:
            return
        generation = self._generation
        loud = bool(getattr(self.cfg, "output_loudness", True))
        if apply_clarity and (self.cfg.senior_clarity_dsp or loud or extra_gain != 1.0):
            wav_bytes = apply_senior_clarity_dsp_wav(
                wav_bytes,
                presence_gain=self.cfg.clarity_presence_gain,
                extra_gain=extra_gain,
                eq=self.cfg.senior_clarity_dsp,
                loudness=loud,
            )
        with self._play_lock:
            if generation != self._generation:
                return
            self._stop_event.clear()
            self._playing = True
            try:
                self._play_any(wav_bytes)
                self.last_error = None
            except Exception as e:
                self.last_error = str(e)
                log.warning("Přehrávání zvuku selhalo: %s", e)
            finally:
                self._playing = False
                self._last_play_ended_at = time.monotonic()

    def _play_any(self, wav_bytes: bytes) -> None:
        is_linux = platform.system().lower() == "linux"
        errors: list[str] = []
        if is_linux and shutil.which("aplay") and time.monotonic() >= self._aplay_broken_until:
            try:
                if self._play_via_aplay(wav_bytes):
                    return
                errors.append("aplay selhal")
            except Exception as e:
                errors.append(f"aplay: {e}")
            if self._stop_event.is_set():
                return
            self._aplay_broken_until = time.monotonic() + 30.0
        sd = None
        try:
            import sounddevice as sd  # type: ignore[no-redef]
        except Exception as e:
            errors.append(f"sounddevice: {e}")
        if sd is not None:
            try:
                self._play_via_sounddevice(sd, wav_bytes)
                return
            except Exception as e:
                errors.append(f"sounddevice: {e}")
        if self._stop_event.is_set():
            return
        if platform.system().lower() == "windows":
            self._play_via_winsound(wav_bytes)
            return
        if shutil.which("ffplay"):
            if self._play_via_cmd(["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"], wav_bytes):
                return
            errors.append("ffplay selhal")
        raise RuntimeError("; ".join(errors) or "žádný přehrávač zvuku není k dispozici")

    def play_tone(self, kind: str) -> None:
        wav = synthesize_tone_wav(kind)
        if wav:
            self.play_wav_bytes(wav, apply_clarity=False)

    def _select_output(self) -> AudioDeviceInfo | None:
        devices = list_audio_devices()
        return select_best_device(
            devices,
            want_input=False,
            configured=self.cfg.speaker_device,
            backend=self.cfg.audio_backend,
        )

    @staticmethod
    def _apply_edge_fade_pcm16(
        pcm: bytes, rate: int, fade_in_ms: float = 4.0, fade_out_ms: float = 8.0
    ) -> bytes:
        samples = pcm16_samples(pcm)
        n = len(samples)
        if n < 8:
            return pcm
        out = array.array("h", samples)
        fi = min(n // 2, max(1, int(rate * fade_in_ms / 1000.0)))
        fo = min(n // 2, max(1, int(rate * fade_out_ms / 1000.0)))
        for i in range(fi):
            gain = 0.5 * (1.0 - math.cos(math.pi * (i + 1) / fi))
            out[i] = round(out[i] * gain)
        for i in range(fo):
            gain = 0.5 * (1.0 - math.cos(math.pi * (i + 1) / fo))
            idx = n - 1 - i
            out[idx] = round(out[idx] * gain)
        return out.tobytes()

    @classmethod
    def _prepare_output_wav(
        cls,
        wav_bytes: bytes,
        *,
        target_rate: int = HAT_NATIVE_RATE,
        target_channels: int = 1,
        tail_silence_ms: int = 35,
    ) -> bytes:
        """Připraví čistý WAV pro výstup (fade hran, převzorkování bez zrnění, tichý doběh proti lupnutí)."""
        if not wav_bytes or len(wav_bytes) <= 44:
            return wav_bytes
        try:
            with wave.open(io.BytesIO(wav_bytes), "rb") as w:
                channels = w.getnchannels()
                width = w.getsampwidth()
                rate = w.getframerate()
                raw = w.readframes(w.getnframes())
            if width != 2 or not raw:
                return wav_bytes
            if channels > 1:
                raw = downmix_to_mono_pcm16(raw, channels)
            raw = cls._apply_edge_fade_pcm16(raw, rate)
            if target_rate > 0 and rate != target_rate:
                raw = resample_pcm16_hq(raw, rate, target_rate)
                rate = target_rate
            if tail_silence_ms > 0:
                tail_samples = max(0, int(rate * tail_silence_ms / 1000.0))
                if tail_samples:
                    raw += b"\x00" * (tail_samples * 2)
            out_ch = 2 if target_channels == 2 else 1
            if out_ch == 2:
                raw = mono_to_stereo_pcm16(raw)
            return pcm_to_wav(raw, rate=rate, channels=out_ch, width=2)
        except Exception as e:
            log.debug("Příprava výstupního WAV přeskočena: %s", e)
            return wav_bytes

    def _play_via_aplay(self, wav_bytes: bytes) -> bool:
        dev = self._select_output()
        cmd = ["aplay", "-q"]
        pcm = alsa_pcm_name(dev, self.cfg.alsa_card)
        if pcm:
            cmd.extend(["-D", pcm])
        target_ch = 2 if (pcm and pcm.startswith("hw:")) else 1
        prepared = self._prepare_output_wav(
            wav_bytes,
            target_rate=HAT_NATIVE_RATE,
            target_channels=target_ch,
            tail_silence_ms=35,
        )
        return self._play_via_cmd(cmd, prepared)

    def _play_via_cmd(self, cmd: list[str], wav_bytes: bytes) -> bool:
        """Přehraje přes externí přehrávač. Vrací True při úspěchu nebo při záměrném zastavení."""
        shm_dir = "/dev/shm" if Path("/dev/shm").is_dir() and os.access("/dev/shm", os.W_OK) else None
        with tempfile.NamedTemporaryFile(suffix=".wav", dir=shm_dir, delete=False) as f:
            f.write(wav_bytes)
            tmp_path = Path(f.name)
        proc: subprocess.Popen | None = None
        try:
            with self._lock:
                if self._stop_event.is_set():
                    return True
                proc = subprocess.Popen(
                    cmd + [str(tmp_path)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
                )
                self._proc = proc
            while proc.poll() is None:
                if self._stop_event.is_set():
                    self.stop()
                    return True
                time.sleep(0.02)
            if self._stop_event.is_set():
                return True
            if proc.returncode != 0:
                err = b""
                try:
                    err = proc.stderr.read() if proc.stderr else b""
                except Exception:
                    pass
                log.warning(
                    "%s skončil s chybou %s: %s",
                    cmd[0],
                    proc.returncode,
                    err.decode(errors="ignore").strip()[-200:],
                )
                return False
            return True
        finally:
            with self._lock:
                if self._proc is proc:
                    self._proc = None
            if proc is not None and proc.stderr:
                try:
                    proc.stderr.close()
                except Exception:
                    pass
            tmp_path.unlink(missing_ok=True)

    def _play_via_sounddevice(self, sd, wav_bytes: bytes) -> None:
        dev = self._select_output()
        dev_idx = dev.index if dev and isinstance(dev.index, int) else None

        with wave.open(io.BytesIO(wav_bytes), "rb") as w:
            rate = w.getframerate()
            channels = w.getnchannels()
            width = w.getsampwidth()
            raw = w.readframes(w.getnframes())
        if width != 2:
            raise RuntimeError(f"nepodporovaná bitová hloubka WAV: {width * 8} bit")
        if channels > 1:
            raw = downmix_to_mono_pcm16(raw, channels)
        raw = self._apply_edge_fade_pcm16(raw, rate)

        stream = None
        target_rate, out_channels = rate, 1
        last_err: Exception | None = None
        for cand_rate, cand_ch in _stream_format_candidates(self.cfg, dev, False, rate):
            try:
                stream = sd.RawOutputStream(
                    samplerate=cand_rate, device=dev_idx, channels=cand_ch, dtype="int16"
                )
                target_rate, out_channels = cand_rate, cand_ch
                break
            except Exception as e:
                last_err = e
                log.debug("Reproduktor odmítl %d Hz / %d kan.: %s", cand_rate, cand_ch, e)
        if stream is None:
            raise last_err or RuntimeError("Nelze otevřít reproduktor")
        if target_rate != rate:
            raw = resample_pcm16_hq(raw, rate, target_rate)
        tail_samples = max(0, int(target_rate * 0.035))
        if tail_samples:
            raw += b"\x00" * (tail_samples * 2)
        if out_channels == 2:
            raw = mono_to_stereo_pcm16(raw)

        frame_bytes = 2 * out_channels
        chunk_bytes = int(target_rate * 0.04) * frame_bytes
        with stream:
            for offset in range(0, len(raw), chunk_bytes):
                if self._stop_event.is_set():
                    stream.abort()
                    break
                stream.write(raw[offset : offset + chunk_bytes])

    def _play_via_winsound(self, wav_bytes: bytes) -> None:
        import winsound

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(wav_bytes)
            tmp_path = Path(f.name)
        try:
            with wave.open(io.BytesIO(wav_bytes), "rb") as w:
                dur_s = w.getnframes() / max(1, w.getframerate())
            winsound.PlaySound(str(tmp_path), winsound.SND_FILENAME | winsound.SND_ASYNC)
            t0 = time.monotonic()
            while time.monotonic() - t0 < dur_s:
                if self._stop_event.is_set():
                    winsound.PlaySound(None, winsound.SND_PURGE)
                    break
                time.sleep(0.03)
        finally:
            winsound.PlaySound(None, winsound.SND_PURGE)
            tmp_path.unlink(missing_ok=True)
