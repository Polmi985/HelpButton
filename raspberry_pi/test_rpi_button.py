"""Testy pro Raspberry Pi 5 Hlasové Tlačítko (Grandpa-Proof VAD, USB Audio, STT/Překlad, E2E Server)."""

from __future__ import annotations

import array
import math
import sys
from typing import Iterable

import httpx

from helpbutton.agents.mock import MockPc, MockPhone
from helpbutton.server.app import create_app
from helpbutton.server.config import Config, Contact
from raspberry_pi.audio_io import (
    AudioDeviceInfo,
    AudioPlayer,
    RecordedUtterance,
    VadDetector,
    apply_mic_speech_cleanup_pcm16,
    apply_senior_clarity_dsp_pcm16,
    apply_senior_clarity_dsp_wav,
    is_transient_pop_frame,
    pcm16_rms,
    pcm_to_wav,
    process_vad_stream,
    resample_pcm16_hq,
    resample_pcm16_mono,
    select_best_device,
    suppress_transients_pcm16,
    synthesize_tone_wav,
)
from raspberry_pi.client import RpiVoiceButtonClient
from raspberry_pi.config import RpiConfig, load_rpi_config
from raspberry_pi.stt import (
    SpeechToText,
    TranscriptionResult,
    classify_utterance_completeness,
    is_repeat_request,
    is_whisper_hallucination,
    normalize_senior_speech,
    select_whisper_prompt,
    translate_to_czech,
)
from raspberry_pi.tts import TextToSpeech, split_into_clauses


def make_frame(
    amplitude: int = 0, freq: float = 220.0, rate: int = 16000, samples_per_frame: int = 480
) -> bytes:
    """Vygeneruje jeden 30ms PCM rámec: buď ticho/šum, nebo simulovaný lidský hlas."""
    arr = array.array("h")
    for i in range(samples_per_frame):
        if amplitude <= 0:
            arr.append(0)
        else:
            val = int(amplitude * math.sin(2.0 * math.pi * freq * i / rate))
            arr.append(val)
    if sys.byteorder != "little":
        arr.byteswap()
    return arr.tobytes()


SILENCE_FRAME = make_frame(amplitude=25, freq=60.0)
SPEECH_FRAME = make_frame(amplitude=3200, freq=210.0)


def test_usb_audio_device_selection_prefers_usb_over_hdmi():
    devices = [
        AudioDeviceInfo(0, "vc4-hdmi-0 (HDMI Audio)", is_input=False, is_output=True, is_usb=False),
        AudioDeviceInfo(1, "vc4-hdmi-1 (HDMI Audio)", is_input=False, is_output=True, is_usb=False),
        AudioDeviceInfo(2, "USB PnP Sound Device (Zvukovka)", is_input=False, is_output=True, is_usb=True),
        AudioDeviceInfo(3, "Default ALSA Input", is_input=True, is_output=False, is_usb=False),
        AudioDeviceInfo(4, "USB Condenser Squid Microphone", is_input=True, is_output=False, is_usb=True),
    ]
    mic = select_best_device(devices, want_input=True, configured="auto")
    spk = select_best_device(devices, want_input=False, configured="auto")
    assert mic is not None and mic.index == 4
    assert spk is not None and spk.index == 2


HAT_DEVICES = [
    AudioDeviceInfo(0, "vc4-hdmi-0: MAI PCM i2s-hifi-0 (hw:0,0)", is_input=False, is_output=True),
    AudioDeviceInfo(
        1,
        "audioinjector-pi-soundcard: AudioInjector audio wm8731-hifi-0 (hw:2,0)",
        True,
        True,
        48000,
        is_hat=True,
        max_input_channels=2,
        max_output_channels=2,
    ),
    AudioDeviceInfo(
        2, "pulse", True, True, 44100, is_pipewire=True, max_input_channels=32, max_output_channels=32
    ),
    AudioDeviceInfo(
        3, "pipewire", True, True, 44100, is_pipewire=True, max_input_channels=64, max_output_channels=64
    ),
    AudioDeviceInfo(
        4, "default", True, True, 44100, is_pipewire=True, max_input_channels=64, max_output_channels=64
    ),
]


def test_hat_selection_prefers_pipewire_then_raw_hat():
    from raspberry_pi.audio_io import _classify_device_name

    assert _classify_device_name(
        "audioinjector-pi-soundcard: AudioInjector audio wm8731-hifi-0 (hw:2,0)"
    ) == (False, True, False)
    assert _classify_device_name("pipewire") == (False, False, True)
    assert _classify_device_name("vc4-hdmi-0: MAI PCM i2s-hifi-0 (hw:0,0)") == (False, False, False)

    for want_input in (True, False):
        auto = select_best_device(HAT_DEVICES, want_input=want_input, configured="auto", backend="auto")
        assert auto is not None and auto.name == "pipewire"
        raw = select_best_device(HAT_DEVICES, want_input=want_input, configured="auto", backend="alsa")
        assert raw is not None and raw.is_hat
    no_pw = [d for d in HAT_DEVICES if not d.is_pipewire]
    spk = select_best_device(no_pw, want_input=False, configured="auto")
    assert spk is not None and spk.is_hat


def test_alsa_pcm_names_for_hat_and_pipewire():
    from raspberry_pi.audio_io import alsa_pcm_name

    assert alsa_pcm_name(HAT_DEVICES[1]) == "plughw:CARD=audioinjectorpi,DEV=0"
    assert alsa_pcm_name(HAT_DEVICES[3]) == "pipewire"
    assert (
        alsa_pcm_name(AudioDeviceInfo("plughw:CARD=audioinjectorpi,DEV=0", "x", True, False))
        == "plughw:CARD=audioinjectorpi,DEV=0"
    )
    assert alsa_pcm_name(None) is None


class _FakeSd:
    """Simuluje WM8731 přes surové hw: přijme jen 48 kHz / 2 kanály (jako bez PipeWire)."""

    def __init__(self, left: bytes, right: bytes):
        self.opened: list[tuple[int, int]] = []
        self._left = array.array("h", left)
        self._right = array.array("h", right)
        self.written = bytearray()

    def _check(self, samplerate, channels):
        self.opened.append((samplerate, channels))
        if samplerate != 48000 or channels != 2:
            raise RuntimeError("Invalid number of channels / sample rate")

    def _block(self, n):
        inter = array.array("h", bytes(n * 4))
        inter[0::2] = self._left[:n]
        inter[1::2] = self._right[:n]
        return inter.tobytes()

    def RawInputStream(self, samplerate, blocksize, device, channels, dtype, callback=None, stall=False):
        self._check(samplerate, channels)
        sd = self

        class _In:
            def __init__(self):
                self._stop = None
                self._thread = None

            def __enter__(self):
                import threading
                import time as _t

                if callback is not None and not getattr(sd, "stall", False):
                    self._stop = threading.Event()

                    def _pump():
                        while not self._stop.is_set():
                            callback(sd._block(blocksize), blocksize, None, None)
                            _t.sleep(0.005)

                    self._thread = threading.Thread(target=_pump, daemon=True)
                    self._thread.start()
                return self

            def __exit__(self, *a):
                if self._stop is not None:
                    self._stop.set()
                    self._thread.join(timeout=1.0)
                return False

            def read(self, n):
                return sd._block(n), False

        return _In()

    def RawOutputStream(self, samplerate, device, channels, dtype):
        self._check(samplerate, channels)
        sd = self

        class _Out:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def write(self, data):
                sd.written.extend(data)

            def abort(self):
                pass

        return _Out()


def test_hat_capture_48k_stereo_is_downmixed_to_16k_mono():
    from raspberry_pi.audio_io import MicrophoneStream

    cfg = RpiConfig()
    speech_48k = make_frame(amplitude=4000, freq=220.0, rate=48000, samples_per_frame=1440)
    fake = _FakeSd(speech_48k, speech_48k)
    mic = MicrophoneStream(cfg)
    frames = []
    for frame in mic._iter_sounddevice(fake, HAT_DEVICES[1], lambda: len(frames) >= 3):
        frames.append(frame)
    assert fake.opened[-1] == (48000, 2)
    assert mic.active_format == (48000, 2)
    assert all(len(f) == 960 for f in frames)
    assert pcm16_rms(frames[0]) > 2000


def test_hat_playback_falls_back_to_48k_stereo():
    cfg = RpiConfig()
    player = AudioPlayer(cfg)
    player._select_output = lambda: HAT_DEVICES[1]  # type: ignore[method-assign]
    fake = _FakeSd(b"", b"")
    wav = synthesize_tone_wav("beep", rate=22050)
    player._play_via_sounddevice(fake, wav)
    assert fake.opened[-1] == (48000, 2)
    samples = array.array("h", bytes(fake.written))
    assert len(samples) > 0 and len(samples) % 2 == 0
    assert samples[0::2] == samples[1::2]


def test_alsa_backend_opens_hat_natively_first():
    from raspberry_pi.audio_io import _stream_format_candidates

    cfg = RpiConfig()
    cfg.audio_backend = "alsa"
    assert _stream_format_candidates(cfg, HAT_DEVICES[1], True, 16000)[0] == (48000, 2)
    cfg.audio_backend = "auto"
    assert _stream_format_candidates(cfg, HAT_DEVICES[3], True, 16000)[0] == (16000, 1)


def test_gpio_refuses_pins_used_by_audio_hat():
    import asyncio

    from raspberry_pi.button_hw import HAT_RESERVED_PINS, ButtonController

    assert set(HAT_RESERVED_PINS) == {2, 3, 18, 19, 20, 21}
    cfg = RpiConfig()
    assert cfg.gpio_pin == 17 and cfg.led_pin == 27
    cfg.gpio_pin = 18
    btn = ButtonController(cfg, lambda: None)
    btn._init_gpio(asyncio.new_event_loop())
    assert btn._gpio_btn is None


def test_resample_48khz_to_16khz_preserves_signal():
    frame_48k = make_frame(amplitude=4000, freq=220.0, rate=48000, samples_per_frame=1440)
    resampled = resample_pcm16_mono(frame_48k, src_rate=48000, dst_rate=16000)
    assert len(resampled) == 480 * 2
    assert pcm16_rms(resampled) > 2000


def test_tone_generator_produces_valid_wav():
    for kind in ("beep", "listen_followup", "double", "low", "working_tick"):
        wav = synthesize_tone_wav(kind)
        assert wav.startswith(b"RIFF") and len(wav) > 600


def test_senior_clarity_dsp_boosts_high_consonants_and_removes_low_rumble():
    """Ověří psychoakustický filtr pro seniory (Presbycusis DSP):
    1. Utlumí 50Hz brum pod 140 Hz.
    2. Zvýrazní 3000Hz pásmo souhlásek (s, š, č, t, k).
    3. Měkký kompresor nikdy nepřekročí rozsah 16-bit PCM (-32767..32767).
    """
    low_rumble = make_frame(amplitude=2500, freq=45.0, rate=16000, samples_per_frame=1600)
    high_consonant = make_frame(amplitude=2500, freq=3100.0, rate=16000, samples_per_frame=1600)

    filtered_low = apply_senior_clarity_dsp_pcm16(low_rumble, rate=16000, presence_gain=1.55)
    filtered_high = apply_senior_clarity_dsp_pcm16(high_consonant, rate=16000, presence_gain=1.55)

    assert pcm16_rms(filtered_low) < pcm16_rms(low_rumble) * 0.65
    assert pcm16_rms(filtered_high) > pcm16_rms(high_consonant) * 1.15

    loud_wav = pcm_to_wav(
        make_frame(amplitude=30000, freq=2800.0, rate=16000, samples_per_frame=800), rate=16000
    )
    processed_wav = apply_senior_clarity_dsp_wav(loud_wav, presence_gain=1.8, extra_gain=1.4)
    assert processed_wav.startswith(b"RIFF") and len(processed_wav) == len(loud_wav)


def test_mic_bandpass_agc_and_transient_pop_rejection():
    """Ověří 4-vrstvé čištění zvuku z levného USB mikrofonu před Whisper STT:
    1. Pásmová propust (85–7200 Hz) utlumí 45Hz síťový brum a nechá projít 1000Hz lidský hlas.
    2. AGC automaticky zesílí tichou řeč seniora (RMS ~495 -> >2000) bez přebuzení.
    3. Detektor transientů odfiltruje krátké ťuknutí hrnkem do stolu, aby nespustilo falešný poslech.
    """
    hum_45hz = make_frame(amplitude=2500, freq=45.0, rate=16000, samples_per_frame=1600)
    voice_1khz = make_frame(amplitude=2500, freq=1000.0, rate=16000, samples_per_frame=1600)
    clean_hum = apply_mic_speech_cleanup_pcm16(
        hum_45hz, rate=16000, agc_enabled=False, noise_subtraction=False
    )
    clean_voice = apply_mic_speech_cleanup_pcm16(
        voice_1khz, rate=16000, agc_enabled=False, noise_subtraction=False
    )
    assert pcm16_rms(clean_hum) < pcm16_rms(hum_45hz) * 0.60
    assert pcm16_rms(clean_voice) > pcm16_rms(voice_1khz) * 0.85

    quiet_speech = make_frame(amplitude=700, freq=300.0, rate=16000, samples_per_frame=1600)
    boosted_speech = apply_mic_speech_cleanup_pcm16(
        quiet_speech,
        rate=16000,
        agc_enabled=True,
        target_rms=4800.0,
        max_gain=4.5,
    )
    assert pcm16_rms(boosted_speech) > pcm16_rms(quiet_speech) * 3.0

    pop_arr = array.array("h", [0] * 480)
    pop_arr[100] = 18000
    pop_arr[101] = -16000
    pop_arr[102] = 12000
    pop_frame = pop_arr.tobytes()
    assert is_transient_pop_frame(pop_frame)
    assert not is_transient_pop_frame(SPEECH_FRAME)

    vad = VadDetector(RpiConfig(mic_transient_filter=True))
    is_sp, _ = vad.is_speech(pop_frame, 16000)
    assert not is_sp

    stream_with_pop = SILENCE_FRAME * 2 + pop_frame + SILENCE_FRAME * 2
    suppressed = suppress_transients_pcm16(stream_with_pop, rate=16000)
    assert pcm16_rms(suppressed) < pcm16_rms(stream_with_pop) * 0.4


def test_vad_zero_crosstalk_speaker_ducking_ignores_own_voice():
    """Dokud reproduktor hraje (is_speaker_active=True), mikrofon nesmí zachytit
    odraz hlasu HelpButton z místnosti, ale jakmile reproduktor dohraje, nahraje odpověď seniora.
    """
    cfg = RpiConfig(start_timeout_s=1.0, silence_tail_s=0.5, min_speech_s=0.3)
    idx_state = {"i": 0}

    def gen() -> Iterable[bytes]:
        frames = [SPEECH_FRAME] * 20 + [SILENCE_FRAME] * 10 + [SPEECH_FRAME] * 15 + [SILENCE_FRAME] * 25
        for i, f in enumerate(frames):
            idx_state["i"] = i
            yield f

    utt = process_vad_stream(
        gen(),
        cfg,
        is_speaker_active=lambda: idx_state["i"] < 20,
    )
    assert utt.has_speech
    assert 0.35 <= utt.speech_duration_s <= 0.60


def test_vad_tolerates_mid_sentence_thinking_pause():
    """Dědeček zmáčkne tlačítko, 2s přemýšlí, řekne 'Zmizel mi...', na 1.8s se odmlčí,
    dořekne '...WhatsApp' a pak je 2.2s ticho.
    Nahrávání se NESMÍ utnout během 1.8s pomlky uprostřed věty!
    """
    cfg = RpiConfig(
        pre_roll_ms=300,
        start_timeout_s=6.0,
        silence_tail_s=2.2,
        min_speech_s=0.30,
    )
    stream = (
        [SILENCE_FRAME] * 65
        + [SPEECH_FRAME] * 30
        + [SILENCE_FRAME] * 60
        + [SPEECH_FRAME] * 35
        + [SILENCE_FRAME] * 80
    )
    utt = process_vad_stream(stream, cfg)
    assert utt.has_speech
    assert not utt.timed_out
    assert utt.speech_duration_s >= 1.85
    assert utt.duration_s >= 5.5


def test_vad_hold_to_talk_never_times_out_while_button_held():
    """Pokud dědeček drží tlačítko stisknuté 4 sekundy předtím, než začne mluvit,
    nahrávání nesmí vypršet a po uvolnění tlačítka vše odešle.
    """
    cfg = RpiConfig(start_timeout_s=2.0, silence_tail_s=1.0, min_speech_s=0.3)
    idx_Tracker = {"i": 0}

    def gen() -> Iterable[bytes]:
        frames = [SILENCE_FRAME] * 100 + [SPEECH_FRAME] * 20 + [SILENCE_FRAME] * 40
        for i, f in enumerate(frames):
            idx_Tracker["i"] = i
            yield f

    utt = process_vad_stream(
        gen(),
        cfg,
        is_button_held=lambda: idx_Tracker["i"] < 115,
    )
    assert utt.has_speech
    assert utt.speech_duration_s >= 0.5


def test_vad_second_button_press_finishes_recording_early():
    """Pokud dědeček zmáčkne tlačítko, řekne větu a zmáčkne tlačítko podruhé ('už jsem domluvil'),
    nahrávka se ihned dokončí a odešle bez čekání na 2.2 s ticha.
    """
    cfg = RpiConfig(silence_tail_s=2.2, min_speech_s=0.3)
    step = {"i": 0}

    def gen() -> Iterable[bytes]:
        for i in range(200):
            step["i"] = i
            yield SPEECH_FRAME if 10 <= i < 30 else SILENCE_FRAME

    utt = process_vad_stream(
        gen(),
        cfg,
        finish_early=lambda: step["i"] == 32,
    )
    assert utt.has_speech
    assert step["i"] == 32


def test_vad_button_press_during_auto_followup_absorbs_and_continues():
    """Když se HelpButton zeptá 'Řekněte ano' (is_followup=True) a dědeček ze zvyku zmáčkne tlačítko
    před tím, než řekne 'Ano', nahrávání se nezruší, ale pokračuje a nahraje 'Ano'.
    """
    cfg = RpiConfig(start_timeout_s=1.5, followup_silence_tail_s=0.9, silence_tail_s=1.2, min_speech_s=0.3)
    step = {"i": 0}
    pressed_once = {"v": False}

    def gen() -> Iterable[bytes]:
        for i in range(150):
            step["i"] = i
            if i == 35:
                pressed_once["v"] = True
            yield SPEECH_FRAME if 60 <= i < 75 else SILENCE_FRAME

    def on_btn() -> bool:
        if pressed_once["v"]:
            pressed_once["v"] = False
            return True
        return False

    utt = process_vad_stream(
        gen(),
        cfg,
        is_followup=True,
        on_button_pressed_during_followup=on_btn,
    )
    assert utt.has_speech
    assert utt.speech_duration_s >= 0.4


def test_vad_times_out_cleanly_on_pure_silence():
    cfg = RpiConfig(start_timeout_s=0.6, min_speech_s=0.3)
    utt = process_vad_stream([SILENCE_FRAME] * 50, cfg)
    assert not utt.has_speech
    assert utt.timed_out


def test_vad_near_field_tv_rejection_ignores_distant_television():
    """Když dědeček mluví u mikrofonu (RMS ~2260) a po domluvení hraje v obýváku
    vzdálená televize (RMS ~310 – hlasitější než klidový šum 17, ale <22 % hlasu dědečka),
    Near-Field filtr odfiltruje televizi a včas ukončí nahrávání.
    """
    cfg = RpiConfig(
        silence_tail_s=0.9,
        min_speech_s=0.3,
        near_field_tv_rejection=True,
        near_field_drop_ratio=0.22,
    )
    tv_background_frame = make_frame(amplitude=440, freq=240.0)
    stream = [SILENCE_FRAME] * 10 + [SPEECH_FRAME] * 25 + [tv_background_frame] * 45
    utt = process_vad_stream(stream, cfg)
    assert utt.has_speech
    assert not utt.timed_out
    assert utt.speech_duration_s <= 0.85


def test_vad_dynamic_3tier_short_burst_followup_and_hesitation_extension():
    """Ověří 3-stupňové sémantické ukončování:
    1. Krátká odpověď 'Ano' (is_followup=True, řeč < 1.1 s) skončí bleskově za 0.65 s.
    2. Průběžná kontrola on_pause_check: pokud dědeček řekne 'Zmizel mi a...' ('hesitation'),
       prodlouží se čekání na 2.8 s; pokud řekne kompletní větu ('complete'), skončí ihned.
    """
    cfg = RpiConfig(
        silence_tail_s=2.2,
        followup_silence_tail_s=1.6,
        short_burst_silence_tail_s=0.60,
        hesitation_silence_tail_s=2.8,
        rolling_stt_pause_s=0.45,
        min_speech_s=0.30,
    )
    short_stream = [SILENCE_FRAME] * 5 + [SPEECH_FRAME] * 12 + [SILENCE_FRAME] * 25
    utt_short = process_vad_stream(short_stream, cfg, is_followup=True)
    assert utt_short.has_speech
    assert utt_short.duration_s < 1.35

    utt_complete = process_vad_stream(
        [SILENCE_FRAME] * 5 + [SPEECH_FRAME] * 25 + [SILENCE_FRAME] * 20,
        cfg,
        on_pause_check=lambda _wav: ("complete", "Zmizel mi WhatsApp"),
    )
    assert utt_complete.has_speech
    assert utt_complete.pre_transcribed_text == "Zmizel mi WhatsApp"

    hesitation_stream = (
        [SILENCE_FRAME] * 5
        + [SPEECH_FRAME] * 20
        + [SILENCE_FRAME] * 80
        + [SPEECH_FRAME] * 20
        + [SILENCE_FRAME] * 95
    )
    calls = {"n": 0}

    def pause_cb(_wav: bytes) -> tuple[str, str]:
        calls["n"] += 1
        if calls["n"] == 1:
            return ("hesitation", "Zmizel mi a")
        return ("complete", "Zmizel mi a nejde zapnout")

    utt_hes = process_vad_stream(hesitation_stream, cfg, on_pause_check=pause_cb)
    assert utt_hes.has_speech
    assert utt_hes.speech_duration_s >= 1.15
    assert utt_hes.pre_transcribed_text == "Zmizel mi a nejde zapnout"


def test_whisper_hallucination_filter():
    assert is_whisper_hallucination("")
    assert is_whisper_hallucination("   ...  ♪ ")
    assert is_whisper_hallucination("Titulky vytvořil JohnyX.")
    assert is_whisper_hallucination("Děkuji za pozornost!")
    assert is_whisper_hallucination("Subtitles by Amara.org community")
    assert is_whisper_hallucination("ano ano ano ano ano ano ano")

    assert not is_whisper_hallucination("ano")
    assert not is_whisper_hallucination("ne")
    assert not is_whisper_hallucination("Zmizel mi WhatsApp")


async def test_translation_en_and_sk_to_czech():
    cs, tr = await translate_to_czech("My WhatsApp disappeared", "en")
    assert cs == "Zmizel mi WhatsApp" and tr

    cs, tr = await translate_to_czech("Yes please", "en")
    assert cs == "ano" and tr

    cs, tr = await translate_to_czech("Áno", "sk")
    assert cs == "ano" and tr

    cs, tr = await translate_to_czech("The text is too small, I can't read it", "en")
    assert cs == "Je to moc malé, nevidím" and tr

    cs, tr = await translate_to_czech("Someone from the bank called me to install AnyDesk", "en")
    assert "AnyDesk" in cs and tr

    cs, tr = await translate_to_czech("Zmizel mi WhatsApp", "cs")
    assert cs == "Zmizel mi WhatsApp" and not tr


def test_semantic_completeness_and_repeat_request_classifiers():
    assert classify_utterance_completeness("Zmizel mi a") == "hesitation"
    assert classify_utterance_completeness("Nevím jak...") == "hesitation"
    assert classify_utterance_completeness("Zmizel mi WhatsApp") == "complete"
    assert classify_utterance_completeness("ano", is_followup=True) == "complete"

    assert is_repeat_request("Cože?")
    assert is_repeat_request("Prosím? Nerozuměl jsem.")
    assert is_repeat_request("Zopakuj to pomaleji")
    assert is_repeat_request("What? Repeat please")
    assert not is_repeat_request("Zmizel mi WhatsApp")
    assert not is_repeat_request("ano")


def test_dynamic_prompts_and_fuzzy_phonetic_senior_normalization():
    """Ověří kontextový prompt pro Whisper a fonetickou opravu komolených slov seniorů."""
    p_default = select_whisper_prompt(is_followup=False)
    p_followup = select_whisper_prompt(is_followup=True)
    assert "WhatsApp" in p_default and "AnyDesk" in p_default
    assert p_followup.startswith("Ano. Ne. Určitě.")

    assert normalize_senior_speech("Zmizel mi votsap") == "Zmizel mi WhatsApp"
    assert normalize_senior_speech("Zmizel mi Vojtěch") == "Zmizel mi WhatsApp"
    assert normalize_senior_speech("Zmizel mi WhasApp") == "Zmizel mi WhatsApp"
    assert normalize_senior_speech("Pošli esemesku Lence") == "Pošli SMS Lence"
    assert (
        normalize_senior_speech("Chtěli po mně nainstalovat enydesk") == "Chtěli po mně nainstalovat AnyDesk"
    )
    assert (
        normalize_senior_speech("Nejde mi otevřít seznámek na internety")
        == "Nejde mi otevřít Seznam na internet"
    )


def test_sentence_streaming_clause_splitter():
    clauses = split_into_clauses("WhatsApp je v telefonu vypnutý. Mám ho zapnout a otevřít? Řekněte ano.")
    assert clauses == [
        "WhatsApp je v telefonu vypnutý.",
        "Mám ho zapnout a otevřít? Řekněte ano.",
    ]


class FakePlayer(AudioPlayer):
    def __init__(self, cfg: RpiConfig):
        super().__init__(cfg)
        self.tones: list[str] = []
        self.wavs_played: int = 0
        self.extra_gains: list[float] = []

    def play_tone(self, kind: str) -> None:
        self.tones.append(kind)

    def play_wav_bytes(
        self,
        wav_bytes: bytes,
        *,
        apply_clarity: bool = True,
        extra_gain: float = 1.0,
    ) -> None:
        if wav_bytes:
            self.wavs_played += 1
            self.extra_gains.append(extra_gain)


class ScriptedStt(SpeechToText):
    def __init__(self, cfg: RpiConfig, phrases: list[tuple[str, str]]):
        super().__init__(cfg)
        self._phrases = list(phrases)

    @property
    def local_available(self) -> bool:
        return True

    async def warmup(self) -> None:
        pass

    async def transcribe_wav(
        self,
        wav_bytes: bytes,
        extra_prompt: str = "",
        *,
        is_followup: bool = False,
    ) -> TranscriptionResult:
        raw, lang = self._phrases.pop(0)
        cs, translated = await translate_to_czech(raw, lang)
        if self.cfg.fuzzy_senior_repair:
            cs = normalize_senior_speech(cs)
        return TranscriptionResult(text=cs, raw_text=raw, language=lang, translated=translated)


class DummyTts(TextToSpeech):
    def __init__(self, cfg: RpiConfig):
        super().__init__(cfg)
        self.rates_used: list[float | None] = []

    async def warmup(self, senior_name: str = "Paní Jano") -> None:
        pass

    async def synthesize(
        self,
        text: str,
        lang: str = "cs",
        rate_override: float | None = None,
    ) -> bytes | None:
        self.rates_used.append(rate_override)
        return synthesize_tone_wav("beep")


async def _connect_mock_device(app, agent) -> None:
    registry = app.state.registry

    async def send(msg: dict) -> None:
        import asyncio

        async def _run() -> None:
            res = await agent.invoke(msg["tool"], msg.get("args") or {})
            registry.on_result({"type": "result", "id": msg["id"], **res})

        asyncio.get_running_loop().create_task(_run())

    hello = await agent._hello()
    await registry.attach(hello, send)


async def test_e2e_rpi_button_restores_whatsapp_with_auto_followup(tmp_path):
    """Kompletní průchod:
    1. Dědeček zmáčkne tlačítko na Raspberry Pi 5 (1×!).
    2. Řekne 'Zmizel mi WhatsApp' (nebo anglicky 'My WhatsApp disappeared').
    3. HelpButton na serveru diagnostikuje telefon, přečte otázku 'WhatsApp byl vypnutý. Mám ho zapnout a otevřít? Řekněte ano.'
    4. Raspberry Pi 5 díky `expects_answer=True` AUTOMATICKY pípne a poslouchá odpověď bez nutnosti mačkat tlačítko podruhé!
    5. Dědeček řekne 'Ano'.
    6. HelpButton zapne WhatsApp na telefonu a přečte potvrzení 'Hotovo...'.
    """
    srv_cfg = Config(
        llm_provider="keywords", vlm_provider="none", data_dir=tmp_path, mdns=False, adb_watchdog=False
    )
    srv_cfg.contacts = [
        Contact("Lenka", phone="+420 777 123 456", aliases=["lenka", "lence"], dative="Lence")
    ]
    app = create_app(srv_cfg)

    phone = MockPhone("Telefon babičky", delay=0)
    pc = MockPc("Počítač", delay=0)
    await _connect_mock_device(app, phone)
    await _connect_mock_device(app, pc)
    assert phone.apps["com.whatsapp"] == "disabled"

    rpi_cfg = RpiConfig(
        enable_gpio=False,
        enable_keyboard=False,
        echo_guard_s=0.0,
        silence_tail_s=0.3,
        followup_silence_tail_s=0.3,
        min_speech_s=0.15,
        auto_listen_followups=True,
        speak_listening_greeting=True,
    )
    player = FakePlayer(rpi_cfg)
    stt = ScriptedStt(rpi_cfg, [("My WhatsApp disappeared", "en"), ("Yes", "en")])
    tts = DummyTts(rpi_cfg)

    def fake_mic(_stop_chk) -> Iterable[bytes]:
        return [SILENCE_FRAME] * 5 + [SPEECH_FRAME] * 12 + [SILENCE_FRAME] * 15

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        client = RpiVoiceButtonClient(
            rpi_cfg,
            http_client=http_client,
            stt=stt,
            tts=tts,
            player=player,
            frame_source=fake_mic,
        )
        final_reply = await client.run_interaction()

    assert final_reply is not None
    assert final_reply["screen"] == "hotovo"
    assert phone.apps["com.whatsapp"] == "ok"
    assert phone.foreground == "com.whatsapp"
    assert any("Poslouchám" in s for s in client.spoken_history)
    assert any("je v telefonu vypnut" in s for s in client.spoken_history)
    assert any("Hotovo" in s for s in client.spoken_history)
    assert "listen_followup" in player.tones
    assert "double" in player.tones


async def test_e2e_emergency_stop_cancels_active_session(tmp_path):
    """Stisk tlačítka během čekání/práce okamžitě zavolá /api/stop a řekne 'Zastavila jsem'."""
    srv_cfg = Config(
        llm_provider="keywords", vlm_provider="none", data_dir=tmp_path, mdns=False, adb_watchdog=False
    )
    app = create_app(srv_cfg)
    phone = MockPhone("Telefon babičky", delay=0)
    await _connect_mock_device(app, phone)

    rpi_cfg = RpiConfig(enable_gpio=False, enable_keyboard=False, echo_guard_s=0.0)
    player = FakePlayer(rpi_cfg)
    tts = DummyTts(rpi_cfg)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        client = RpiVoiceButtonClient(rpi_cfg, http_client=http_client, tts=tts, player=player)
        await http_client.post("/api/utterance", json={"text": "Zmizel mi WhatsApp", "source": "button"})
        stop_reply = await client._emergency_stop()
        assert "Zastavila" in stop_reply["say"]
        after = (await http_client.post("/api/utterance", json={"text": "ano", "source": "button"})).json()
        assert phone.apps["com.whatsapp"] == "disabled"
        assert after["screen"] != "hotovo"


async def test_e2e_local_repeat_slower_interceptor_preserves_pending_confirmation(tmp_path):
    """Když se HelpButton zeptá 'WhatsApp je v telefonu vypnutý. Mám ho zapnout a otevřít? Řekněte ano.'
    a dědeček přeslechne a řekne 'Cože? Zopakuj to', Raspberry Pi:
    1. Neodešle 'Cože?' na server (čímž by server zrušil čekající potvrzení CEKA_NA_ANO!).
    2. Zopakuje otázku pomaleji (rate_override=0.68) a hlasitěji (extra_gain=1.30).
    3. Znovu automaticky poslouchá odpověď a po následném 'Ano' WhatsApp úspěšně zapne!
    """
    srv_cfg = Config(
        llm_provider="keywords", vlm_provider="none", data_dir=tmp_path, mdns=False, adb_watchdog=False
    )
    app = create_app(srv_cfg)
    phone = MockPhone("Telefon babičky", delay=0)
    await _connect_mock_device(app, phone)

    rpi_cfg = RpiConfig(
        enable_gpio=False,
        enable_keyboard=False,
        echo_guard_s=0.0,
        silence_tail_s=0.3,
        followup_silence_tail_s=0.3,
        min_speech_s=0.15,
        auto_listen_followups=True,
        rolling_micro_stt=False,
        slow_repeat_rate=0.68,
    )
    player = FakePlayer(rpi_cfg)
    stt = ScriptedStt(
        rpi_cfg,
        [
            ("Zmizel mi WhatsApp", "cs"),
            ("Cože? Zopakuj to prosím", "cs"),
            ("Ano", "cs"),
        ],
    )
    tts = DummyTts(rpi_cfg)

    def fake_mic(_stop_chk) -> Iterable[bytes]:
        return [SILENCE_FRAME] * 5 + [SPEECH_FRAME] * 12 + [SILENCE_FRAME] * 15

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        client = RpiVoiceButtonClient(
            rpi_cfg,
            http_client=http_client,
            stt=stt,
            tts=tts,
            player=player,
            frame_source=fake_mic,
        )
        final_reply = await client.run_interaction()

    assert final_reply is not None
    assert final_reply["screen"] == "hotovo"
    assert phone.apps["com.whatsapp"] == "ok"
    assert sum("je v telefonu vypnut" in s for s in client.spoken_history) == 2
    assert 0.68 in tts.rates_used
    assert any(g > 1.2 for g in player.extra_gains)


async def test_e2e_standalone_edge_failover_when_lan_server_unreachable():
    """Pokud během hackatlonu vypadne Wi-Fi nebo notebook se serverem (ConnectError),
    Raspberry Pi 5 automaticky přepne na svůj vestavěný lokální nouzový HelpButton orchestrátor
    a odpoví seniorovi bez pádu.
    """
    rpi_cfg = RpiConfig(
        server_url="http://127.0.0.1:59999",
        enable_gpio=False,
        enable_keyboard=False,
        echo_guard_s=0.0,
        standalone_fallback=True,
        rolling_micro_stt=False,
    )
    player = FakePlayer(rpi_cfg)
    stt = ScriptedStt(rpi_cfg, [("Někdo mi volá z banky a chce heslo", "cs")])
    tts = DummyTts(rpi_cfg)

    def fake_mic(_stop_chk) -> Iterable[bytes]:
        return [SILENCE_FRAME] * 5 + [SPEECH_FRAME] * 12 + [SILENCE_FRAME] * 15

    client = RpiVoiceButtonClient(
        rpi_cfg,
        stt=stt,
        tts=tts,
        player=player,
        frame_source=fake_mic,
    )
    try:
        reply = await client.run_interaction()
        assert reply is not None
        assert "say" in reply and len(reply["say"]) > 5
    finally:
        await client.close()


def test_mdns_url_prefers_ipv4_and_brackets_ipv6():
    from raspberry_pi.client import server_url_from_addresses

    assert server_url_from_addresses(["fe80::1", "192.168.1.10"], 8765) == "http://192.168.1.10:8765"
    assert server_url_from_addresses(["2001:db8::5"], 8765) == "http://[2001:db8::5]:8765"
    assert server_url_from_addresses([], 8765) is None


async def test_server_403_is_reported_not_silently_failed_over():
    import pytest

    from raspberry_pi.client import ServerRejectedError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "cizí původ požadavku"})

    cfg = RpiConfig()
    cfg.server_url = "http://helpbutton.example.com:8765"
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=cfg.server_url)
    client = RpiVoiceButtonClient(cfg, http_client=http)
    with pytest.raises(ServerRejectedError):
        await client._get_json("/api/state")
    await http.aclose()


async def test_firmware_requests_pass_server_host_check(tmp_path):
    cfg_srv = Config(data_dir=tmp_path, mdns=False, adb_watchdog=False)
    app = create_app(cfg_srv)
    for base in ("http://192.168.1.10:8765", "http://helpbutton-pc.local:8765", "http://helpbutton-local-fallback"):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base) as http:
            r = await http.get("/api/state")
            assert r.status_code == 200, base


def _scripted_client(rpi_cfg, http_client=None, phrases=(), frames=None):
    player = FakePlayer(rpi_cfg)
    stt = ScriptedStt(rpi_cfg, list(phrases))
    tts = DummyTts(rpi_cfg)
    mic = frames or ([SILENCE_FRAME] * 5 + [SPEECH_FRAME] * 12 + [SILENCE_FRAME] * 15)
    client = RpiVoiceButtonClient(
        rpi_cfg, http_client=http_client, stt=stt, tts=tts, player=player, frame_source=lambda _s: list(mic)
    )
    return client, player, tts


def _fast_cfg(**kw) -> RpiConfig:
    base = dict(
        enable_gpio=False,
        enable_keyboard=False,
        echo_guard_s=0.0,
        silence_tail_s=0.3,
        followup_silence_tail_s=0.3,
        min_speech_s=0.15,
        rolling_micro_stt=False,
        working_heartbeat_s=0.0,
    )
    base.update(kw)
    return RpiConfig(**base)


def _speech_frames():
    return [SILENCE_FRAME] * 5 + [SPEECH_FRAME] * 12 + [SILENCE_FRAME] * 15


def test_config_normalizes_paths_urls_and_previously_ignored_keys(tmp_path, monkeypatch):
    from pathlib import Path

    from raspberry_pi.config import PACKAGE_DIR, load_rpi_config, normalize_server_url

    assert normalize_server_url("192.168.1.10") == "http://192.168.1.10:8765"
    assert normalize_server_url("helpbutton-pc.local:9000") == "http://helpbutton-pc.local:9000"
    assert normalize_server_url("http://10.0.0.5:8765/") == "http://10.0.0.5:8765"
    assert normalize_server_url("") == "auto"

    toml = tmp_path / "rpi.toml"
    toml.write_text(
        '[server]\nurl = "192.168.1.50"\n'
        "[button]\nled_pin = false\n"
        '[audio]\nslow_repeat_rate = 0.6\nsample_rate = 48000\nbackend = "ALSA"\n'
        "[vad]\nrolling_stt_pause_s = 0.9\nnear_field_drop_ratio = 0.3\nmax_auto_followups = 4\n"
        '[tts]\npiper_model = "raspberry_pi/voices/x.onnx"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    for env in ("HELPBUTTON_SERVER", "HELPBUTTON_RPI_TTS", "HELPBUTTON_RPI_WHISPER", "HELPBUTTON_RPI_AUDIO_BACKEND"):
        monkeypatch.delenv(env, raising=False)
    cfg = load_rpi_config(toml)
    assert cfg.server_url == "http://192.168.1.50:8765"
    assert cfg.led_pin is None
    assert cfg.slow_repeat_rate == 0.6
    assert cfg.rolling_stt_pause_s == 0.9 and cfg.near_field_drop_ratio == 0.3 and cfg.max_auto_followups == 4
    assert cfg.sample_rate == 16000
    assert cfg.audio_backend == "alsa"
    assert Path(cfg.piper_model) == (PACKAGE_DIR.parent / "raspberry_pi" / "voices" / "x.onnx").resolve()


def test_config_with_broken_toml_falls_back_to_defaults(tmp_path):
    from raspberry_pi.config import load_rpi_config

    bad = tmp_path / "bad.toml"
    bad.write_text("[server\nurl = ", encoding="utf-8")
    cfg = load_rpi_config(bad)
    assert cfg.gpio_pin == 17 and cfg.sample_rate == 16000


def test_entrypoint_explains_missing_venv_instead_of_traceback(monkeypatch):
    import pytest

    import raspberry_pi.__main__ as entry

    monkeypatch.setattr(entry, "_missing_modules", lambda: [])
    entry.ensure_runtime()

    monkeypatch.setattr(entry, "_missing_modules", lambda: ["httpx"])
    monkeypatch.setattr(entry, "_venv_python", lambda: None)
    with pytest.raises(SystemExit) as ei:
        entry.ensure_runtime()
    assert ei.value.code == 1


def test_entrypoint_reexecs_into_project_venv(monkeypatch, tmp_path):
    import raspberry_pi.__main__ as entry

    fake_py = tmp_path / "python"
    fake_py.write_text("")
    calls = {}

    def fake_execve(path, args, env):
        calls["path"], calls["args"], calls["env"] = path, args, env
        raise SystemExit(0)

    def fake_call(args, env=None):
        calls["path"], calls["args"], calls["env"] = args[0], args, env
        return 0

    import subprocess

    monkeypatch.setattr(entry, "_missing_modules", lambda: ["httpx"])
    monkeypatch.setattr(entry, "_venv_python", lambda: fake_py)
    monkeypatch.setattr(entry.os, "execve", fake_execve, raising=False)
    monkeypatch.setattr(subprocess, "call", fake_call)
    monkeypatch.delenv(entry._REEXEC_ENV, raising=False)
    monkeypatch.setattr(entry.sys, "argv", ["raspberry_pi", "--test-audio"])
    import pytest

    with pytest.raises(SystemExit):
        entry.ensure_runtime()
    assert calls["args"][1:] == ["-m", "raspberry_pi", "--test-audio"]
    assert calls["env"][entry._REEXEC_ENV] == "1"
    assert str(entry.REPO_DIR) in calls["env"]["PYTHONPATH"]


async def test_slow_server_reply_is_not_resent_to_local_fallback():
    """Server zpracovává dlouhou akci (ReadTimeout) – příkaz se NESMÍ poslat podruhé záložnímu mozku."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/utterance":
            raise httpx.ReadTimeout("server ještě pracuje", request=request)
        return httpx.Response(200, json={"say": "Poslouchám.", "ts": 1.0})

    cfg = _fast_cfg(server_url="http://192.168.1.10:8765", standalone_fallback=True)
    client, player, _ = _scripted_client(cfg, phrases=[("Zmizel mi WhatsApp", "cs")])
    fallback_used: list[int] = []
    client._get_local_fallback_client = lambda: fallback_used.append(1)  # type: ignore[method-assign]
    client._shared_http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=cfg.server_url)
    reply = await client.run_interaction()
    await client.close()
    assert reply is None
    assert calls.count("/api/utterance") == 1
    assert not fallback_used
    assert any("nemohu spojit" in s for s in client.spoken_history)


async def test_unreachable_server_without_fallback_says_why():
    cfg = _fast_cfg(server_url="http://127.0.0.1:59998", standalone_fallback=False)
    client, player, _ = _scripted_client(cfg, phrases=[("Zmizel mi WhatsApp", "cs")])
    reply = await client.run_interaction()
    await client.close()
    assert reply is None
    assert any("nemohu spojit" in s for s in client.spoken_history)
    assert "low" in player.tones


async def test_known_down_server_goes_straight_to_fallback():
    """Když už víme, že server neodpovídá, nečekáme u každého stisku na timeout připojení."""
    cfg = _fast_cfg(server_url="http://192.168.1.10:8765", standalone_fallback=True)
    client, _, _ = _scripted_client(cfg)
    hits: list[str] = []

    def main_handler(request: httpx.Request) -> httpx.Response:
        hits.append("main")
        return httpx.Response(200, json={})

    def fb_handler(request: httpx.Request) -> httpx.Response:
        hits.append("fallback")
        return httpx.Response(200, json={"say": "ok"})

    client._shared_http = httpx.AsyncClient(
        transport=httpx.MockTransport(main_handler), base_url=cfg.server_url
    )
    fb = httpx.AsyncClient(transport=httpx.MockTransport(fb_handler), base_url="http://helpbutton-local-fallback")
    client._get_local_fallback_client = lambda: fb  # type: ignore[method-assign]
    client._server_ok = False
    await client._post_json("/api/utterance", {"text": "ahoj"})
    assert hits == ["fallback"]
    await fb.aclose()
    await client.close()


async def test_emergency_stop_after_cancel_still_speaks_confirmation(tmp_path):
    import asyncio as _a

    from raspberry_pi.client import ClientState

    srv_cfg = Config(
        llm_provider="keywords", vlm_provider="none", data_dir=tmp_path, mdns=False, adb_watchdog=False
    )
    app = create_app(srv_cfg)
    cfg = _fast_cfg()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as http:
        client, player, _ = _scripted_client(cfg, http_client=http)

        async def _long_session():
            client.state = ClientState.PROCESSING
            await _a.sleep(30)

        client._session_task = _a.create_task(_long_session())
        await _a.sleep(0)
        client._press_started_at = 0.0
        client._on_button_pressed()
        await _a.wait_for(client._session_task, timeout=5)
        assert any("Zastavila" in s for s in client.spoken_history)
        assert "double" in player.tones
        assert client.state == ClientState.IDLE and not client._cancel_flag


async def test_press_during_greeting_skips_greeting_instead_of_stopping():
    from raspberry_pi.client import ClientState

    client, _, _ = _scripted_client(_fast_cfg())
    client.state = ClientState.PROMPTING
    client._press_started_at = 0.0
    client._on_button_pressed()
    assert client._skip_prompt is True
    assert client._session_task is None


async def test_microphone_failure_is_announced():
    cfg = _fast_cfg(server_url="http://127.0.0.1:59997", standalone_fallback=False)
    player = FakePlayer(cfg)

    def broken_mic(_stop):
        raise RuntimeError("Nebyl nalezen žádný funkční zvukový vstup")

    client = RpiVoiceButtonClient(
        cfg, stt=ScriptedStt(cfg, []), tts=DummyTts(cfg), player=player, frame_source=broken_mic
    )
    assert await client.run_interaction() is None
    await client.close()
    assert any("mikrofon" in s.lower() for s in client.spoken_history)


async def test_working_heartbeat_survives_server_progress_messages():
    import asyncio as _a

    from raspberry_pi.client import ClientState

    cfg = _fast_cfg(working_heartbeat_s=0.02)
    client, player, _ = _scripted_client(cfg)
    client.state = ClientState.PROCESSING
    hb = _a.create_task(client._working_heartbeat_loop())
    await _a.sleep(0.06)
    client.state = ClientState.SPEAKING
    await _a.sleep(0.06)
    client.state = ClientState.PROCESSING
    ticks_before = player.tones.count("working_tick")
    await _a.sleep(0.15)
    hb.cancel()
    assert player.tones.count("working_tick") > ticks_before


async def test_raw_audio_reply_tone_is_not_played_twice():
    wav = synthesize_tone_wav("double")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/button/audio":
            return httpx.Response(
                200,
                content=wav,
                headers={
                    "content-type": "audio/wav",
                    "x-helpbutton-led": "green",
                    "x-helpbutton-tone": "double",
                    "x-helpbutton-say": "Hotovo",
                    "x-helpbutton-expects": "0",
                },
            )
        return httpx.Response(200, json={"say": "", "ts": 2.0})

    cfg = _fast_cfg(stt_mode="server")
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://192.168.1.10:8765")
    player = FakePlayer(cfg)
    client = RpiVoiceButtonClient(
        cfg,
        http_client=http,
        stt=SpeechToText(cfg),
        tts=DummyTts(cfg),
        player=player,
        frame_source=lambda _s: _speech_frames(),
    )
    reply = await client.run_interaction()
    await http.aclose()
    assert reply is not None
    assert player.tones.count("double") == 1


async def test_raw_audio_server_error_is_reported():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/button/audio":
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json={"say": "", "ts": 3.0})

    cfg = _fast_cfg(stt_mode="server")
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://192.168.1.10:8765")
    client = RpiVoiceButtonClient(
        cfg,
        http_client=http,
        stt=SpeechToText(cfg),
        tts=DummyTts(cfg),
        player=FakePlayer(cfg),
        frame_source=lambda _s: _speech_frames(),
    )
    assert await client.run_interaction() is None
    await http.aclose()
    assert any("nemohu spojit" in s for s in client.spoken_history)


def test_player_stop_cancels_queued_sounds():
    import threading
    import time as _t

    player = AudioPlayer(RpiConfig())
    played: list[int] = []
    started = threading.Event()
    release = threading.Event()

    def fake_play_any(wav):
        played.append(len(played))
        started.set()
        release.wait(2)

    player._play_any = fake_play_any  # type: ignore[method-assign]
    wav = synthesize_tone_wav("beep")
    t1 = threading.Thread(target=player.play_wav_bytes, args=(wav,))
    t1.start()
    started.wait(2)
    t2 = threading.Thread(target=player.play_wav_bytes, args=(wav,))
    t2.start()
    _t.sleep(0.05)
    player.stop()
    release.set()
    t1.join(2)
    t2.join(2)
    assert played == [0]


def test_player_reports_errors_instead_of_silently_failing():
    player = AudioPlayer(RpiConfig())

    def boom(_wav):
        raise RuntimeError("aplay: device busy")

    player._play_any = boom  # type: ignore[method-assign]
    player.play_tone("beep")
    assert player.last_error and "busy" in player.last_error
    assert not player.is_playing


def test_microphone_stall_is_detected_and_raises():
    import pytest

    from raspberry_pi.audio_io import MicrophoneStream

    fake = _FakeSd(b"", b"")
    fake.stall = True
    mic = MicrophoneStream(RpiConfig())
    mic.STALL_TIMEOUT_S = 0.2
    with pytest.raises(RuntimeError, match="neposlal"):
        for _ in mic._iter_sounddevice(fake, HAT_DEVICES[1], None):
            pass


async def test_stt_model_load_failure_falls_back_to_server_stt(monkeypatch):
    import importlib.util as ilu
    import sys as _sys
    import types

    fake_fw = types.ModuleType("faster_whisper")

    def _boom(*a, **k):
        raise OSError("model 'small' nelze stáhnout (offline)")

    fake_fw.WhisperModel = _boom
    monkeypatch.setitem(_sys.modules, "faster_whisper", fake_fw)
    real_find = ilu.find_spec
    monkeypatch.setattr(
        ilu, "find_spec", lambda name, *a: object() if name == "faster_whisper" else real_find(name, *a)
    )
    stt = SpeechToText(RpiConfig())
    assert stt.local_available
    await stt.warmup()
    assert not stt.local_available and "offline" in (stt.load_error or "")


async def test_piper_13_api_is_used_without_cli(tmp_path, monkeypatch):
    import importlib.util as ilu
    import io as _io
    import sys as _sys
    import types
    import wave as _wave

    from raspberry_pi.tts import TextToSpeech

    class Voice13:
        cfg_used = None

        def synthesize_wav(self, text, wav_file, syn_config=None):
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(22050)
            wav_file.writeframes(b"\x00\x01" * 2205)
            Voice13.cfg_used = syn_config

    class SynCfg:
        def __init__(self, length_scale=None):
            self.length_scale = length_scale

    fake_piper = types.ModuleType("piper")
    fake_piper.PiperVoice = types.SimpleNamespace(load=lambda path: Voice13())
    fake_piper.SynthesisConfig = SynCfg
    monkeypatch.setitem(_sys.modules, "piper", fake_piper)
    real_find = ilu.find_spec
    monkeypatch.setattr(
        ilu, "find_spec", lambda name, *a: object() if name == "piper" else real_find(name, *a)
    )
    model = tmp_path / "v.onnx"
    model.write_bytes(b"x")
    cfg = RpiConfig(
        piper_model=str(model), piper_bin="does-not-exist", cache_dir=tmp_path / "c", tts_engine="piper"
    )
    tts = TextToSpeech(cfg)
    wav = await tts._synth_piper("Dobrý den", 0.85)
    assert wav and wav[:4] == b"RIFF"
    assert not tts._piper_py_failed
    with _wave.open(_io.BytesIO(wav)) as w:
        assert w.getframerate() == 22050
    assert abs(Voice13.cfg_used.length_scale - 1 / 0.85) < 1e-6


def test_espeak_streamed_wav_header_is_fixed():
    import io as _io
    import struct
    import wave as _wave

    from raspberry_pi.tts import _fix_streamed_wav

    good = synthesize_tone_wav("beep")
    broken = bytearray(good)
    struct.pack_into("<I", broken, 4, 0x7FFFFFFF)
    struct.pack_into("<I", broken, 40, 0x7FFFFFFF)
    fixed = _fix_streamed_wav(bytes(broken))
    with _wave.open(_io.BytesIO(fixed)) as w:
        assert w.getnframes() * 2 == len(good) - 44


def test_button_without_gpio_or_tty_reports_no_input():
    import asyncio as _a

    from raspberry_pi.button_hw import ButtonController

    btn = ButtonController(RpiConfig(enable_gpio=False, enable_keyboard=False), lambda: None)
    loop = _a.new_event_loop()
    btn.start(loop)
    assert btn.has_input is False
    btn.stop()
    loop.close()


def _sine(freq: float, amp: float, rate: int, seconds: float) -> list[float]:
    n = int(rate * seconds)
    return [amp * math.sin(2.0 * math.pi * freq * i / rate) for i in range(n)]


def _pcm(values) -> bytes:
    arr = array.array("h", (max(-32767, min(32767, int(v))) for v in values))
    if sys.byteorder != "little":
        arr.byteswap()
    return arr.tobytes()


def _rms(values) -> float:
    vals = list(values)
    return math.sqrt(sum(v * v for v in vals) / max(1, len(vals)))


def _speechlike(rate: int, seconds: float, peak: float = 30000.0) -> bytes:
    """Slabiky (4 Hz obálka) z harmonických – podobné řeči z Piperu, normalizované na špičku."""
    n = int(rate * seconds)
    vals = []
    for i in range(n):
        t = i / rate
        env = 0.15 + 0.85 * abs(math.sin(math.pi * 4.0 * t)) ** 3
        v = sum(math.sin(2 * math.pi * f * t) / k for k, f in enumerate((180, 360, 720, 1440, 2880), 1))
        vals.append(env * v)
    m = max(abs(v) for v in vals)
    return _pcm(v * peak / m for v in vals)


def _samples(pcm: bytes) -> list[int]:
    return list(array.array("h", pcm))


def test_cleanup_gain_is_smooth_not_pumping():
    """Dřív: řečové rámce ×AGC, tiché rámce ×0,32 -> skoky až 20× každých 20 ms (rozsekaný zvuk)."""
    rate, frame = 16000, 320
    vals: list[float] = []
    for k in range(60):
        amp = 3000.0 if k % 2 == 0 else 60.0
        vals += [amp * math.sin(2 * math.pi * 300 * (k * frame + i) / rate) for i in range(frame)]
    pcm = _pcm(vals)
    out = _samples(
        apply_mic_speech_cleanup_pcm16(
            pcm, rate=rate, bandpass=False, transient_filter=False, noise_floor_rms=100.0, max_gain=6.0
        )
    )
    loud_gain = [
        _rms(out[k * frame : (k + 1) * frame]) / _rms(vals[k * frame : (k + 1) * frame])
        for k in range(2, 58, 2)
    ]
    quiet_gain = [
        _rms(out[k * frame : (k + 1) * frame]) / _rms(vals[k * frame : (k + 1) * frame])
        for k in range(3, 58, 2)
    ]
    ratio = (sum(loud_gain) / len(loud_gain)) / (sum(quiet_gain) / len(quiet_gain))
    assert ratio <= 2.05
    assert max(abs(v) for v in out) <= 24000


def test_click_filter_keeps_plosive_consonants():
    """Výbuch souhlásky (p/t/k) těsně před samohláskou se nesmí ztlumit – dřív se „uřízl“."""
    rate = 16000
    silence = [20.0 * math.sin(i) for i in range(int(rate * 0.2))]
    burst = [9000.0 if i % 2 == 0 else -9000.0 for i in range(64)]
    vowel = _sine(300, 3000, rate, 0.2)
    pcm = _pcm(silence + burst + vowel)
    out = _samples(suppress_transients_pcm16(pcm, rate=rate))
    start = len(silence)
    assert max(abs(v) for v in out[start : start + 64]) >= 0.7 * 9000


def test_clarity_output_is_louder_without_clipping_or_distortion():
    np = __import__("pytest").importorskip("numpy")
    rate = 22050
    rng = np.random.default_rng(0)
    t = np.arange(rate * 3) / rate
    env = (np.abs(np.sin(np.pi * 3.3 * t)) ** 2) * (0.3 + 0.7 * np.abs(np.sin(np.pi * 0.7 * t)))
    sig = np.convolve(rng.normal(0, 1, t.size), np.hanning(30), mode="same") * env
    speech = (sig / np.abs(sig).max() * 30000).astype("<i2").tobytes()
    loud = apply_senior_clarity_dsp_pcm16(speech, rate=rate, loudness=True)
    x = np.frombuffer(speech, dtype="<i2").astype(float)
    y = np.frombuffer(loud, dtype="<i2").astype(float)
    assert np.sqrt(np.mean(y**2)) >= 1.6 * np.sqrt(np.mean(x**2))
    assert np.max(np.abs(y)) <= 31001 and np.count_nonzero(np.abs(y) >= 32767) == 0

    tone = _pcm(_sine(3000, 30000, rate, 0.5))
    out = np.frombuffer(
        apply_senior_clarity_dsp_pcm16(tone, rate=rate, presence_gain=1.55, extra_gain=1.4), dtype="<i2"
    ).astype(float)[2000:]
    spec = np.abs(np.fft.rfft(out * np.hanning(out.size)))
    freqs = np.fft.rfftfreq(out.size, 1 / rate)
    fund = spec[np.abs(freqs - 3000) < 60].max()
    harm = spec[np.abs(freqs - 9000) < 60].max()
    assert harm / fund < 0.001
    assert np.max(np.abs(out)) <= 31001


def test_fir_decimator_removes_aliasing():
    np = __import__("pytest").importorskip("numpy")
    from raspberry_pi.audio_io import Fir3Decimator

    def run(freq: float) -> tuple[float, float]:
        sig = _pcm(_sine(freq, 8000, 48000, 0.5))
        dec = Fir3Decimator()
        out = b"".join(dec.process(sig[i : i + 2880]) for i in range(0, len(sig), 2880))
        box = resample_pcm16_mono(sig, 48000, 16000)
        y = np.frombuffer(out, dtype="<i2").astype(float)[100:]
        b = np.frombuffer(box, dtype="<i2").astype(float)[100:]
        return float(np.sqrt(np.mean(y**2))) / 5657.0, float(np.sqrt(np.mean(b**2))) / 5657.0

    passed, _ = run(1000)
    alias_fir, alias_box = run(10000)
    assert passed >= 0.95
    assert alias_fir < 0.05 and alias_box > 0.3


def test_numpy_and_pure_python_dsp_give_same_result(monkeypatch):
    np = __import__("pytest").importorskip("numpy")
    import raspberry_pi.audio_io as aio

    rng = np.random.default_rng(1)
    base = np.frombuffer(_speechlike(16000, 1.0, peak=9000), dtype="<i2").astype(float)
    noisy = np.clip(base + rng.normal(0, 150, base.size), -32767, 32767).astype("<i2")
    noisy[4000:4003] = [18000, -16000, 12000]
    pcm = noisy.tobytes()
    speech22 = _speechlike(22050, 1.0, peak=26000)

    def run_all():
        return (
            aio.apply_mic_speech_cleanup_pcm16(pcm, rate=16000, noise_floor_rms=120.0),
            aio.suppress_transients_pcm16(pcm, rate=16000),
            aio.apply_senior_clarity_dsp_pcm16(speech22, rate=22050, loudness=True),
            aio.apply_senior_clarity_dsp_pcm16(speech22, rate=22050, extra_gain=1.3),
        )

    fast = run_all()
    monkeypatch.setattr(aio, "_np", None)
    monkeypatch.setattr(aio, "_lfilter", None)
    slow = run_all()
    for a, b in zip(fast, slow):
        assert len(a) == len(b)
        diff = np.abs(np.frombuffer(a, dtype="<i2").astype(int) - np.frombuffer(b, dtype="<i2").astype(int))
        assert diff.max() <= 2


class _FakeWhisper:
    """Zástupce faster-whisper modelu: zaznamená parametry a vrací předem dané přepisy."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[tuple[object, dict]] = []

    def transcribe(self, audio, **kw):
        import types

        self.calls.append((audio, kw))
        lang, prob, text = self.script.pop(0)
        info = types.SimpleNamespace(language=kw.get("language") or lang, language_probability=prob)
        return iter([types.SimpleNamespace(text=text)] if text else []), info


def _local_stt(cfg: RpiConfig, model) -> SpeechToText:
    class _LocalStt(SpeechToText):
        @property
        def local_available(self) -> bool:
            return True

    stt = _LocalStt(cfg)
    stt._model = model
    return stt


def _utt_wav(seconds: float = 0.9) -> bytes:
    return pcm_to_wav(_pcm(_sine(220, 3000, 16000, seconds)), 16000)


async def test_stt_forces_czech_with_vocabulary_and_fast_options():
    np = __import__("pytest").importorskip("numpy")
    cfg = RpiConfig(stt_vocabulary=["Denis"])
    model = _FakeWhisper([("cs", 1.0, " Napiš Denisovi.")])
    stt = _local_stt(cfg, model)
    stt.set_context_words(["Lenka", "Lence"])
    res = await stt.transcribe_wav(_utt_wav())
    assert res.text == "Napiš Denisovi."
    audio, kw = model.calls[0]
    assert isinstance(audio, np.ndarray) and audio.dtype == np.float32
    assert kw["language"] == "cs"
    assert kw["without_timestamps"] is True and kw["vad_filter"] is False
    assert "Denis" in kw["hotwords"] and "Lence" in kw["hotwords"] and "WhatsApp" in kw["hotwords"]
    assert len(model.calls) == 1


async def test_stt_auto_mode_rejects_wrong_language_and_keeps_english():
    cfg = RpiConfig(whisper_language="auto")
    model = _FakeWhisper([("pl", 0.95, "zła odpowiedź"), ("cs", 1.0, "Zmizel mi WhatsApp.")])
    res = await _local_stt(cfg, model).transcribe_wav(_utt_wav())
    assert res.text == "Zmizel mi WhatsApp." and res.language == "cs"
    assert model.calls[0][1]["language"] is None and model.calls[1][1]["language"] == "cs"

    model_en = _FakeWhisper([("en", 0.92, "My WhatsApp disappeared")])
    res_en = await _local_stt(cfg, model_en).transcribe_wav(_utt_wav())
    assert res_en.text == "Zmizel mi WhatsApp" and res_en.translated and len(model_en.calls) == 1


async def test_stt_retries_instead_of_saying_it_did_not_understand():
    from raspberry_pi.stt import SENIOR_PROMPT_CS

    model = _FakeWhisper([("cs", 1.0, ""), ("cs", 1.0, "Napiš Denisovi.")])
    res = await _local_stt(RpiConfig(), model).transcribe_wav(_utt_wav())
    assert res.text == "Napiš Denisovi."
    retry_kw = model.calls[1][1]
    assert retry_kw["no_speech_threshold"] is None and retry_kw["vad_filter"] is False
    assert retry_kw["initial_prompt"] is None

    leak = " ".join(SENIOR_PROMPT_CS.split()[:9])
    model2 = _FakeWhisper([("cs", 1.0, leak), ("cs", 1.0, "Otevři fotky.")])
    res2 = await _local_stt(RpiConfig(), model2).transcribe_wav(_utt_wav())
    assert res2.text == "Otevři fotky."


async def test_stt_cancelled_background_job_does_no_work():
    import threading

    model = _FakeWhisper([("cs", 1.0, "nemá se volat")])
    ev = threading.Event()
    ev.set()
    res = await _local_stt(RpiConfig(), model).transcribe_wav(_utt_wav(), cancel_event=ev)
    assert res.text == "" and model.calls == []


async def test_czech_sentence_is_never_sent_to_online_translation(monkeypatch):
    import raspberry_pi.stt as stt_mod

    async def boom(*a, **k):
        raise AssertionError("online překlad se nesmí volat pro češtinu")

    monkeypatch.setattr(stt_mod, "_online_translate", boom)
    for text in ("Napiš na WhatsApp, že přijdu", "My jsme doma", "Zmizel mi WhatsApp", "Stop"):
        out, tr = await translate_to_czech(text, "cs")
        assert not tr


def test_url_in_command_is_not_a_hallucination_and_completeness_tiers():
    assert not is_whisper_hallucination("Otevři www.seznam.cz")
    assert is_whisper_hallucination("www.seznam.cz")
    assert classify_utterance_completeness("Napiš Denisovi.") == "likely_complete"
    assert classify_utterance_completeness("Napiš Denisovi") == "continue"
    assert classify_utterance_completeness("Napiš Denisovi, že") == "hesitation"


def test_vad_background_transcription_shortens_tail_and_reports_pending():
    from raspberry_pi.audio_io import PAUSE_PENDING

    cfg = RpiConfig(
        silence_tail_s=2.2,
        rolling_stt_pause_s=0.3,
        likely_complete_tail_s=0.6,
        min_speech_s=0.3,
        min_listen_s=0.0,
    )
    stream = [SILENCE_FRAME] * 5 + [SPEECH_FRAME] * 20 + [SILENCE_FRAME] * 100

    polls = {"n": 0}

    def poll():
        polls["n"] += 1
        return None if polls["n"] < 4 else ("likely_complete", "Napiš Denisovi.", "cs")

    utt = process_vad_stream(stream, cfg, on_pause_check=lambda _p: PAUSE_PENDING, poll_pause_check=poll)
    assert utt.pre_transcribed_text == "Napiš Denisovi."
    assert utt.duration_s < 1.6

    utt2 = process_vad_stream(
        stream, cfg, on_pause_check=lambda _p: PAUSE_PENDING, poll_pause_check=lambda: None
    )
    assert utt2.pre_transcription_pending and not utt2.pre_transcribed_text

    cancels = {"n": 0}
    resumed = (
        [SILENCE_FRAME] * 5
        + [SPEECH_FRAME] * 20
        + [SILENCE_FRAME] * 12
        + [SPEECH_FRAME] * 15
        + [SILENCE_FRAME] * 100
    )
    process_vad_stream(
        resumed,
        cfg,
        on_pause_check=lambda _p: PAUSE_PENDING,
        poll_pause_check=lambda: None,
        on_pause_cancel=lambda: cancels.__setitem__("n", cancels["n"] + 1),
    )
    assert cancels["n"] == 1


def test_stt_audio_is_trimmed_after_speech():
    cfg = RpiConfig(silence_tail_s=2.2, min_speech_s=0.3, speech_trim_tail_ms=300)
    utt = process_vad_stream([SILENCE_FRAME] * 5 + [SPEECH_FRAME] * 20 + [SILENCE_FRAME] * 100, cfg)
    assert len(utt.stt_pcm_bytes) == 35 * 960
    assert len(utt.pcm_bytes) > len(utt.stt_pcm_bytes)
    assert utt.stt_wav_bytes[:4] == b"RIFF"


async def test_client_reuses_background_transcription_without_second_pass():
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        if request.url.path == "/api/utterance":
            sent.append(_json.loads(request.content)["text"])
            return httpx.Response(200, json={"say": "Dobře.", "ts": 2.0})
        return httpx.Response(200, json={"say": "Poslouchám.", "ts": 1.0})

    cfg = _fast_cfg(rolling_micro_stt=True, rolling_stt_pause_s=0.3, silence_tail_s=1.0)

    class CountingStt(ScriptedStt):
        calls = 0

        async def transcribe_wav(self, wav_bytes, extra_prompt="", *, is_followup=False):
            CountingStt.calls += 1
            return await super().transcribe_wav(wav_bytes, extra_prompt, is_followup=is_followup)

    stt = CountingStt(cfg, [("Napiš Denisovi.", "cs")])
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://192.168.1.10:8765")
    client = RpiVoiceButtonClient(
        cfg,
        http_client=http,
        stt=stt,
        tts=DummyTts(cfg),
        player=FakePlayer(cfg),
        frame_source=lambda _s: [SILENCE_FRAME] * 5 + [SPEECH_FRAME] * 12 + [SILENCE_FRAME] * 40,
    )
    reply = await client.run_interaction()
    await http.aclose()
    assert reply is not None and sent == ["Napiš Denisovi."]
    assert CountingStt.calls == 1


def test_client_passes_family_names_to_stt_and_config_parses_stt_options(tmp_path, monkeypatch):
    from raspberry_pi.config import load_rpi_config

    cfg = RpiConfig()
    client = RpiVoiceButtonClient(cfg, stt=SpeechToText(cfg), tts=DummyTts(cfg), player=FakePlayer(cfg))
    client._update_stt_context({"family": "Lenka", "family_dative": "Lence"})
    assert {"Lenka", "Lence"} <= set(client.stt.vocabulary())

    toml = tmp_path / "rpi.toml"
    toml.write_text(
        '[stt]\nlanguage = "AUTO"\nvocabulary = "Denis, Lenka"\n[audio]\noutput_loudness = false\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("HELPBUTTON_RPI_LANGUAGE", raising=False)
    loaded = load_rpi_config(toml)
    assert loaded.whisper_language == "auto"
    assert loaded.stt_vocabulary == ["Denis", "Lenka"]
    assert loaded.output_loudness is False


def test_stereo_to_mono_handles_dead_channel_and_phase_inversion():
    from raspberry_pi.audio_io import stereo_to_mono_pcm16

    mono = make_frame(amplitude=4000, freq=260.0, rate=16000, samples_per_frame=480)
    src = array.array("h", mono)

    dead_r = array.array("h", bytes(len(src) * 4))
    dead_r[0::2] = src
    dead_r[1::2] = array.array("h", [0] * len(src))
    out_dead = stereo_to_mono_pcm16(dead_r.tobytes())
    assert abs(pcm16_rms(out_dead) - pcm16_rms(mono)) < 5.0

    inv = array.array("h", bytes(len(src) * 4))
    inv[0::2] = src
    inv[1::2] = array.array("h", [-s for s in src])
    out_inv = stereo_to_mono_pcm16(inv.tobytes())
    assert abs(pcm16_rms(out_inv) - pcm16_rms(mono)) < 5.0


def test_click_filter_preserves_plosives_with_vot_gap_and_final_consonants():
    """Souhláska p/t/k s 20ms aspirační mezerou (VOT) před samohláskou i koncová souhláska
    po samohlásce musí zůstat 100% neporušená."""
    rate = 16000
    silence = [20.0 * math.sin(i) for i in range(int(rate * 0.1))]
    plosive_start = [11000.0 if i % 2 == 0 else -11000.0 for i in range(64)]
    vot_gap = [120.0 * math.sin(i * 0.7) for i in range(int(rate * 0.018))]
    vowel = _sine(280, 3200, rate, 0.15)
    final_plosive = [9500.0 if i % 2 == 0 else -9500.0 for i in range(64)]
    pcm = _pcm(silence + plosive_start + vot_gap + vowel + vot_gap + final_plosive + silence)
    out = _samples(suppress_transients_pcm16(pcm, rate=rate))

    idx_start = len(silence)
    idx_final = len(silence) + len(plosive_start) + len(vot_gap) + len(vowel) + len(vot_gap)
    assert max(abs(v) for v in out[idx_start : idx_start + 64]) >= 0.95 * 11000
    assert max(abs(v) for v in out[idx_final : idx_final + 64]) >= 0.95 * 9500


def test_pop_immune_agc_amplifies_quiet_speech_despite_loud_initial_mouth_pop():
    """Jediné hlasité lupnutí úst na začátku nesmí zablokovat AGC pro zbytek tiché věty,
    a vysoké frekvence sykavek (5,5 kHz) nesmí být utlumeny."""
    rate = 16000
    pop_chunk = _sine(180, 22000, rate, 0.02)
    quiet_voice = _sine(320, 700, rate, 0.80)
    pcm = _pcm(pop_chunk + quiet_voice)
    cleaned = apply_mic_speech_cleanup_pcm16(
        pcm,
        rate=rate,
        agc_enabled=True,
        target_rms=4800.0,
        max_gain=6.0,
        transient_filter=False,
    )
    out = _samples(cleaned)
    tail_rms = _rms(out[int(rate * 0.15) :])
    orig_tail_rms = _rms(_samples(_pcm(quiet_voice))[int(rate * 0.13) :])
    assert tail_rms > orig_tail_rms * 3.5
    assert max(abs(v) for v in out) <= 24000

    sibilant = make_frame(amplitude=2000, freq=5500.0, rate=16000, samples_per_frame=1600)
    clean_sib = apply_mic_speech_cleanup_pcm16(
        sibilant, rate=16000, agc_enabled=False, noise_subtraction=False
    )
    assert pcm16_rms(clean_sib) >= pcm16_rms(sibilant) * 0.92


def test_vad_loud_start_or_pop_does_not_cut_off_quieter_second_half_of_sentence():
    """Hlasitý začátek věty (nebo fouknutí do mikrofonu) nesmí uzamknout práh VAD tak vysoko,
    aby se normální/tišší druhá polovina věty uřízla."""
    cfg = RpiConfig(
        pre_roll_ms=300,
        silence_tail_s=0.9,
        min_speech_s=0.3,
        speech_trim_tail_ms=300,
        near_field_tv_rejection=True,
        near_field_drop_ratio=0.22,
    )
    loud_opening = make_frame(amplitude=14000, freq=200.0)
    normal_continuation = make_frame(amplitude=900, freq=220.0)
    soft_ending = make_frame(amplitude=260, freq=300.0)

    stream = (
        [SILENCE_FRAME] * 10
        + [loud_opening] * 6
        + [normal_continuation] * 35
        + [soft_ending] * 4
        + [SILENCE_FRAME] * 40
    )
    utt = process_vad_stream(stream, cfg)
    assert utt.has_speech
    assert utt.speech_duration_s >= 1.15
    expected_min_frames = 10 + 6 + 35 + 4
    assert len(utt.stt_pcm_bytes) >= expected_min_frames * 960


def test_vad_short_blip_reset_preserves_preroll_for_subsequent_speech():
    """Krátké lupnutí/nádech (< min_speech_s) následované 1s tichem nesmí smazat pre-roll
    pro skutečné slovo, které přijde hned poté."""
    cfg = RpiConfig(pre_roll_ms=300, min_speech_s=0.30, silence_tail_s=0.6, min_listen_s=0.0)
    stream = (
        [SILENCE_FRAME] * 10
        + [SPEECH_FRAME] * 2
        + [SILENCE_FRAME] * 35
        + [SPEECH_FRAME] * 15
        + [SILENCE_FRAME] * 25
    )
    utt = process_vad_stream(stream, cfg)
    assert utt.has_speech
    assert len(utt.pcm_bytes) == 44 * 960


async def test_stt_fast_single_pass_greedy_warmup_and_non_blocking_speculative_pause():
    import pytest

    pytest.importorskip("numpy")
    cfg = RpiConfig()
    assert cfg.whisper_beam_size == 1
    model = _FakeWhisper([("cs", 1.0, ""), ("cs", 1.0, "Otevři Seznam.")])
    stt = _local_stt(cfg, model)

    await stt.warmup()
    assert len(model.calls) == 1
    warm_kw = model.calls[0][1]
    assert warm_kw["beam_size"] == 1 and warm_kw["temperature"] == 0.0
    assert warm_kw["initial_prompt"] and "WhatsApp" in warm_kw["initial_prompt"]

    res = await stt.transcribe_wav(_utt_wav(0.9))
    assert res.text == "Otevři Seznam."
    call_kw = model.calls[1][1]
    assert call_kw["beam_size"] == 1 and call_kw["best_of"] == 1 and call_kw["temperature"] == 0.0

    with stt._run_lock:
        spec_res = await stt.transcribe_wav(_utt_wav(0.6), fast_speculative=True)
    assert spec_res.text == ""
    assert len(model.calls) == 2

    assert classify_utterance_completeness("Zmizel mi.") == "hesitation"
    assert classify_utterance_completeness("Chtěl bych.") == "hesitation"
    assert classify_utterance_completeness("Otevři mi.") == "hesitation"


async def test_server_voice_eager_warmup_and_fast_float32_mic_pipeline(monkeypatch):
    np = __import__("pytest").importorskip("numpy")
    import helpbutton.server.voice as srv_voice

    cfg = Config()
    cfg.contacts = [Contact("Lenka", phone="+420777123456", aliases=["lenka"], dative="Lence")]
    v = srv_voice.Voice(cfg)
    fake_model = _FakeWhisper([("cs", 1.0, ""), ("cs", 1.0, "Zmizel mi votsap")])
    monkeypatch.setattr(srv_voice.Voice, "stt_available", property(lambda self: True))
    monkeypatch.setattr(v, "_model", fake_model)

    await v.warmup()
    assert len(fake_model.calls) == 1
    assert fake_model.calls[0][1]["beam_size"] == 1
    assert fake_model.calls[0][1]["temperature"] == 0.0

    mono_48k = make_frame(amplitude=1200, freq=260.0, rate=48000, samples_per_frame=48000)
    src = array.array("h", mono_48k)
    stereo = array.array("h", bytes(len(src) * 4))
    stereo[0::2] = src
    wav_48k = pcm_to_wav(stereo.tobytes(), rate=48000, channels=2)

    text = await v.transcribe(wav_48k, content_type="audio/wav", rate=48000)
    assert text == "Zmizel mi WhatsApp"
    audio_arg, kw = fake_model.calls[1]
    assert isinstance(audio_arg, np.ndarray) and audio_arg.dtype == np.float32
    assert kw["beam_size"] == 1 and kw["temperature"] == 0.0 and kw["vad_filter"] is False
    assert "Lence" in kw["hotwords"]


def test_clarity_dsp_and_48khz_resampling_have_zero_intersample_clipping():
    """Výstupní DSP + převzorkování 22050 -> 48000 Hz nesmí přetéct 16bit rozsah ani lupat na okraji."""
    import numpy as np

    rate = 22050
    t = np.arange(int(rate * 1.0)) / rate
    speech = (
        8000 * np.sin(2 * math.pi * 190 * t) * np.exp(-(((t - 0.25) / 0.12) ** 2))
        + 5000 * np.sin(2 * math.pi * 420 * t) * np.exp(-(((t - 0.25) / 0.10) ** 2))
        + 2200 * np.sin(2 * math.pi * 3400 * t) * np.exp(-(((t - 0.65) / 0.08) ** 2))
        + 1200 * np.sin(2 * math.pi * 260 * t)
    )
    pcm = np.clip(np.rint(speech), -32767, 32767).astype("<i2").tobytes()
    for eg in (1.0, 1.3):
        dsp = apply_senior_clarity_dsp_pcm16(pcm, rate=rate, extra_gain=eg, eq=True, loudness=True)
        up = resample_pcm16_hq(dsp, 22050, 48000)
        s48 = array.array("h")
        s48.frombytes(up)
        assert max(abs(v) for v in s48) < 32000
        assert sum(1 for v in s48 if abs(v) >= 32767) == 0

    edge_pcm = make_frame(amplitude=12000, freq=210.0, rate=22050, samples_per_frame=2205)
    up_edge = resample_pcm16_hq(edge_pcm, 22050, 48000)
    s_edge = array.array("h")
    s_edge.frombytes(up_edge)
    assert abs(s_edge[0]) < 1500


def test_audio_player_prepare_output_wav_resamples_to_48k_with_fades_and_tail():
    """Výstup pro aplay se čistě převzorkuje na 48 kHz s jemným náběhem/doběhem a tichým koncem."""
    import io
    import wave

    raw = make_frame(amplitude=9000, freq=300.0, rate=22050, samples_per_frame=2205)
    wav_in = pcm_to_wav(raw, rate=22050, channels=1, width=2)
    wav_out = AudioPlayer._prepare_output_wav(
        wav_in, target_rate=48000, target_channels=2, tail_silence_ms=35
    )
    with wave.open(io.BytesIO(wav_out), "rb") as w:
        assert w.getframerate() == 48000
        assert w.getnchannels() == 2
        frames = w.readframes(w.getnframes())
    samples = array.array("h")
    samples.frombytes(frames)
    assert abs(samples[0]) < 300
    tail_samples = int(48000 * 0.030) * 2
    assert all(v == 0 for v in samples[-tail_samples:])


def test_vad_ignores_dc_offset_and_50hz_hum_and_captures_crisp_ano_ne_instantly():
    """VAD odfiltruje DC posun i 50Hz brum, nezamrzne na nižším prahu šumu a bleskově zachytí 'ne'."""
    cfg = RpiConfig()
    vad = VadDetector(cfg)

    dc_hum = array.array(
        "h",
        (
            max(-32767, min(32767, round(1800 + 180 * math.sin(2 * math.pi * 50 * i / 16000))))
            for i in range(480)
        ),
    ).tobytes()
    speaking, rms = vad.is_speech(dc_hum, 16000)
    assert not speaking and rms < 160.0

    room_noise = make_frame(amplitude=310, freq=180.0, rate=16000, samples_per_frame=480)
    for _ in range(20):
        vad.calibrate_frame(room_noise, 16000)
    assert vad.noise_floor > 140.0

    stream = [SILENCE_FRAME] * 8 + [SPEECH_FRAME] * 6 + [SILENCE_FRAME] * 40
    utt = process_vad_stream(stream, cfg, is_followup=True)
    assert utt.has_speech
    assert utt.speech_duration_s >= 0.15
    assert utt.duration_s <= 0.95


async def test_stt_tries_local_files_only_before_online_hf_and_warms_up_fast(monkeypatch):
    """Cached faster-whisper model se načítá offline (`local_files_only=True`) a warmup používá 0,25 s audio."""
    import importlib.machinery
    import types

    calls: list[tuple[str, dict]] = []
    fake_model = _FakeWhisper([("cs", 1.0, "")])

    def _whisper_ctor(model_name, **kwargs):
        calls.append((model_name, dict(kwargs)))
        if kwargs.get("local_files_only") and model_name == "missing-local":
            raise FileNotFoundError("not cached yet")
        return fake_model

    fake_mod = types.ModuleType("faster_whisper")
    fake_mod.__spec__ = importlib.machinery.ModuleSpec("faster_whisper", loader=None)
    fake_mod.WhisperModel = _whisper_ctor
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_mod)

    stt_cached = SpeechToText(RpiConfig())
    m1 = await stt_cached._ensure_model()
    assert m1 is fake_model
    assert len(calls) == 1 and calls[0][1].get("local_files_only") is True

    stt_uncached = SpeechToText(RpiConfig(whisper_model="missing-local"))
    m2 = await stt_uncached._ensure_model()
    assert m2 is fake_model
    assert len(calls) == 3
    assert calls[1][1].get("local_files_only") is True and "local_files_only" not in calls[2][1]

    await stt_cached.warmup()
    assert len(fake_model.calls[0][0]) == 4000


async def test_client_speculative_empty_pause_falls_through_and_followup_empty_does_not_reopen_mic():
    """Prázdný mezivýsledek z pomlky nesmí přeskočit plný přepis a neúspěšný follow-up už znovu neotvírá mikrofon."""
    cfg = _fast_cfg()
    stt = ScriptedStt(cfg, [("ne", "cs")])
    client = RpiVoiceButtonClient(cfg, stt=stt, tts=DummyTts(cfg), player=FakePlayer(cfg))
    wav = _utt_wav(0.3)
    utt = RecordedUtterance(
        wav_bytes=wav, pcm_bytes=wav[44:], duration_s=0.3, speech_duration_s=0.2, peak_rms=900.0
    )
    client._pause_result = TranscriptionResult("", "", "cs")

    text = await client._transcribe_utterance(utt, is_followup=True)
    assert text == "ne"

    client._server_ok = False
    reply = await client._send_with_heartbeat(utt, "", is_followup=True)
    assert reply is not None
    assert reply["expects_answer"] is False
    assert reply["led"] == "off"


async def test_instant_listen_skips_posloucham_and_does_not_wait_for_slow_press():
    """Výchozí konfigurace rovnou poslouchá bez 'Poslouchám' a neblokuje mikrofon pomalým /api/press."""
    import asyncio as _a
    import time as _t

    cfg = _fast_cfg(server_url="http://testserver")
    assert cfg.speak_listening_greeting is False
    opened_at: list[float] = []
    utterances_sent: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/press":
            await _a.sleep(0.35)
            return httpx.Response(200, json={"say": "Poslouchám.", "ts": 1.0})
        if request.url.path == "/api/utterance":
            utterances_sent.append(request.url.path)
            return httpx.Response(
                200, json={"say": "Hotovo.", "screen": "hotovo", "led": "green", "tone": "double"}
            )
        return httpx.Response(200, json={})

    t_start = _t.monotonic()

    def fake_mic(_stop):
        opened_at.append(_t.monotonic() - t_start)
        return [SILENCE_FRAME] * 5 + [SPEECH_FRAME] * 10 + [SILENCE_FRAME] * 14

    player = FakePlayer(cfg)
    stt = ScriptedStt(cfg, [("Otevři Seznam", "cs")])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=cfg.server_url) as http:
        client = RpiVoiceButtonClient(
            cfg, http_client=http, stt=stt, tts=DummyTts(cfg), player=player, frame_source=fake_mic
        )
        reply = await client.run_interaction()

    assert reply is not None and reply["screen"] == "hotovo"
    assert opened_at and opened_at[0] < 0.15
    assert utterances_sent == ["/api/utterance"]
    assert not any("Poslouchám" in s for s in client.spoken_history)
    assert "beep" in player.tones

    cfg_silent = _fast_cfg(server_url="http://testserver", listen_beep=False)
    player_silent = FakePlayer(cfg_silent)
    stt_silent = ScriptedStt(cfg_silent, [("Stop", "cs")])
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=cfg_silent.server_url
    ) as http:
        client2 = RpiVoiceButtonClient(
            cfg_silent,
            http_client=http,
            stt=stt_silent,
            tts=DummyTts(cfg_silent),
            player=player_silent,
            frame_source=fake_mic,
        )
        await client2.run_interaction()
    assert "beep" not in player_silent.tones
    assert not any("Poslouchám" in s for s in client2.spoken_history)


async def test_instant_listen_still_aborts_when_press_returns_zastavila():
    """Pokud souběžný /api/press zastavil běžící akci na serveru, okamžitý poslech se ihned ukončí."""
    import asyncio as _a
    import time as _t

    utterances: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/press":
            await _a.sleep(0.02)
            return httpx.Response(200, json={"screen": "klid", "say": "Zastavila jsem.", "ts": 2.0})
        utterances.append(request.url.path)
        return httpx.Response(200, json={})

    def endless_slow_mic(stop_chk):
        for _ in range(120):
            if stop_chk():
                break
            _t.sleep(0.015)
            yield SILENCE_FRAME

    cfg = _fast_cfg(server_url="http://testserver")
    player = FakePlayer(cfg)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=cfg.server_url) as http:
        client = RpiVoiceButtonClient(
            cfg,
            http_client=http,
            stt=ScriptedStt(cfg, []),
            tts=DummyTts(cfg),
            player=player,
            frame_source=endless_slow_mic,
        )
        reply = await client.run_interaction()

    assert reply is not None and "Zastavila" in reply["say"]
    assert utterances == []
    assert "double" in player.tones
    assert any("Zastavila" in s for s in client.spoken_history)


async def test_rpi_stt_optimizations_followup_hotwords_max_tokens_and_48k_resampling(monkeypatch):
    """Follow-up použije úsporná hotwords a nízký strop tokenů; 48kHz WAV v stt.py nealiasuje na 4 kHz."""
    import importlib.machinery
    import math as _m
    import types
    import numpy as np
    from raspberry_pi.stt import wav_to_float32

    cfg = RpiConfig()
    model = _FakeWhisper([("cs", 1.0, "Ano."), ("cs", 1.0, "Zmizel mi WhatsApp.")])
    stt = _local_stt(cfg, model)

    res_fu = await stt.transcribe_wav(_utt_wav(0.3), is_followup=True)
    assert res_fu.text == "Ano."
    kw_fu = model.calls[0][1]
    assert kw_fu["max_new_tokens"] == 24
    assert kw_fu["compression_ratio_threshold"] == 2.4
    assert kw_fu["language_detection_segments"] == 1
    assert "ano" in kw_fu["hotwords"] and "WhatsApp" not in kw_fu["hotwords"]

    res_main = await stt.transcribe_wav(_utt_wav(0.9), is_followup=False)
    assert res_main.text == "Zmizel mi WhatsApp."
    kw_main = model.calls[1][1]
    assert kw_main["max_new_tokens"] == 128
    assert "WhatsApp" in kw_main["hotwords"]

    strict_calls: list[dict] = []

    class StrictWhisper:
        def __init__(self, model_name, device="cpu", compute_type="int8", cpu_threads=4):
            strict_calls.append({"model_name": model_name, "device": device, "compute_type": compute_type})

    fake_mod = types.ModuleType("faster_whisper")
    fake_mod.__spec__ = importlib.machinery.ModuleSpec("faster_whisper", loader=None)
    fake_mod.WhisperModel = StrictWhisper
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_mod)
    await SpeechToText(cfg)._ensure_model()
    assert strict_calls and strict_calls[0]["compute_type"] == "int8"

    t = np.arange(48000, dtype=np.float64) / 48000.0
    sig = 8000.0 * np.sin(2.0 * _m.pi * 500.0 * t) + 8000.0 * np.sin(2.0 * _m.pi * 12000.0 * t)
    wav48 = pcm_to_wav(np.clip(sig, -32767, 32767).astype("<i2").tobytes(), 48000)
    out = wav_to_float32(wav48)
    assert out is not None and out.dtype == np.float32
    spec = np.abs(np.fft.rfft(out * np.hanning(out.size)))
    f = np.fft.rfftfreq(out.size, 1.0 / 16000.0)
    assert (
        20.0 * _m.log10(float(spec[(f > 3800) & (f < 4200)].max()) / float(spec[(f > 480) & (f < 520)].max()))
        < -55.0
    )


def test_example_configs_parse_all_fast_defaults():
    from pathlib import Path
    from helpbutton.server.config import load_config as load_server_config

    rpi_toml = Path(__file__).resolve().parent / "rpi_button.example.toml"
    rc = load_rpi_config(rpi_toml)
    assert rc.speak_listening_greeting is False
    assert rc.listen_beep is True
    assert rc.whisper_model == "base"
    assert rc.min_listen_s == 4.0
    assert rc.silence_tail_s == 1.15
    assert rc.followup_start_timeout_s == 4.0
    assert rc.output_volume_pct == 92

    srv_toml = Path(__file__).resolve().parents[1] / "helpbutton.example.toml"
    sc = load_server_config(srv_toml)
    assert sc.whisper_compute_type == "auto"
    assert sc.whisper_beam_size == 1


def test_vad_min_listen_4s_prevents_early_cutoff_and_allows_manual_finish():
    """Na běžný stisk musí poslech vydržet minimálně 4 s i při pauze mezi slovy (ruční druhý stisk skončí hned)."""
    cfg = RpiConfig()
    assert cfg.min_listen_s == 4.0
    stream = (
        [SILENCE_FRAME] * 10
        + [SPEECH_FRAME] * 10
        + [SILENCE_FRAME] * 50
        + [SPEECH_FRAME] * 20
        + [SILENCE_FRAME] * 80
    )
    utt = process_vad_stream(stream, cfg, on_pause_check=lambda _p: ("complete", "Otevři"))
    assert utt.has_speech
    assert utt.duration_s >= 4.0
    assert utt.speech_duration_s >= 0.85

    step = {"i": 0}

    def early_gen():
        for i in range(180):
            step["i"] = i
            yield SPEECH_FRAME if 5 <= i < 20 else SILENCE_FRAME

    utt_early = process_vad_stream(early_gen(), cfg, finish_early=lambda: step["i"] == 25)
    assert utt_early.has_speech
    assert utt_early.duration_s < 1.5


async def test_stt_relaxed_rescue_and_speculative_short_text_guard():
    """Když 1. průchod vrátí prázdno, 2. uvolněný pokus bez promptu zachrání text; 1-slovo ze spekulace nezahodí dlouhou větu."""
    cfg = RpiConfig(whisper_beam_size=1, rolling_micro_stt=False)
    model = _FakeWhisper([("cs", 1.0, ""), ("cs", 1.0, "Otevři Seznam"), ("cs", 1.0, "Otevři Seznam")])
    stt = _local_stt(cfg, model)

    res = await stt.transcribe_wav(_utt_wav(0.9), fast_speculative=False)
    assert res.text == "Otevři Seznam"
    assert model.calls[0][1]["no_speech_threshold"] == 0.78
    assert model.calls[1][1]["no_speech_threshold"] is None
    assert model.calls[1][1]["beam_size"] == 2

    res_spec = await stt.transcribe_wav(_utt_wav(0.9), fast_speculative=True)
    assert res_spec.text == "Otevři Seznam"
    assert model.calls[2][1]["max_new_tokens"] == 128

    full_cfg = _fast_cfg(rolling_micro_stt=True)
    seen_calls = {"n": 0}

    class GuardStt(SpeechToText):
        @property
        def local_available(self) -> bool:
            return True

        async def warmup(self) -> None:
            pass

        async def transcribe_wav(
            self, wav_bytes: bytes, extra_prompt: str = "", *, is_followup: bool = False, **_kw
        ):
            seen_calls["n"] += 1
            return TranscriptionResult("Otevři mi prosím Seznam", "Otevři mi prosím Seznam", "cs")

    client, _p, _t = _scripted_client(full_cfg, [])
    client.stt = GuardStt(full_cfg)
    client._pause_result = TranscriptionResult("Otevři", "Otevři", "cs")
    utt = RecordedUtterance(
        wav_bytes=_utt_wav(2.0),
        pcm_bytes=b"\x01\x00" * 32000,
        duration_s=2.5,
        speech_duration_s=2.0,
        peak_rms=2000.0,
    )
    txt = await client._transcribe_utterance(utt, is_followup=False)
    assert seen_calls["n"] == 1
    assert txt == "Otevři mi prosím Seznam"


def test_connect_pi_script_lf_and_valid_toml_update():
    from pathlib import Path

    script = (Path(__file__).resolve().parent / "connect_pi.sh").read_bytes()
    assert b"\r\n" not in script
    assert b"load_rpi_config" in script
    assert b"ORIG_ARGS" in script
