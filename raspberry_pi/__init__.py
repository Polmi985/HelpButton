"""HelpButton LAN – Raspberry Pi 5 Hlasové Tlačítko (Grandpa-Proof Voice Button & Audio Client).

Tento balíček běží na Raspberry Pi 5 (8 GB RAM) a zajišťuje:
  * Fyzické tlačítko (GPIO 17 přes lgpio/gpiozero + klávesnice/USB tlačítko),
  * Automatickou detekci USB mikrofonu ("chobotnice") a USB zvukové karty,
  * Chytré nahrávání s detekcí konce řeči (VAD) přizpůsobené seniorům
    (funguje na krátký stisk i podržení, toleruje 2.2s pomlky na přemýšlení,
    automaticky poslouchá odpověď ANO/NE po dotazu HelpButton),
  * Lokální převod řeči na text (Faster-Whisper int8) + volitelný překlad EN->CS,
  * Přirozený český hlas (Edge-TTS Vlasta / offline Piper TTS / espeak-ng záloha)
    s okamžitou diskovou cache,
  * Plné napojení na HelpButton Server přes LAN (mDNS) nebo USB kabel.
"""

from __future__ import annotations

__version__ = "1.0.0"
