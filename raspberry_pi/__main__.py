"""Spouštěcí bod pro Raspberry Pi 5 Hlasové Tlačítko (`python -m raspberry_pi`).

Příklady použití na Raspberry Pi 5:
  python -m raspberry_pi                        # automaticky najde server i zvukovku (Audio Injector Zero / PipeWire)
  python -m raspberry_pi --list-audio           # vypíše zvuková zařízení (PipeWire, HAT WM8731, USB)
  python -m raspberry_pi --audio-backend alsa   # obejde PipeWire a použije HAT přímo (48 kHz / 2 kanály)
  python -m raspberry_pi --test-audio           # otestuje reproduktor, mikrofon, VAD, STT i TTS
  python -m raspberry_pi --server http://192.168.1.10:8765
  python -m raspberry_pi --bilingual            # čte odpovědi česky i anglicky (pro porotu)

Funguje i bez `source .venv/bin/activate`: pokud chybí knihovny, program se sám přepne
do virtuálního prostředí projektu (`.venv`), které vytvořil `setup_rpi5.sh`.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import logging
import os
import signal
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
REQUIRED_MODULES = ("httpx", "websockets", "zeroconf")
_REEXEC_ENV = "HELPBUTTON_RPI_REEXEC"


def _venv_python() -> Path | None:
    for cand in (REPO_DIR / ".venv" / "bin" / "python", REPO_DIR / ".venv" / "Scripts" / "python.exe"):
        if cand.exists():
            return cand
    return None


def _missing_modules() -> list[str]:
    return [m for m in REQUIRED_MODULES if importlib.util.find_spec(m) is None]


def ensure_runtime() -> None:
    """Spuštění systémovým Pythonem (bez aktivovaného `.venv`) by skončilo `No module named 'httpx'`.

    Pokud knihovny chybí a projekt má `.venv`, program se v něm sám znovu spustí se stejnými
    argumenty. Jinak vypíše přesný návod, jak instalaci dokončit (místo tracebacku).
    """
    missing = _missing_modules()
    if sys.version_info >= (3, 11) and not missing:
        return
    venv_py = _venv_python()
    already_in_venv = Path(sys.prefix).resolve() == (REPO_DIR / ".venv").resolve()
    if venv_py is not None and not already_in_venv and not os.environ.get(_REEXEC_ENV):
        env = dict(os.environ)
        env[_REEXEC_ENV] = "1"
        env["PYTHONPATH"] = os.pathsep.join(p for p in (str(REPO_DIR), env.get("PYTHONPATH", "")) if p)
        args = [str(venv_py), "-m", "raspberry_pi", *sys.argv[1:]]
        try:
            sys.stdout.flush()
            sys.stderr.flush()
            if os.name == "nt":
                import subprocess

                raise SystemExit(subprocess.call(args, env=env))
            os.execve(str(venv_py), args, env)
        except OSError as e:
            print(f"[HelpButton] Nepodařilo se spustit {venv_py}: {e}", file=sys.stderr)

    print(
        "\n[HelpButton] Chybí Python knihovny pro hlasové tlačítko: "
        f"{', '.join(missing) if missing else 'Python 3.11+'}",
        file=sys.stderr,
    )
    if venv_py is None:
        print(
            "  Virtuální prostředí .venv neexistuje – instalace nebyla dokončena. Spusťte:\n"
            f"    cd {REPO_DIR}\n    bash raspberry_pi/setup_rpi5.sh",
            file=sys.stderr,
        )
    else:
        print(
            f"  Prostředí {REPO_DIR / '.venv'} je neúplné (instalace pravděpodobně selhala). Spusťte znovu:\n"
            f"    cd {REPO_DIR}\n    bash raspberry_pi/setup_rpi5.sh",
            file=sys.stderr,
        )
    raise SystemExit(1)


def print_audio_devices(cfg) -> None:
    from .audio_io import list_audio_devices, pipewire_running, select_best_device

    devices = list_audio_devices()
    print("\n=== Nalezená zvuková zařízení (PortAudio / ALSA) ===")
    pw_state = "běží" if pipewire_running() else "neběží (použije se přímý přístup k HATu přes ALSA)"
    print(f"  PipeWire: {pw_state} | backend: {cfg.audio_backend} | ALSA karta: {cfg.alsa_card}")
    if not devices:
        print(
            "  (Žádná zvuková zařízení nebyla nalezena. Zkontrolujte HAT Audio Injector Zero: `aplay -l` / `arecord -l`"
        )
        print("   a řádek `dtoverlay=audioinjector-wm8731-audio` v /boot/firmware/config.txt.)", flush=True)
        return
    for d in devices:
        roles = []
        if d.is_input:
            roles.append("MIKROFON (vstup)")
        if d.is_output:
            roles.append("REPRODUKTOR (výstup)")
        tag = (
            " [PipeWire ★]"
            if d.is_pipewire
            else " [HAT WM8731 ★]" if d.is_hat else " [USB]" if d.is_usb else ""
        )
        print(f"  [{d.index}] {d.name} — {', '.join(roles)} ({d.default_samplerate} Hz){tag}")

    best_in = select_best_device(
        devices, want_input=True, configured=cfg.mic_device, backend=cfg.audio_backend
    )
    best_out = select_best_device(
        devices, want_input=False, configured=cfg.speaker_device, backend=cfg.audio_backend
    )
    print("\nAutomaticky vybraná zařízení pro HelpButton:")
    print(
        f"  ► Mikrofon:    {best_in.name if best_in else 'NENALEZEN'} (index={best_in.index if best_in else '-'})"
    )
    print(
        f"  ► Reproduktor: {best_out.name if best_out else 'NENALEZEN'} (index={best_out.index if best_out else '-'})\n",
        flush=True,
    )


async def run_audio_self_test(cfg) -> int:
    """Kompletní test zvuku. Vrací 0 = vše v pořádku, 1 = některý krok selhal (s vysvětlením)."""
    from .audio_io import AudioPlayer, MicrophoneStream, process_vad_stream
    from .stt import SpeechToText
    from .tts import TextToSpeech

    print_audio_devices(cfg)
    player = AudioPlayer(cfg)
    tts = TextToSpeech(cfg)
    stt = SpeechToText(cfg)
    ok = True

    print("1/4 Test zvukových návěstí (pípnutí do 3,5mm jacku HATu)…")
    await asyncio.to_thread(player.play_tone, "beep")
    if player.last_error:
        print(f"  [!] Přehrávání selhalo: {player.last_error}")
        ok = False
    await asyncio.sleep(0.2)

    print(f"2/4 Test české syntézy řeči (TTS engine: {tts.active_engine_name()})…")
    wav = await tts.synthesize(
        "Dobrý den, tady HelpButton. Zkouška reproduktoru a mikrofonu. Řekněte prosím jednu větu."
    )
    if wav:
        await asyncio.to_thread(player.play_wav_bytes, wav)
    else:
        ok = False
        print(
            "  [!] Nepodařilo se vygenerovat TTS WAV. Zkontrolujte instalaci `piper-tts`, `edge-tts` nebo `espeak-ng`."
        )

    await asyncio.sleep(max(cfg.echo_guard_s, 0.2))
    print("3/4 Test nahrávání z mikrofonu HATu s detekcí konce řeči (mluvte nyní)…")
    await asyncio.to_thread(player.play_tone, "beep")
    mic = MicrophoneStream(cfg)
    stop_flag = False

    def _rec():
        nonlocal stop_flag
        try:
            return process_vad_stream(mic.iter_frames(lambda: stop_flag), cfg)
        finally:
            stop_flag = True

    try:
        utt = await asyncio.to_thread(_rec)
    except Exception as e:
        print(f"  [!] Mikrofon nelze otevřít: {e}")
        return 1
    if not utt.has_speech:
        print("  [!] Nebyla zaznamenána žádná řeč (timeout). Zkontrolujte v `alsamixer -c audioinjectorpi`:")
        print("      Input Mux = Mic, Capture zapnutý (~80 %), Mic Boost zapnutý.")
        return 1

    print(
        f"  ✓ Nahráno {utt.duration_s:.1f} s zvuku (řeč: {utt.speech_duration_s:.1f} s, špičkové RMS: {utt.peak_rms:.0f})."
    )
    print("  Přehrávám váš hlas zpět do reproduktoru pro kontrolu čistoty…")
    await asyncio.to_thread(player.play_wav_bytes, utt.wav_bytes)

    print("4/4 Test převodu řeči na text (Faster-Whisper STT + překlad)…")
    if stt.local_available:
        await stt.warmup()
    if stt.local_available:
        res = await stt.transcribe_wav(utt.stt_wav_bytes)
        print(
            f"  ✓ Přepis trval:     {res.elapsed_s:.2f} s (model {cfg.whisper_model}, jazyk {cfg.whisper_language})"
        )
        print(f"  ✓ Rozpoznaný jazyk: {res.language}")
        print(f"  ✓ Původní přepis:   {res.raw_text!r}")
        print(f"  ✓ Český příkaz:     {res.text!r} (přeloženo={res.translated})")
        if res.text:
            confirm_wav = await tts.synthesize(f"Slyšela jsem: {res.text}")
            if confirm_wav:
                await asyncio.to_thread(player.play_wav_bytes, confirm_wav)
        else:
            print("  [!] Whisper nerozpoznal žádný text – mluvte blíž k mikrofonu HATu.")
    else:
        reason = stt.load_error or "Faster-Whisper není lokálně nainstalován"
        print(f"  ({reason} – v ostrém provozu se pošle zvuk na server.)")
    if ok:
        print("\n=== Test zvuku úspěšně dokončen! ===")
        return 0
    print("\n=== Test zvuku dokončen s chybami (viz [!] výše). ===")
    return 1


async def _run_client(cfg) -> None:
    """Spustí klienta a zajistí čisté ukončení i při `systemctl stop` (SIGTERM)."""
    from .client import RpiVoiceButtonClient

    client = RpiVoiceButtonClient(cfg)
    main_task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, getattr(signal, "SIGHUP", None)):
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, main_task.cancel)
        except (NotImplementedError, RuntimeError, ValueError):
            pass
    try:
        await client.run_forever()
    except asyncio.CancelledError:
        pass
    finally:
        await client.shutdown()


def main() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    ensure_runtime()

    from .audio_io import ensure_wm8731_mixer
    from .config import load_rpi_config

    parser = argparse.ArgumentParser(description="HelpButton LAN – Hlasové tlačítko pro Raspberry Pi 5")
    parser.add_argument("--config", default=None, help="Cesta k rpi_button.toml")
    parser.add_argument(
        "--server", default=None, help="URL serveru HelpButton (např. http://192.168.1.10:8765 nebo 'auto')"
    )
    parser.add_argument(
        "--token", default=None, help="Párovací token serveru HelpButton (odpovídá [helpbutton] token v helpbutton.toml)"
    )
    parser.add_argument(
        "--gpio-pin", type=int, default=None, help="BCM číslo GPIO pinu tlačítka (výchozí: 17)"
    )
    parser.add_argument(
        "--no-gpio", action="store_true", help="Vypnout GPIO a používat pouze klávesnici / USB tlačítko"
    )
    parser.add_argument(
        "--mic", default=None, help="Index nebo část názvu mikrofonu (výchozí: auto = PipeWire / HAT)"
    )
    parser.add_argument(
        "--speaker", default=None, help="Index nebo část názvu výstupu (výchozí: auto = PipeWire / HAT)"
    )
    parser.add_argument(
        "--audio-backend",
        choices=["auto", "pipewire", "alsa"],
        default=None,
        help="auto = PipeWire, jinak přímo HAT; alsa = obejít PipeWire (48 kHz / 2 kanály)",
    )
    parser.add_argument("--tts", choices=["auto", "piper", "edge", "server", "espeak"], default=None)
    parser.add_argument(
        "--whisper-model", default=None, help="Velikost Faster-Whisper modelu (tiny, base, small, medium)"
    )
    parser.add_argument(
        "--min-listen",
        type=float,
        default=None,
        help="Minimální doba poslechu po stisku v sekundách (výchozí: 4.0)",
    )
    parser.add_argument(
        "--silence-tail", type=float, default=None, help="Délka ticha po řeči v sekundách (výchozí: 1.15)"
    )
    parser.add_argument(
        "--bilingual",
        action="store_true",
        help="Číst odpovědi česky i anglicky a rozpoznávat i angličtinu (pro demo)",
    )
    parser.add_argument(
        "--language",
        choices=["cs", "auto"],
        default=None,
        help="Jazyk rozpoznávání: cs = jen čeština (nejpřesnější, výchozí), auto = i angličtina",
    )
    parser.add_argument(
        "--no-clarity-dsp", action="store_true", help="Vypnout psychoakustický filtr pro seniory"
    )
    parser.add_argument("--no-rolling-stt", action="store_true", help="Vypnout průběžný přepis během pomlky")
    parser.add_argument("--no-streaming-tts", action="store_true", help="Vypnout větné streamování TTS")
    parser.add_argument(
        "--list-audio", action="store_true", help="Vypsat připojená zvuková zařízení a skončit"
    )
    parser.add_argument(
        "--test-audio",
        action="store_true",
        help="Spustit kompletní test reproduktoru, mikrofonu, VAD, STT a TTS",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Podrobné ladicí výpisy")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    if not args.verbose:
        for noisy in (
            "httpx",
            "httpx2",
            "httpcore",
            "huggingface_hub",
            "websockets",
            "zeroconf",
            "faster_whisper",
            "asyncio",
        ):
            logging.getLogger(noisy).setLevel(logging.WARNING)

    from .config import normalize_server_url

    cfg = load_rpi_config(args.config)
    if args.server:
        cfg.server_url = normalize_server_url(args.server)
    if args.gpio_pin is not None:
        cfg.gpio_pin = args.gpio_pin
    if args.no_gpio:
        cfg.enable_gpio = False
    if args.mic is not None:
        cfg.mic_device = int(args.mic) if args.mic.strip().isdigit() else args.mic
    if args.speaker is not None:
        cfg.speaker_device = int(args.speaker) if args.speaker.strip().isdigit() else args.speaker
    if args.audio_backend:
        cfg.audio_backend = args.audio_backend
    if args.tts:
        cfg.tts_engine = args.tts
    if args.whisper_model:
        cfg.whisper_model = args.whisper_model
    if args.token is not None:
        cfg.token = args.token.strip()
    if args.min_listen is not None:
        cfg.min_listen_s = max(0.0, args.min_listen)
    if args.silence_tail is not None:
        cfg.silence_tail_s = max(0.3, args.silence_tail)
    if args.language:
        cfg.whisper_language = args.language
    elif args.bilingual and cfg.whisper_language == "cs":
        cfg.whisper_language = "auto"
    if args.bilingual:
        cfg.bilingual_tts = True
    if args.no_clarity_dsp:
        cfg.senior_clarity_dsp = False
    if args.no_rolling_stt:
        cfg.rolling_micro_stt = False
    if args.no_streaming_tts:
        cfg.sentence_streaming_tts = False

    if args.list_audio:
        print_audio_devices(cfg)
        return

    if cfg.hat_mixer_init:
        try:
            ensure_wm8731_mixer(cfg.alsa_card, cfg.output_volume_pct)
        except Exception as e:
            logging.getLogger("helpbutton.rpi").warning("Nastavení mixéru přeskočeno: %s", e)

    if args.test_audio:
        try:
            raise SystemExit(asyncio.run(run_audio_self_test(cfg)))
        except KeyboardInterrupt:
            print("\nTest přerušen.")
            raise SystemExit(130)

    print_audio_devices(cfg)
    try:
        asyncio.run(_run_client(cfg))
    except KeyboardInterrupt:
        pass
    print("\nHelpButton tlačítko ukončeno.")


if __name__ == "__main__":
    main()
