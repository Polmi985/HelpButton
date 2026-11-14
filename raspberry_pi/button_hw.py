"""Ovladač fyzického tlačítka a stavové LED pro Raspberry Pi 5 + klávesnice/USB tlačítko.

Podporuje současně:
  1. GPIO tlačítko na Raspberry Pi 5 (výchozí BCM GPIO 17 = pin 11, spíná proti GND = pin 9)
     přes `gpiozero` + `lgpio` (plně kompatibilní s čipem RP1 na Raspberry Pi 5).
     Piny jsou dostupné na průchozí liště HATu Audio Injector Zero; piny HATu (GPIO 2, 3, 18–21)
     jsou chráněné a nelze je omylem použít,
  2. USB tlačítko / klávesnici (Mezerník nebo Enter v terminálu) pro okamžité testování
     přes USB kabel nebo SSH bez nutnosti pájet piny,
  3. Stavovou LED na GPIO (výchozí BCM GPIO 27 = pin 13, s podporou PWM dýchání v klidu)
     + barevnou indikaci v terminálu.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import sys
import threading
import time
from typing import Callable

from .config import RpiConfig

log = logging.getLogger("helpbutton.rpi.button")

HAT_RESERVED_PINS: dict[int, str] = {
    2: "I2C SDA (WM8731)",
    3: "I2C SCL (WM8731)",
    18: "I2S BCLK",
    19: "I2S LRCLK",
    20: "I2S DIN",
    21: "I2S DOUT",
}

TERM_LED = {
    "blue": "\033[94m● MODRÁ (Poslouchám / Čekám na odpověď)\033[0m",
    "blink": "\033[96m◐ BLIKÁ (HelpButton pracuje…)\033[0m",
    "green": "\033[92m● ZELENÁ (Hotovo)\033[0m",
    "red": "\033[91m● ČERVENÁ (Odmítnuto / Chyba)\033[0m",
    "off": "\033[90m○ KLID (Jemné noční podsvícení)\033[0m",
}


class ButtonController:
    """Sjednocené ovládání GPIO tlačítka (Pi 5) a klávesnice/USB tlačítka."""

    def __init__(self, cfg: RpiConfig, on_press: Callable[[], None]):
        self.cfg = cfg
        self._on_press = on_press
        self._gpio_btn = None
        self._gpio_led = None
        self._is_pwm_led = False
        self._led_state = "off"
        self._running = False
        self._kb_held_until = 0.0
        self._last_press_ts = 0.0
        self._debounce_s = 0.08
        self._kb_thread: threading.Thread | None = None
        self._term_fd: int | None = None
        self._term_settings = None
        self.gpio_active = False

    @property
    def led_state(self) -> str:
        return self._led_state

    @property
    def has_input(self) -> bool:
        """True, pokud funguje aspoň jedno ovládání (GPIO tlačítko nebo klávesnice v terminálu)."""
        return self.gpio_active or (self._kb_thread is not None and self._kb_thread.is_alive())

    def is_held(self) -> bool:
        """Vrátí True, pokud senior právě drží fyzické GPIO tlačítko nebo klávesu."""
        if self._gpio_btn is not None:
            try:
                if bool(self._gpio_btn.is_pressed):
                    return True
            except Exception:
                pass
        return time.monotonic() < self._kb_held_until

    def set_led(self, mode: str) -> None:
        """Nastaví stav LED podle `X-HelpButton-Led` (`blue`, `blink`, `green`, `red`, `off`)."""
        mode = (mode or "off").lower()
        if mode == self._led_state and self._gpio_led is None:
            return
        self._led_state = mode
        label = TERM_LED.get(mode, mode) if sys.stderr.isatty() else mode
        log.info("LED: %s", label)
        if self._gpio_led is not None:
            try:
                if mode == "blink":
                    if self._is_pwm_led:
                        self._gpio_led.pulse(fade_in_time=0.25, fade_out_time=0.25)
                    else:
                        self._gpio_led.blink(on_time=0.35, off_time=0.35)
                elif mode in ("blue", "green", "red"):
                    self._gpio_led.on()
                else:
                    if self._is_pwm_led and self.cfg.led_pwm_breathing:
                        self._gpio_led.value = 0.08
                    else:
                        self._gpio_led.off()
            except Exception as e:
                log.debug("GPIO LED chyba: %s", e)

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Spustí sledování GPIO pinu (pokud běží na Raspberry Pi) a klávesnice."""
        self._running = True
        if self.cfg.enable_gpio:
            self._init_gpio(loop)
        if self.cfg.enable_keyboard and sys.stdin and sys.stdin.isatty():
            self._kb_thread = threading.Thread(target=self._keyboard_loop, args=(loop,), daemon=True)
            self._kb_thread.start()

    def stop(self) -> None:
        self._running = False
        self._restore_terminal()
        if self._kb_thread is not None and self._kb_thread.is_alive():
            self._kb_thread.join(timeout=0.3)
        if self._gpio_led is not None:
            try:
                self._gpio_led.off()
                self._gpio_led.close()
            except Exception:
                pass
            self._gpio_led = None
        if self._gpio_btn is not None:
            try:
                self._gpio_btn.close()
            except Exception:
                pass
            self._gpio_btn = None
        self.gpio_active = False

    def _restore_terminal(self) -> None:
        """Vrátí terminál do normálního režimu (jinak po Ctrl+C nefunguje echo psaných znaků)."""
        if self._term_fd is None or self._term_settings is None:
            return
        try:
            import termios

            termios.tcsetattr(self._term_fd, termios.TCSADRAIN, self._term_settings)
        except Exception:
            pass
        self._term_settings = None

    def trigger_press(self, hold_s: float = 0.0) -> None:
        """Programové vyvolání stisku (pro testy a klávesnici) s ochranou proti zákmitům."""
        now = time.monotonic()
        if now - self._last_press_ts < self._debounce_s:
            return
        self._last_press_ts = now
        if hold_s > 0:
            self._kb_held_until = now + hold_s
        self._on_press()

    def _init_gpio(self, loop: asyncio.AbstractEventLoop) -> None:
        if self.cfg.gpio_pin in HAT_RESERVED_PINS:
            log.error(
                "GPIO %d je obsazen zvukovým HATem (%s) – tlačítko na GPIO vypnuto, použijte GPIO 17 (pin 11).",
                self.cfg.gpio_pin,
                HAT_RESERVED_PINS[self.cfg.gpio_pin],
            )
            return
        if self.cfg.led_pin is not None and self.cfg.led_pin in HAT_RESERVED_PINS:
            log.error(
                "GPIO %d je obsazen zvukovým HATem (%s) – LED vypnuta, použijte GPIO 27 (pin 13).",
                self.cfg.led_pin,
                HAT_RESERVED_PINS[self.cfg.led_pin],
            )
            self.cfg.led_pin = None
        try:
            os.environ.setdefault("GPIOZERO_PIN_FACTORY", "lgpio")
            from gpiozero import Button, LED, PWMLED  # type: ignore[import-untyped]

            self._gpio_btn = Button(self.cfg.gpio_pin, pull_up=True, bounce_time=0.06)

            def _when_pressed() -> None:
                loop.call_soon_threadsafe(self.trigger_press, 0.0)

            self._gpio_btn.when_pressed = _when_pressed
            self.gpio_active = True
            log.info(
                "GPIO tlačítko aktivní na BCM pinu %d (výchozí GPIO 17 = pin 11 + GND pin 9).",
                self.cfg.gpio_pin,
            )

            if self.cfg.led_pin is not None:
                try:
                    if self.cfg.led_pwm_breathing:
                        self._gpio_led = PWMLED(self.cfg.led_pin)
                        self._is_pwm_led = True
                    else:
                        self._gpio_led = LED(self.cfg.led_pin)
                    log.info("GPIO LED aktivní na BCM pinu %d (PWM=%s).", self.cfg.led_pin, self._is_pwm_led)
                except Exception:
                    try:
                        self._gpio_led = LED(self.cfg.led_pin)
                        self._is_pwm_led = False
                    except Exception as le:
                        log.warning("GPIO LED na pinu %s nelze otevřít: %s", self.cfg.led_pin, le)
        except Exception as e:
            self._gpio_btn = None
            text = str(e).lower()
            if "busy" in text:
                hint = "pin už používá jiný proces – neběží tlačítko podruhé (např. systemd služba)?"
            elif "permission" in text or "denied" in text:
                hint = "chybí oprávnění – uživatel musí být ve skupině gpio (odhlásit/přihlásit po instalaci)"
            elif isinstance(e, ImportError) or "lgpio" in text:
                hint = "chybí knihovna lgpio/gpiozero – spusťte znovu raspberry_pi/setup_rpi5.sh"
            else:
                hint = "na tomto počítači není GPIO (to je v pořádku mimo Raspberry Pi)"
            log.warning("GPIO tlačítko není aktivní (%s; %s) – funguje Mezerník/Enter.", e, hint)

    def _keyboard_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Sleduje stisk Mezerníku nebo Enteru v terminálu (Windows i Linux)."""
        if platform.system().lower() == "windows":
            import msvcrt

            while self._running:
                if msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    if ch in (" ", "\r", "\n"):
                        loop.call_soon_threadsafe(self.trigger_press, 0.25)
                    elif ch in ("\x03", "q", "Q"):
                        break
                time.sleep(0.03)
        else:
            try:
                import select
                import termios
                import tty

                fd = sys.stdin.fileno()
                old_settings = termios.tcgetattr(fd)
                self._term_fd, self._term_settings = fd, old_settings
                try:
                    tty.setcbreak(fd)
                    while self._running:
                        rlist, _, _ = select.select([fd], [], [], 0.05)
                        if rlist:
                            data = os.read(fd, 32)
                            if not data:
                                break
                            if any(b in data for b in (b" ", b"\r", b"\n")):
                                loop.call_soon_threadsafe(self.trigger_press, 0.25)
                finally:
                    self._restore_terminal()
            except Exception:
                while self._running:
                    line = sys.stdin.readline()
                    if not line:
                        break
                    loop.call_soon_threadsafe(self.trigger_press, 0.0)
