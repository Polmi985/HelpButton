#!/usr/bin/env bash
# ==============================================================================
# Jednotný příkaz pro Raspberry Pi 5: aktualizace kódu, čistý reset konfigurace,
# nastavení serveru + tokenu, ověření zvuku i spojení a spuštění tlačítka.
#
# Použití:
#   bash raspberry_pi/connect_pi.sh <IP_NEBO_URL_SERVERU> [TOKEN] [volby]
#   bash raspberry_pi/connect_pi.sh http://192.168.1.10:8765 <TOKEN>
#   bash raspberry_pi/connect_pi.sh 192.168.1.10 TOKEN --service
#
# Volby:
#   --server URL       Adresa serveru (např. http://192.168.1.10:8765 nebo auto)
#   --token TOKEN      Párovací token ([helpbutton] token v helpbutton.toml na PC serveru)
#   --model MODEL      Faster-Whisper model (base | small | tiny, výchozí z šablony: base)
#   --min-listen SEC   Minimální doba poslechu v sekundách (výchozí: 4.0)
#   --service          Nainstaluje/restartuje systemd user službu helpbutton.service
#   --test             Spustí nejprve `--test-audio` a skončí
#   --no-pull          Přeskočí `git fetch && git reset --hard origin/main`
#   --no-start         Pouze nastaví a ověří spojení, nespouští klienta
#   --full-setup       Vynutí celý `bash raspberry_pi/setup_rpi5.sh`
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_DIR}"
ORIG_ARGS=("$@")

SERVER_ARG=""
TOKEN_ARG=""
MODEL_ARG=""
MIN_LISTEN_ARG=""
HAS_TOKEN=0
MODE="run"
NO_PULL=0
FULL_SETUP=0

usage() {
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        -h|--help)
            usage
            exit 0
            ;;
        --server)
            SERVER_ARG="${2:-}"
            shift 2
            ;;
        --token)
            TOKEN_ARG="${2:-}"
            HAS_TOKEN=1
            shift 2
            ;;
        --model|--whisper-model)
            MODEL_ARG="${2:-}"
            shift 2
            ;;
        --min-listen)
            MIN_LISTEN_ARG="${2:-}"
            shift 2
            ;;
        --service)
            MODE="service"
            shift
            ;;
        --test)
            MODE="test"
            shift
            ;;
        --no-start)
            MODE="none"
            shift
            ;;
        --no-pull)
            NO_PULL=1
            shift
            ;;
        --full-setup)
            FULL_SETUP=1
            shift
            ;;
        -*)
            echo "Neznámý přepínač: $1" >&2
            usage >&2
            exit 2
            ;;
        *)
            if [ -z "${SERVER_ARG}" ]; then
                SERVER_ARG="$1"
            elif [ "${HAS_TOKEN}" = "0" ]; then
                TOKEN_ARG="$1"
                HAS_TOKEN=1
            else
                echo "Neočekávaný argument: $1" >&2
                exit 2
            fi
            shift
            ;;
    esac
done

echo "=== [1/6] Zastavuji běžící instanci HelpButton tlačítka ==="
systemctl --user stop helpbutton.service 2>/dev/null || true
pkill -f "python.*-m[[:space:]]+raspberry_pi" 2>/dev/null || true

if [ "${NO_PULL}" = "0" ] && [ -z "${HELPBUTTON_CONNECT_UPDATED:-}" ] && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "=== [2/6] Aktualizuji repozitář na nejnovější origin/main ==="
    if git fetch origin && git reset --hard origin/main; then
        HELPBUTTON_CONNECT_UPDATED=1
        export HELPBUTTON_CONNECT_UPDATED
        exec bash "${BASH_SOURCE[0]}" "${ORIG_ARGS[@]}" --no-pull
    else
        echo "  [!] git fetch selhal (offline?) – pokračuji s aktuální verzí souborů."
    fi
else
    echo "=== [2/6] Repozitář připraven ($(git rev-parse --short HEAD 2>/dev/null || echo 'local')) ==="
fi

if [ -z "${SERVER_ARG}" ] && [ -t 0 ]; then
    read -r -p "Zadejte IP nebo URL serveru PC [Enter = auto]: " SERVER_ARG
fi
if [ -z "${SERVER_ARG}" ]; then
    SERVER_ARG="auto"
fi
if [ "${HAS_TOKEN}" = "0" ] && [ -t 0 ]; then
    read -r -p "Zadejte párovací token z helpbutton.toml serveru [Enter = bez tokenu]: " TOKEN_ARG
fi

VPY="${REPO_DIR}/.venv/bin/python"
if [ "${FULL_SETUP}" = "1" ] || [ ! -x "${VPY}" ] || [ ! -f "${REPO_DIR}/raspberry_pi/voices/cs_CZ-jirka-medium.onnx" ]; then
    echo "=== [3/6] Instalace / oprava prostředí (.venv, balíčky, hlas Piper, mixér) ==="
    bash "${SCRIPT_DIR}/setup_rpi5.sh"
else
    if ! "${VPY}" -c "import httpx, websockets, zeroconf, sounddevice, faster_whisper, gpiozero, numpy" >/dev/null 2>&1; then
        echo "=== [3/6] Doplňuji chybějící Python balíčky (.venv) ==="
        bash "${SCRIPT_DIR}/setup_rpi5.sh"
    else
        echo "=== [3/6] Prostředí .venv i hlas Piper jsou připraveny ==="
    fi
fi

echo "=== [4/6] Resetuji a zapisuji raspberry_pi/rpi_button.toml ==="
rm -rf "${SCRIPT_DIR}/.tts_cache"/* 2>/dev/null || true

HELPBUTTON_SETUP_SERVER="${SERVER_ARG}" \
HELPBUTTON_SETUP_TOKEN="${TOKEN_ARG}" \
HELPBUTTON_SETUP_MODEL="${MODEL_ARG}" \
HELPBUTTON_SETUP_MIN_LISTEN="${MIN_LISTEN_ARG}" \
"${VPY}" - <<'PY'
from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

from raspberry_pi.config import load_rpi_config, normalize_server_url

pkg_dir = Path("raspberry_pi")
example_path = pkg_dir / "rpi_button.example.toml"
toml_path = pkg_dir / "rpi_button.toml"

server_url = normalize_server_url(os.environ.get("HELPBUTTON_SETUP_SERVER", "auto"))
token = os.environ.get("HELPBUTTON_SETUP_TOKEN", "").strip()
model = os.environ.get("HELPBUTTON_SETUP_MODEL", "").strip()
min_listen = os.environ.get("HELPBUTTON_SETUP_MIN_LISTEN", "").strip()

text = example_path.read_text(encoding="utf-8")

def set_key(src: str, key: str, value_literal: str) -> str:
    pattern = re.compile(rf"^({re.escape(key)}\s*=\s*).*$", re.MULTILINE)
    if not pattern.search(src):
        raise RuntimeError(f"Klíč {key!r} nebyl nalezen v {example_path}")
    return pattern.sub(lambda m: f"{m.group(1)}{value_literal}", src, count=1)

def q(s: str) -> str:
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'

text = set_key(text, "url", q(server_url))
text = set_key(text, "token", q(token))
if model:
    text = set_key(text, "whisper_model", q(model))
if min_listen:
    text = set_key(text, "min_listen_s", str(max(0.0, float(min_listen))))

toml_path.write_text(text, encoding="utf-8")
os.chmod(toml_path, 0o600)

# Tvrdá validace: TOML se musí načíst bez jediné chyby
tomllib.loads(toml_path.read_text(encoding="utf-8"))
cfg = load_rpi_config(toml_path)
masked = f"{cfg.token[:4]}...{cfg.token[-4:]}" if len(cfg.token) > 8 else ("(nastaven)" if cfg.token else "(prázdný)")
print(f"  ✓ rpi_button.toml uložen a ověřen (url={cfg.server_url}, token={masked}, model={cfg.whisper_model}, min_listen={cfg.min_listen_s:.1f}s)")
PY

echo "=== [5/6] Mixér WM8731 + kontrola Whisper modelu ==="
"${VPY}" - <<'PY'
from faster_whisper import WhisperModel
from raspberry_pi.audio_io import ensure_wm8731_mixer
from raspberry_pi.config import load_rpi_config

cfg = load_rpi_config()
if cfg.hat_mixer_init:
    ensure_wm8731_mixer(cfg.alsa_card, cfg.output_volume_pct)
try:
    WhisperModel(cfg.whisper_model, device=cfg.whisper_device, compute_type=cfg.whisper_compute_type, cpu_threads=cfg.whisper_threads)
    print(f"  ✓ Faster-Whisper model '{cfg.whisper_model}' připraven v cache")
except Exception as e:
    print(f"  [!] Whisper model '{cfg.whisper_model}' zatím nejde načíst: {e}")
PY

echo "=== [6/6] Testuji připojení k HelpButton serveru ==="
"${VPY}" - <<'PY' || true
import asyncio
import httpx

from raspberry_pi.client import verify_server
from raspberry_pi.config import auth_headers, load_rpi_config

async def check() -> None:
    cfg = load_rpi_config()
    if cfg.server_url == "auto":
        print("  (Režim server=auto – server se vyhledá přes mDNS při startu klienta.)")
        return
    try:
        async with httpx.AsyncClient(base_url=cfg.server_url, timeout=4.0, trust_env=False) as http:
            if cfg.token:
                ok = await verify_server(cfg.server_url, cfg.token, http)
                if not ok:
                    print(f"  [!] CHYBA TOKENU: Server {cfg.server_url} nepotvrdil token. Zkontrolujte `[helpbutton] token` v helpbutton.toml na PC!")
                    return
            r = await http.get("/api/state", headers=auth_headers(cfg.token))
            if r.status_code in (401, 403):
                print(f"  [!] CHYBA AUTORIZACE (HTTP {r.status_code}): Server {cfg.server_url} odmítl token – zkopírujte přesný `[helpbutton] token` z helpbutton.toml na PC.")
                return
            r.raise_for_status()
            st = r.json()
            print(f"  ✓ PŘIPOJENO K SERVERU {cfg.server_url} (senior={st.get('senior', '?')}, llm={st.get('llm', '?')}, server_stt={bool((st.get('voice') or {}).get('stt'))})")
    except Exception as e:
        print(f"  [!] Server {cfg.server_url} teď neodpovídá ({e}).")
        print("      Zkontrolujte na PC: (1) běží `python -m helpbutton.server`? (2) sedí IPv4 adresa PC? (3) je povolený TCP port 8765 ve Firewallu?")

asyncio.run(check())
PY

case "${MODE}" in
    service)
        echo "=== Nastavuji a spouštím systemd službu helpbutton.service ==="
        sudo loginctl enable-linger "${USER}" >/dev/null 2>&1 || true
        mkdir -p "${HOME}/.config/systemd/user"
        cat > "${HOME}/.config/systemd/user/helpbutton.service" <<EOF
[Unit]
Description=HelpButton Voice Button (Raspberry Pi 5 + Audio Injector Zero)
After=pipewire.service wireplumber.service network-online.target
Wants=pipewire.service wireplumber.service

[Service]
Type=simple
WorkingDirectory=${REPO_DIR}
Environment="PYTHONUNBUFFERED=1"
ExecStart=${VPY} -m raspberry_pi
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF
        systemctl --user daemon-reload
        systemctl --user enable --now helpbutton.service
        systemctl --user restart helpbutton.service
        systemctl --user --no-pager status helpbutton.service || true
        echo ""
        echo "Živý výpis logů spustíte přes: journalctl --user -u helpbutton.service -f"
        ;;
    test)
        exec "${VPY}" -m raspberry_pi --test-audio
        ;;
    none)
        echo "Hotovo. Spuštění tlačítka: cd ${REPO_DIR} && python3 -m raspberry_pi"
        ;;
    *)
        exec "${VPY}" -m raspberry_pi
        ;;
esac
