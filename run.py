"""Spustí server HelpButton (tlačítko + chat Jiřík s hlasem Jirka) a ovládání tohoto počítače, pak otevře chat
v prohlížeči.

Spouštějte přes „Start Jirik.bat“. Ukončení: zavřete toto okno nebo Ctrl+C."""
from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
PORT = 8765
URL = f"http://127.0.0.1:{PORT}"
CHAT = f"{URL}/chat"


def up(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def main() -> int:
    os.chdir(HERE)
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "HELPBUTTON_CONFIG": str(HERE / "helpbutton.toml")}
    if not (HERE / "voices" / "cs_CZ-jirka-medium.onnx").exists():
        print("[!] Chybí hlas voices/cs_CZ-jirka-medium.onnx – Jiřík bude bez hlasu.")
    if not up("http://localhost:11434/api/tags"):
        print("[!] Ollama neběží – spusťte aplikaci Ollama. Bez ní Jiřík nemůže odpovídat "
              "a neumí klikat na obrazovce.")
    if up(f"{URL}/api/ping"):
        print(f"Jiřík už běží – otevírám {CHAT}")
        webbrowser.open(CHAT)
        return 0
    py = sys.executable
    procs = [subprocess.Popen([py, "-m", "helpbutton.server", "--config", "helpbutton.toml"], cwd=HERE, env=env)]
    try:
        for _ in range(360):
            if up(f"{URL}/api/ping", 1.0):
                break
            if procs[0].poll() is not None:
                print("[!] Server se nespustil – chyba je vypsaná výše.")
                return 1
            time.sleep(0.5)
        else:
            print("[!] Server Jiříka neodpověděl včas.")
            return 1
        procs.append(subprocess.Popen([py, "-m", "helpbutton.agents.pc", "--server", f"ws://127.0.0.1:{PORT}/ws/device",
                                       "--name", "Tento počítač"], cwd=HERE, env=env))
        time.sleep(1.5)
        webbrowser.open(CHAT)
        print(f"\n=== Jiřík běží: {CHAT}  · tlačítko: {URL}/button  · rodina: {URL}/  (ukončení: zavřete toto okno nebo Ctrl+C) ===\n")
        while procs[0].poll() is None:        # ovládání počítače jde znovu spustit tlačítkem „Spustit vše“
            time.sleep(1)
        print("[!] Server Jiříka skončil.")
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()


if __name__ == "__main__":
    sys.exit(main())
