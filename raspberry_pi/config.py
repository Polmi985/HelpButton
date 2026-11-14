"""Konfigurace pro Raspberry Pi 5 hlasové tlačítko (rpi_button.toml / CLI / ENV)."""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger("helpbutton.rpi.config")

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_DIR = PACKAGE_DIR.parent
DEFAULT_SERVER_PORT = 8765


@dataclass
class RpiConfig:
    server_url: str = "auto"
    token: str = ""
    standalone_fallback: bool = True

    gpio_pin: int = 17
    led_pin: int | None = 27
    led_pwm_breathing: bool = True
    enable_gpio: bool = True
    enable_keyboard: bool = True

    audio_backend: str = "auto"
    alsa_card: str = "audioinjectorpi"
    output_volume_pct: int = 92
    output_loudness: bool = True
    hat_mixer_init: bool = True
    mic_device: str | int | None = "auto"
    speaker_device: str | int | None = "auto"
    sample_rate: int = 16000
    frame_ms: int = 30
    usb_hotplug_retry_s: float = 1.0

    pre_roll_ms: int = 500
    min_listen_s: float = 4.0
    start_timeout_s: float = 7.0
    followup_start_timeout_s: float = 4.0
    silence_tail_s: float = 1.15
    followup_silence_tail_s: float = 0.80
    short_burst_silence_tail_s: float = 0.50
    hesitation_silence_tail_s: float = 1.8
    rolling_stt_pause_s: float = 0.45
    likely_complete_tail_s: float = 0.75
    speech_trim_tail_ms: int = 450
    min_speech_s: float = 0.22
    max_record_s: float = 25.0
    echo_guard_s: float = 0.20
    press_grace_s: float = 0.45
    speak_listening_greeting: bool = False
    listen_beep: bool = True
    auto_listen_followups: bool = True
    max_auto_followups: int = 2
    vad_aggressiveness: int = 2
    energy_multiplier: float = 2.4
    min_rms_threshold: float = 160.0
    near_field_tv_rejection: bool = True
    near_field_drop_ratio: float = 0.22

    mic_bandpass_filter: bool = True
    mic_agc_enabled: bool = True
    mic_agc_target_rms: float = 4800.0
    mic_agc_max_gain: float = 6.0
    mic_transient_filter: bool = True
    mic_noise_subtraction: bool = True

    senior_clarity_dsp: bool = True
    clarity_presence_gain: float = 1.55
    slow_repeat_rate: float = 0.68
    working_heartbeat_s: float = 2.5

    stt_mode: str = "auto"
    whisper_model: str = "base"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    whisper_threads: int = 4
    whisper_beam_size: int = 1
    whisper_language: str = "cs"
    whisper_vad_filter: bool = False
    stt_vocabulary: list[str] = field(default_factory=list)
    fuzzy_senior_repair: bool = True
    rolling_micro_stt: bool = True
    auto_translate_to_cs: bool = True
    bilingual_tts: bool = False

    tts_engine: str = "auto"
    sentence_streaming_tts: bool = True
    edge_voice: str = "cs-CZ-VlastaNeural"
    edge_en_voice: str = "en-US-AriaNeural"
    piper_bin: str = "piper"
    piper_model: str = str(PACKAGE_DIR / "voices" / "cs_CZ-jirka-medium.onnx")
    speech_rate: float = 0.85
    cache_dir: Path = field(default_factory=lambda: PACKAGE_DIR / ".tts_cache")
    prewarm_phrases: bool = True


def auth_headers(token: str) -> dict[str, str]:
    """Hlavička pro API serveru: klíč odvozený z párovacího tokenu (stejně jako helpbutton.common.protocol.api_key)."""
    if not token:
        return {}
    import hashlib
    import hmac

    key = hmac.new(token.encode(), b"helpbutton-api-v1", hashlib.sha256).hexdigest()
    return {"Authorization": f"Bearer {key}"}


def server_proof(token: str, nonce: str) -> str:
    """Podpis, kterým se pravý server prokáže (stejně jako helpbutton.common.protocol.auth_proof s rolí server-api)."""
    import hashlib
    import hmac

    return hmac.new(token.encode(), f"server-api|{nonce}|".encode(), hashlib.sha256).hexdigest()


def normalize_server_url(url: str | None) -> str:
    """Doplní chybějící `http://` a port 8765 („192.168.1.10“ -> „http://192.168.1.10:8765“)."""
    raw = (url or "").strip()
    if not raw or raw.lower() == "auto":
        return "auto"
    if "://" not in raw:
        host = raw.rstrip("/")
        try:
            has_port = urlsplit(f"http://{host}").port is not None
        except ValueError:
            has_port = False
        raw = f"http://{host}" if has_port else f"http://{host}:{DEFAULT_SERVER_PORT}"
    return raw.rstrip("/")


def resolve_data_path(value: str | os.PathLike, *, must_exist: bool = True) -> str:
    """Relativní cesty z TOML (např. `raspberry_pi/voices/...`) vztáhne ke kořeni repozitáře,
    aby nezáleželo na tom, z jaké složky se program spouští (SSH, systemd)."""
    p = Path(os.path.expanduser(str(value)))
    if p.is_absolute():
        return str(p)
    for base in (Path.cwd(), REPO_DIR, PACKAGE_DIR):
        cand = base / p
        if cand.exists():
            return str(cand.resolve())
    return str((REPO_DIR / p).resolve()) if must_exist else str(p)


def _optional_pin(value: object) -> int | None:
    """`led_pin = false` / `-1` / `"none"` = bez LED (TOML nemá null). `false` se nesmí stát GPIO 0."""
    if value is None or value is False:
        return None
    if isinstance(value, str):
        if value.strip().lower() in ("", "none", "off", "false", "no"):
            return None
        value = int(value)
    pin = int(value)  # type: ignore[arg-type]
    return pin if pin >= 0 else None


def _validate(cfg: RpiConfig) -> RpiConfig:
    """Opraví nesmyslné hodnoty, které by jinak shodily VAD, Whisper nebo zvuk."""
    if cfg.sample_rate != 16000:
        log.warning(
            "sample_rate=%s není podporováno (Whisper i WebRTC VAD pracují v 16 kHz) – používám 16000.",
            cfg.sample_rate,
        )
        cfg.sample_rate = 16000
    if cfg.frame_ms not in (10, 20, 30):
        log.warning("frame_ms=%s není podporováno WebRTC VAD (10/20/30) – používám 30.", cfg.frame_ms)
        cfg.frame_ms = 30
    cfg.vad_aggressiveness = max(0, min(3, int(cfg.vad_aggressiveness)))
    cfg.output_volume_pct = max(0, min(100, int(cfg.output_volume_pct)))
    cfg.whisper_threads = max(1, min(int(cfg.whisper_threads), os.cpu_count() or 4))
    cfg.whisper_beam_size = max(1, int(cfg.whisper_beam_size))
    cfg.speech_rate = max(0.4, min(2.0, float(cfg.speech_rate)))
    cfg.slow_repeat_rate = max(0.4, min(2.0, float(cfg.slow_repeat_rate)))
    cfg.max_record_s = max(2.0, float(cfg.max_record_s))
    cfg.audio_backend = (cfg.audio_backend or "auto").strip().lower()
    if cfg.audio_backend not in ("auto", "pipewire", "alsa"):
        log.warning("Neznámý audio backend %r – používám 'auto'.", cfg.audio_backend)
        cfg.audio_backend = "auto"
    cfg.tts_engine = (cfg.tts_engine or "auto").strip().lower()
    if cfg.tts_engine not in ("auto", "piper", "edge", "server", "espeak"):
        log.warning("Neznámý TTS engine %r – používám 'auto'.", cfg.tts_engine)
        cfg.tts_engine = "auto"
    cfg.whisper_language = (cfg.whisper_language or "cs").strip().lower()
    if cfg.whisper_language not in ("auto",) and not (
        len(cfg.whisper_language) == 2 and cfg.whisper_language.isalpha()
    ):
        log.warning("Neznámý jazyk Whisperu %r – používám 'cs'.", cfg.whisper_language)
        cfg.whisper_language = "cs"
    cfg.rolling_stt_pause_s = max(0.25, float(cfg.rolling_stt_pause_s))
    cfg.likely_complete_tail_s = max(0.4, float(cfg.likely_complete_tail_s))
    cfg.min_listen_s = max(0.0, float(cfg.min_listen_s))
    cfg.followup_start_timeout_s = max(1.0, float(cfg.followup_start_timeout_s))
    cfg.speech_trim_tail_ms = max(0, int(cfg.speech_trim_tail_ms))
    cfg.stt_mode = (cfg.stt_mode or "auto").strip().lower()
    if cfg.stt_mode not in ("auto", "local", "server"):
        log.warning("Neznámý STT režim %r – používám 'auto'.", cfg.stt_mode)
        cfg.stt_mode = "auto"
    for attr in ("mic_device", "speaker_device"):
        val = getattr(cfg, attr)
        if isinstance(val, str) and val.strip().lstrip("-").isdigit():
            setattr(cfg, attr, int(val.strip()))
    cfg.server_url = normalize_server_url(cfg.server_url)
    cfg.piper_model = resolve_data_path(cfg.piper_model)
    if os.sep in cfg.piper_bin or "/" in cfg.piper_bin:
        cfg.piper_bin = resolve_data_path(cfg.piper_bin)
    cfg.cache_dir = Path(resolve_data_path(cfg.cache_dir, must_exist=False))
    if not cfg.cache_dir.is_absolute():
        cfg.cache_dir = (REPO_DIR / cfg.cache_dir).resolve()
    if cfg.led_pin is not None and cfg.led_pin == cfg.gpio_pin:
        log.warning("LED a tlačítko nemohou sdílet GPIO %d – LED vypnuta.", cfg.gpio_pin)
        cfg.led_pin = None
    return cfg


def load_rpi_config(path: str | os.PathLike | None = None) -> RpiConfig:
    """Načte konfiguraci z TOML souboru a proměnných prostředí."""
    cfg = RpiConfig()
    default_toml = PACKAGE_DIR / "rpi_button.toml"
    p = Path(path) if path else Path(os.environ.get("HELPBUTTON_RPI_CONFIG", default_toml))
    if not p.is_absolute() and not p.exists():
        p = Path(resolve_data_path(p))
    if path and not p.exists():
        log.error("Konfigurační soubor %s neexistuje – používám výchozí nastavení.", p)
    raw: dict = {}
    if p.exists():
        try:
            raw = tomllib.loads(p.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError) as e:
            log.error(
                "Chyba v konfiguraci %s (%s) – používám výchozí nastavení. Opravte soubor podle "
                "raspberry_pi/rpi_button.example.toml.",
                p,
                e,
            )
            raw = {}
    try:
        _apply_toml(cfg, raw)
    except (TypeError, ValueError) as e:
        log.error("Neplatná hodnota v konfiguraci %s (%s) – zbytek souboru ignoruji.", p, e)

    cfg.server_url = os.environ.get("HELPBUTTON_SERVER", cfg.server_url)
    cfg.token = os.environ.get("HELPBUTTON_TOKEN", cfg.token)
    cfg.tts_engine = os.environ.get("HELPBUTTON_RPI_TTS", cfg.tts_engine)
    cfg.whisper_model = os.environ.get("HELPBUTTON_RPI_WHISPER", cfg.whisper_model)
    cfg.audio_backend = os.environ.get("HELPBUTTON_RPI_AUDIO_BACKEND", cfg.audio_backend)
    cfg.whisper_language = os.environ.get("HELPBUTTON_RPI_LANGUAGE", cfg.whisper_language)
    return _validate(cfg)


def _apply_toml(cfg: RpiConfig, raw: dict) -> None:
    if raw:
        srv = raw.get("server", {})
        cfg.server_url = str(srv.get("url", cfg.server_url))
        cfg.token = str(srv.get("token", cfg.token) or "")
        cfg.standalone_fallback = bool(srv.get("standalone_fallback", cfg.standalone_fallback))

        btn = raw.get("button", {})
        if "gpio_pin" in btn:
            gpio = _optional_pin(btn["gpio_pin"])
            if gpio is None:
                cfg.enable_gpio = False
            else:
                cfg.gpio_pin = gpio
        if "led_pin" in btn:
            cfg.led_pin = _optional_pin(btn["led_pin"])
        cfg.led_pwm_breathing = bool(btn.get("led_pwm_breathing", cfg.led_pwm_breathing))
        cfg.enable_gpio = bool(btn.get("enable_gpio", cfg.enable_gpio))
        cfg.enable_keyboard = bool(btn.get("enable_keyboard", cfg.enable_keyboard))

        aud = raw.get("audio", {})
        cfg.audio_backend = str(aud.get("backend", cfg.audio_backend))
        cfg.alsa_card = str(aud.get("alsa_card", cfg.alsa_card))
        cfg.hat_mixer_init = bool(aud.get("hat_mixer_init", cfg.hat_mixer_init))
        cfg.output_volume_pct = int(aud.get("output_volume_pct", cfg.output_volume_pct))
        cfg.output_loudness = bool(aud.get("output_loudness", cfg.output_loudness))
        cfg.mic_device = aud.get("mic_device", cfg.mic_device)
        cfg.speaker_device = aud.get("speaker_device", cfg.speaker_device)
        cfg.sample_rate = int(aud.get("sample_rate", cfg.sample_rate))
        cfg.mic_bandpass_filter = bool(aud.get("mic_bandpass_filter", cfg.mic_bandpass_filter))
        cfg.mic_agc_enabled = bool(aud.get("mic_agc_enabled", cfg.mic_agc_enabled))
        cfg.mic_agc_target_rms = float(aud.get("mic_agc_target_rms", cfg.mic_agc_target_rms))
        cfg.mic_agc_max_gain = float(aud.get("mic_agc_max_gain", cfg.mic_agc_max_gain))
        cfg.mic_transient_filter = bool(aud.get("mic_transient_filter", cfg.mic_transient_filter))
        cfg.mic_noise_subtraction = bool(aud.get("mic_noise_subtraction", cfg.mic_noise_subtraction))
        cfg.senior_clarity_dsp = bool(aud.get("senior_clarity_dsp", cfg.senior_clarity_dsp))
        cfg.clarity_presence_gain = float(aud.get("clarity_presence_gain", cfg.clarity_presence_gain))
        cfg.working_heartbeat_s = float(aud.get("working_heartbeat_s", cfg.working_heartbeat_s))
        cfg.slow_repeat_rate = float(aud.get("slow_repeat_rate", cfg.slow_repeat_rate))
        cfg.frame_ms = int(aud.get("frame_ms", cfg.frame_ms))
        cfg.usb_hotplug_retry_s = float(aud.get("usb_hotplug_retry_s", cfg.usb_hotplug_retry_s))

        vad = raw.get("vad", {})
        cfg.pre_roll_ms = int(vad.get("pre_roll_ms", cfg.pre_roll_ms))
        cfg.min_listen_s = float(vad.get("min_listen_s", cfg.min_listen_s))
        cfg.start_timeout_s = float(vad.get("start_timeout_s", cfg.start_timeout_s))
        cfg.followup_start_timeout_s = float(
            vad.get("followup_start_timeout_s", cfg.followup_start_timeout_s)
        )
        cfg.silence_tail_s = float(vad.get("silence_tail_s", cfg.silence_tail_s))
        cfg.followup_silence_tail_s = float(vad.get("followup_silence_tail_s", cfg.followup_silence_tail_s))
        cfg.short_burst_silence_tail_s = float(
            vad.get("short_burst_silence_tail_s", cfg.short_burst_silence_tail_s)
        )
        cfg.hesitation_silence_tail_s = float(
            vad.get("hesitation_silence_tail_s", cfg.hesitation_silence_tail_s)
        )
        cfg.min_speech_s = float(vad.get("min_speech_s", cfg.min_speech_s))
        cfg.max_record_s = float(vad.get("max_record_s", cfg.max_record_s))
        cfg.echo_guard_s = float(vad.get("echo_guard_s", cfg.echo_guard_s))
        cfg.press_grace_s = float(vad.get("press_grace_s", cfg.press_grace_s))
        cfg.speak_listening_greeting = bool(vad.get("speak_listening_greeting", cfg.speak_listening_greeting))
        cfg.listen_beep = bool(vad.get("listen_beep", cfg.listen_beep))
        cfg.auto_listen_followups = bool(vad.get("auto_listen_followups", cfg.auto_listen_followups))
        cfg.vad_aggressiveness = int(vad.get("vad_aggressiveness", cfg.vad_aggressiveness))
        cfg.min_rms_threshold = float(vad.get("min_rms_threshold", cfg.min_rms_threshold))
        cfg.near_field_tv_rejection = bool(vad.get("near_field_tv_rejection", cfg.near_field_tv_rejection))
        cfg.near_field_drop_ratio = float(vad.get("near_field_drop_ratio", cfg.near_field_drop_ratio))
        cfg.rolling_stt_pause_s = float(vad.get("rolling_stt_pause_s", cfg.rolling_stt_pause_s))
        cfg.max_auto_followups = int(vad.get("max_auto_followups", cfg.max_auto_followups))
        cfg.energy_multiplier = float(vad.get("energy_multiplier", cfg.energy_multiplier))
        cfg.likely_complete_tail_s = float(vad.get("likely_complete_tail_s", cfg.likely_complete_tail_s))
        cfg.speech_trim_tail_ms = int(vad.get("speech_trim_tail_ms", cfg.speech_trim_tail_ms))

        stt = raw.get("stt", {})
        cfg.stt_mode = stt.get("mode", cfg.stt_mode)
        cfg.whisper_model = stt.get("whisper_model", cfg.whisper_model)
        cfg.whisper_threads = int(stt.get("whisper_threads", cfg.whisper_threads))
        cfg.whisper_beam_size = int(stt.get("whisper_beam_size", cfg.whisper_beam_size))
        cfg.whisper_compute_type = str(stt.get("whisper_compute_type", cfg.whisper_compute_type))
        cfg.whisper_language = str(stt.get("language", stt.get("whisper_language", cfg.whisper_language)))
        cfg.whisper_vad_filter = bool(stt.get("whisper_vad_filter", cfg.whisper_vad_filter))
        vocab = stt.get("vocabulary", cfg.stt_vocabulary)
        if isinstance(vocab, str):
            vocab = [w.strip() for w in vocab.split(",")]
        cfg.stt_vocabulary = [str(w).strip() for w in (vocab or []) if str(w).strip()]
        cfg.whisper_device = str(stt.get("whisper_device", cfg.whisper_device))
        cfg.fuzzy_senior_repair = bool(stt.get("fuzzy_senior_repair", cfg.fuzzy_senior_repair))
        cfg.rolling_micro_stt = bool(stt.get("rolling_micro_stt", cfg.rolling_micro_stt))
        cfg.auto_translate_to_cs = bool(stt.get("auto_translate_to_cs", cfg.auto_translate_to_cs))
        cfg.bilingual_tts = bool(stt.get("bilingual_tts", cfg.bilingual_tts))

        tts = raw.get("tts", {})
        cfg.tts_engine = tts.get("engine", cfg.tts_engine)
        cfg.sentence_streaming_tts = bool(tts.get("sentence_streaming_tts", cfg.sentence_streaming_tts))
        cfg.edge_voice = tts.get("edge_voice", cfg.edge_voice)
        cfg.piper_bin = tts.get("piper_bin", cfg.piper_bin)
        cfg.piper_model = tts.get("piper_model", cfg.piper_model)
        cfg.speech_rate = float(tts.get("speech_rate", cfg.speech_rate))
        cfg.slow_repeat_rate = float(tts.get("slow_repeat_rate", cfg.slow_repeat_rate))
        cfg.edge_en_voice = tts.get("edge_en_voice", cfg.edge_en_voice)
        cfg.prewarm_phrases = bool(tts.get("prewarm_phrases", cfg.prewarm_phrases))
        if "cache_dir" in tts:
            cfg.cache_dir = Path(str(tts["cache_dir"]))
