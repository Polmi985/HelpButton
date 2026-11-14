#!/usr/bin/env bash
# ==============================================================================
# Jednokroková instalace hlasového tlačítka HelpButton na Raspberry Pi 5
# Hardware: Audio Injector Zero (I2S HAT, kodek WM8731) + tlačítko GPIO 17 + LED GPIO 27
# OS: Raspberry Pi OS 64-bit (aarch64), PipeWire + WirePlumber
# Spuštění (jako běžný uživatel, NE přes sudo): bash raspberry_pi/setup_rpi5.sh
# Skript lze spouštět opakovaně – co už je hotové, přeskočí.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
VOICES_DIR="${SCRIPT_DIR}/voices"
VENV_DIR="${REPO_DIR}/.venv"
ALSA_CARD="audioinjectorpi"
OVERLAY="dtoverlay=audioinjector-wm8731-audio"
BOOT_CONFIG="/boot/firmware/config.txt"
[ -f "${BOOT_CONFIG}" ] || BOOT_CONFIG="/boot/config.txt"
REBOOT_NEEDED=0
WARNINGS=()

warn() { echo "  [!] $*"; WARNINGS+=("$*"); }

if [ "$(id -u)" = "0" ]; then
    echo "Nespouštějte tento skript přes sudo / jako root."
    echo "Spusťte ho jako běžný uživatel (o sudo si řekne sám):"
    echo "    bash raspberry_pi/setup_rpi5.sh"
    exit 1
fi

echo "=== [1/8] Instalace systémových balíčků pro Raspberry Pi 5 (zvuk, GPIO RP1, TTS) ==="
REQUIRED_PKGS=(python3-venv python3-pip python3-dev libportaudio2 alsa-utils curl)
OPTIONAL_PKGS=(python3-lgpio gpiod libgpiod-dev portaudio19-dev pipewire-alsa ffmpeg espeak-ng)
sudo apt-get update
sudo apt-get install -y "${REQUIRED_PKGS[@]}"
# Volitelné balíčky po jednom: chybějící balíček (jiná verze OS) nesmí shodit celou instalaci
if ! sudo apt-get install -y "${OPTIONAL_PKGS[@]}"; then
    for pkg in "${OPTIONAL_PKGS[@]}"; do
        sudo apt-get install -y "${pkg}" || warn "Balíček ${pkg} se nepodařilo nainstalovat."
    done
fi

echo "=== [2/8] Device Tree overlay pro Audio Injector Zero (WM8731) ==="
if grep -qE "^\s*${OVERLAY}\s*$" "${BOOT_CONFIG}"; then
    echo "  ✓ ${OVERLAY} už je v ${BOOT_CONFIG}"
else
    echo "  + Přidávám ${OVERLAY} do sekce [all] v ${BOOT_CONFIG}"
    printf '\n[all]\n%s\n' "${OVERLAY}" | sudo tee -a "${BOOT_CONFIG}" > /dev/null
    REBOOT_NEEDED=1
fi
# Uživatel potřebuje přístup k GPIO (tlačítko/LED) a ke zvukové kartě
for grp in gpio audio; do
    if getent group "${grp}" > /dev/null; then
        sudo usermod -aG "${grp}" "${USER}" || warn "Nelze přidat ${USER} do skupiny ${grp}."
    fi
done

echo "=== [3/8] Mixér WM8731: Input Mux = Mic, Mic Boost, Capture ~80 %, Master 0 dB (max bez zkreslení), bez sidetone ==="
if aplay -l 2>/dev/null | grep -q "${ALSA_CARD}"; then
    amix() { amixer -q -c "${ALSA_CARD}" sset "$@" > /dev/null 2>&1 || echo "  (prvek '$1' nenalezen – přeskakuji)"; }
    # Přesný prvek ovladače WM8731 (cset), záloha přes sset. „Mic Boost“ je hlasitost s jedním krokem
    # +20 dB – `sset 'Mic Boost' on` u ní končí chybou a mikrofon by zůstal o 20 dB tišší.
    acset() {
        local name="$1" val="$2"; shift 2
        amixer -q -c "${ALSA_CARD}" cset "name=${name}" "${val}" > /dev/null 2>&1 && return 0
        if [ "$#" -gt 0 ] && amixer -q -c "${ALSA_CARD}" sset "$@" > /dev/null 2>&1; then return 0; fi
        echo "  (prvek '${name}' nenalezen – přeskakuji)"
    }
    # Sluchátkový výstup: 0 dB je nejhlasitější ČISTÉ nastavení. 100 % = +6 dB a plný signál se pak
    # v zesilovači ořezává (chrčí). Hlasitost navíc dodá zpracování řeči v HelpButton (kompresor + limiter).
    amix 'Master' 0dB
    if command -v wpctl > /dev/null; then wpctl set-volume @DEFAULT_AUDIO_SINK@ 1.0 > /dev/null 2>&1 || true; fi
    acset 'Master Playback ZC Switch' on 'Master Playback ZC' on
    # de-emphasis by ubrala výšky – sykavky by byly hůř srozumitelné
    acset 'Playback Deemphasis Switch' off 'Playback Deemphasis' off
    acset 'ADC High Pass Filter Switch' on 'ADC High Pass Filter' on
    acset 'Output Mixer HiFi Playback Switch' on 'Output Mixer HiFi' on
    acset 'Input Mux' Mic 'Input Mux' Mic
    acset 'Mic Capture Switch' on 'Mic' cap        # WM8731 má mikrofon po zapnutí ztlumený
    acset 'Mic Boost Volume' 1 'Mic Boost' 100%     # +20 dB pro mikrofon na desce
    amix 'Capture' 80%                              # linkový vstup (mikrofonu se u WM8731 netýká)
    acset 'Sidetone Playback Volume' 0 'Sidetone' 0
    acset 'Output Mixer Line Bypass Switch' off 'Output Mixer Line Bypass' off
    acset 'Output Mixer Mic Sidetone Switch' off 'Output Mixer Mic Sidetone' off
    acset 'Store DC Offset Switch' off 'Store DC Offset' off
    sudo alsactl store || warn "alsactl store selhal – nastavení mixéru se po restartu obnoví při startu tlačítka."
    echo "  ✓ Nastavení mixéru uloženo (sudo alsactl store)"
elif [ "${REBOOT_NEEDED}" = "1" ]; then
    echo "  (Karta ${ALSA_CARD} se objeví až po restartu – mixér se nastaví při druhém spuštění skriptu.)"
else
    warn "Karta ${ALSA_CARD} není vidět (aplay -l). Zkontrolujte, že je HAT pevně nasazený, a restartujte."
fi

echo "=== [4/8] Python virtuální prostředí (.venv) a knihovny ==="
cd "${REPO_DIR}"
if [ -d "${VENV_DIR}" ] && ! "${VENV_DIR}/bin/python" -c "import sys" > /dev/null 2>&1; then
    echo "  Poškozené .venv – vytvářím znovu."
    rm -rf "${VENV_DIR}"
fi
if [ -d "${VENV_DIR}" ] && [ ! -w "${VENV_DIR}" ]; then
    echo "  .venv patří jinému uživateli (dříve spuštěno přes sudo?) – opravuji vlastníka."
    sudo chown -R "$(id -u):$(id -g)" "${VENV_DIR}"
fi
# --system-site-packages: lgpio z balíčku python3-lgpio musí být vidět i ve .venv
python3 -m venv --system-site-packages "${VENV_DIR}"
VPY="${VENV_DIR}/bin/python"

"${VPY}" -m pip install --upgrade pip
"${VPY}" -m pip install -e ".[dev,voice]"
# numpy + scipy: rychlé zpracování zvuku (filtry, AGC, hlasitost) – bez nich běží pomalejší čistý Python
for pkg in faster-whisper piper-tts edge-tts sounddevice gpiozero numpy scipy; do
    "${VPY}" -m pip install "${pkg}" || warn "Python balíček ${pkg} se nepodařilo nainstalovat."
done
"${VPY}" -m pip install webrtcvad-wheels \
    || warn "webrtcvad se nenainstaloval – detekce řeči použije záložní energetický detektor (funguje, jen méně přesně)."

echo "=== [5/8] Stahování českého hlasového modelu pro Piper TTS (cs_CZ-jirka-medium) ==="
mkdir -p "${VOICES_DIR}"
HF_BASE="https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/cs/cs_CZ/jirka/medium"
download() {  # stáhne do dočasného souboru a přejmenuje až po úspěchu (žádné poloviční / HTML soubory)
    local url="$1" dest="$2" min_size="$3"
    if [ -f "${dest}" ] && [ "$(stat -c %s "${dest}")" -ge "${min_size}" ]; then
        echo "  ✓ $(basename "${dest}") už je stažený"
        return 0
    fi
    if curl -fL --retry 3 --retry-delay 2 -o "${dest}.part" "${url}" \
        && [ "$(stat -c %s "${dest}.part")" -ge "${min_size}" ]; then
        mv "${dest}.part" "${dest}"
        echo "  ✓ Staženo $(basename "${dest}")"
    else
        rm -f "${dest}.part"
        warn "Stažení $(basename "${dest}") selhalo – HelpButton použije náhradní hlas (Edge-TTS / espeak)."
    fi
}
download "${HF_BASE}/cs_CZ-jirka-medium.onnx" "${VOICES_DIR}/cs_CZ-jirka-medium.onnx" 1000000
download "${HF_BASE}/cs_CZ-jirka-medium.onnx.json" "${VOICES_DIR}/cs_CZ-jirka-medium.onnx.json" 1000

echo "=== [6/8] Konfigurace (rpi_button.toml) a helpbutton.toml ==="
if [ ! -f "${SCRIPT_DIR}/rpi_button.toml" ]; then
    cp "${SCRIPT_DIR}/rpi_button.example.toml" "${SCRIPT_DIR}/rpi_button.toml"
    echo "  + Vytvořeno raspberry_pi/rpi_button.toml (upravte dle potřeby)"
else
    echo "  ✓ raspberry_pi/rpi_button.toml už existuje (ponechávám)"
fi
if [ ! -f "${REPO_DIR}/helpbutton.toml" ] && [ -f "${REPO_DIR}/helpbutton.example.toml" ]; then
    cp "${REPO_DIR}/helpbutton.example.toml" "${REPO_DIR}/helpbutton.toml"
    echo "  + Vytvořeno helpbutton.toml (potřebné jen pro záložní lokální server na Pi)"
fi
# Párovací token v konfiguraci smí číst jen tento uživatel (ne ostatní účty na Pi)
chmod 600 "${SCRIPT_DIR}/rpi_button.toml" 2>/dev/null || true
[ -f "${REPO_DIR}/helpbutton.toml" ] && chmod 600 "${REPO_DIR}/helpbutton.toml" 2>/dev/null || true

echo "=== [7/8] Předstažení modelu Faster-Whisper (base, int8) – vyžaduje internet ==="
"${VPY}" - <<'PY' || warn "Stažení modelu Whisper selhalo – stáhne se při prvním startu s internetem (do té doby rozpoznává server)."
from faster_whisper import WhisperModel
WhisperModel("base", device="cpu", compute_type="int8", cpu_threads=4)
print("  ✓ Model base připraven v cache")
PY

echo "=== [8/8] Kontrola instalace ==="
if "${VPY}" - <<'PY'
import importlib.util, sys
need = ["httpx", "websockets", "zeroconf", "fastapi", "sounddevice", "faster_whisper", "gpiozero", "numpy"]
for opt in ("scipy",):
    if importlib.util.find_spec(opt) is None:
        print(f"  (volitelné: {opt} chybí – zvuk se zpracuje pomaleji)")
missing = [m for m in need if importlib.util.find_spec(m) is None]
try:
    import sounddevice  # noqa: F401  (ověří i systémovou knihovnu libportaudio2)
except Exception as e:
    missing.append(f"sounddevice ({e})")
if missing:
    print("  [!] V .venv chybí:", ", ".join(dict.fromkeys(missing)))
    sys.exit(1)
print("  ✓ Všechny Python knihovny jsou v .venv")
PY
then :; else warn "Instalace Python knihovn není úplná – spusťte skript znovu (zkontrolujte připojení k internetu)."; fi
"${VPY}" -m raspberry_pi --list-audio || warn "Výpis zvukových zařízení selhal."

echo ""
echo "=============================================================================="
if [ "${#WARNINGS[@]}" -gt 0 ]; then
    echo "Instalace dokončena s upozorněními:"
    for w in "${WARNINGS[@]}"; do echo "  - ${w}"; done
    echo ""
fi
if [ "${REBOOT_NEEDED}" = "1" ]; then
    echo "!! Byl přidán overlay zvukové karty – RESTARTUJTE Pi (sudo reboot) a spusťte skript znovu."
    echo ""
fi
echo "Hotovo! Jak tlačítko na Raspberry Pi 5 spustit (z této složky, .venv se použije automaticky):"
echo "  cd ${REPO_DIR}"
echo "  python3 -m raspberry_pi --test-audio     # otestuje 3,5mm výstup HATu, mikrofon, STT i TTS"
echo "  python3 -m raspberry_pi                  # spustí hlasové tlačítko (automaticky najde server)"
echo "=============================================================================="
