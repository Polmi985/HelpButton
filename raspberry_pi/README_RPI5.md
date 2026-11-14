# Hlasové tlačítko HelpButton – instalace na Raspberry Pi 5 krok za krokem

Postupujte **v pořadí**. Každý krok končí **✅ Kontrolou** – nepokračujte, dokud neprojde.

**Výsledek:** zmáčknete tlačítko → řeč se nahraje a vyčistí → přepíše se lokálně (Faster-Whisper) → odešle se na server HelpButton → odpověď zazní z výstupu pro sluchátka na zvukové kartě (HAT). Po zapnutí Pi se vše spustí samo.

**Potřebný čas:** asi 45 minut (většinu zabere stahování). Během instalace **je potřeba internet**.

---

## Část A – Co budete potřebovat

| # | Položka | Poznámka |
|---|---|---|
| 1 | Raspberry Pi 5 + oficiální napájecí zdroj USB-C 27 W | doporučeno 8 GB (u 4 GB použijte model Whisper `base`) |
| 2 | karta microSD, 32 GB a víc | třída A2 |
| 3 | zvuková karta **Audio Injector Zero** (HAT) | kodek WM8731, vestavěný mikrofon |
| 4 | reproduktor s vlastním zesilovačem (nebo sluchátka) | zapojuje se do 3,5mm konektoru `Phones` na HATu |
| 5 | tlačítko (spínací) + 2 propojovací kabely | volitelné – bez něj funguje klávesnice |
| 6 | LED + rezistor 220–330 Ω + kabely | volitelná stavová kontrolka |
| 7 | počítač ve **stejné Wi-Fi/LAN**, na kterém běží server HelpButton | nebo vestavěný samostatný režim bez serveru |
| 8 | počítač na nahrání systému na kartu SD a připojení k Pi přes SSH | |

---

## Část B – Příprava Pi

### Krok 1 – Nahrajte operační systém
1. Nainstalujte si do počítače **Raspberry Pi Imager**: https://www.raspberrypi.com/software/
2. Zvolte **Device:** Raspberry Pi 5 · **OS:** *Raspberry Pi OS (64-bit)* · **Storage:** svou kartu microSD.
3. Klikněte na **Next → Edit Settings** a vyplňte:
   - **General:** název počítače `helpbutton`, uživatelské jméno (např. `pi`) a heslo, název a heslo Wi-Fi, jazyk a časové pásmo `Europe/Prague`.
   - **Services:** zaškrtněte **Enable SSH** (přihlášení heslem).
4. Klikněte na **Save → Yes → Write** a počkejte na dokončení.

✅ **Kontrola:** Imager hlásí, že zápis proběhl úspěšně.

### Krok 2 – Nasaďte zvukovou kartu a zapojte tlačítko (Pi VYPNUTÉ)
1. Pevně nasaďte **Audio Injector Zero** na 40pinový konektor Pi (všechny piny musí sedět).
2. Reproduktor nebo sluchátka zapojte do 3,5mm konektoru **`Phones`** na HATu. Vestavěný mikrofon HATu otočte k uživateli a dál od reproduktoru.
3. Zapojte tlačítko (použijte průchozí konektor nahoře na HATu; pin 1 je vlevo nahoře, když je karta SD nahoře, liché piny jsou vlevo, sudé vpravo):

| Součástka | Připojit na | Fyzický pin | BCM |
|---|---|---|---|
| 1. nožička tlačítka | signál | **11** (6. shora, levý sloupec) | GPIO 17 |
| 2. nožička tlačítka | zem | **9** (5. shora, levý sloupec, hned vedle pinu 11) | GND |
| anoda LED (+, delší nožička) přes rezistor 220–330 Ω | signál | **13** (7. shora, levý sloupec) | GPIO 27 |
| katoda LED (−) | zem | **6** (3. shora, pravý sloupec) | GND |

   Tlačítko nepotřebuje rezistor (software zapne vnitřní pull-up v Pi).

> ⛔ **Nikdy nepoužívejte** tyto piny – patří HATu: **GPIO 2, 3** (piny 3, 5, I2C) a **GPIO 18, 19, 20, 21** (piny 12, 35, 38, 40, I2S). Firmware je pro tlačítko i LED odmítne.

```text
                 3V3 Power  (Pin 1)   (Pin 2)  5V Power
   [HAT I2C] GPIO 2 (SDA1)  (Pin 3)   (Pin 4)  5V Power
   [HAT I2C] GPIO 3 (SCL1)  (Pin 5)   (Pin 6)  GND  ◄─── katoda LED (−)
                    GPIO 4  (Pin 7)   (Pin 8)  GPIO 14
 tlačítko ───►        GND   (Pin 9)   (Pin 10) GPIO 15
 tlačítko ───►    GPIO 17  (Pin 11)   (Pin 12) GPIO 18 (I2S) [HAT]
 anoda LED ───►   GPIO 27  (Pin 13)   (Pin 14) GND
```

✅ **Kontrola:** HAT sedí rovně, reproduktor je zapojený, kabely tlačítka jsou na pinech 9 a 11.

### Krok 3 – Spusťte Pi a připojte se přes SSH
1. Vložte kartu microSD a zapněte Pi. Počkejte 1–2 minuty (první start je pomalý).
2. Z počítače (PowerShell / Terminál):

```bash
ssh pi@helpbutton.local
```

(použijte své uživatelské jméno; potvrďte otisk klíče a zadejte heslo; když se adresa `.local` nenajde, zjistěte IP adresu Pi v routeru a použijte `ssh pi@<IP>`).

✅ **Kontrola:** vidíte výzvu jako `pi@helpbutton:~ $`.

> **Bez Wi-Fi?** Připojte port USB-C na Pi k notebooku: do `/boot/firmware/config.txt` přidejte pod `[all]` řádek `dtoverlay=dwc2` a do `/boot/firmware/cmdline.txt` za `rootwait` přidejte `modules-load=dwc2,g_ether`. Pak se připojte přes SSH stejně jako výše přes síťový adaptér USB.

---

## Část C – Instalace softwaru

### Krok 4 – Stáhněte projekt
```bash
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/Polmi985/HelpButton.git
cd HelpButton
```
✅ **Kontrola:** `ls raspberry_pi/setup_rpi5.sh` vypíše název souboru.

### Krok 5 – Spusťte instalátor (první průchod)
```bash
bash raspberry_pi/setup_rpi5.sh
```
Trvá to 10–25 minut. Instalátor: nainstaluje systémové balíčky (PortAudio, ALSA, PipeWire-ALSA, ffmpeg, espeak, knihovny pro GPIO) · přidá `dtoverlay=audioinjector-wm8731-audio` do `/boot/firmware/config.txt` · přidá vašeho uživatele do skupin `gpio` a `audio` · vytvoří `.venv` a nainstaluje balíčky Pythonu (Faster-Whisper, Piper, Edge-TTS, sounddevice, gpiozero, WebRTC VAD) · stáhne český hlas Piper do `raspberry_pi/voices/` · vytvoří `raspberry_pi/rpi_button.toml` a `helpbutton.toml` · stáhne model Whisper `small`.

Spouštějte ho jako běžný uživatel – **ne** přes `sudo` (o heslo si řekne sám a pod rootem se spustit odmítne).

✅ **Kontrola:** skript skončí hláškou *„Hotovo!“* a řádkem `✓ Všechny Python knihovny jsou v .venv`. Když vypíše upozornění (`Instalace dokončena s upozorněními`), opravte problém se sítí nebo balíčkem a spusťte ho znovu – opakované spuštění je bezpečné. Když vypíše **„RESTARTUJTE Pi“**, právě se přidal ovladač zvukové karty → pokračujte krokem 6 (restart je nutný).

### Krok 6 – Restartujte Pi, aby se objevila zvuková karta
```bash
sudo reboot
```
Počkejte asi minutu, znovu se připojte (`ssh pi@helpbutton.local`) a spusťte:
```bash
cd ~/HelpButton
aplay -l | grep audioinjectorpi
arecord -l | grep audioinjectorpi
```
✅ **Kontrola:** oba příkazy vypíšou řádek s `audioinjectorpi`.
❌ Nic se nevypsalo? Viz *Řešení potíží → chybí zvuková karta*.

### Krok 7 – Spusťte instalátor znovu (druhý průchod)
Teď už karta existuje, takže jde nastavit a uložit mixér (mikrofon zapnutý se zesílením +20 dB, **výstup na nejhlasitější úrovni bez zkreslení**, bez příposlechu):
```bash
bash raspberry_pi/setup_rpi5.sh
```
(Vše hotové přeskočí; tento průchod trvá asi minutu.)

✅ **Kontrola:** ve výpisu je `Nastavení mixéru uloženo` a seznam zařízení na konci ukazuje vaši zvukovou kartu.

---

## Část D – Test hardwaru

```bash
cd ~/HelpButton
```
Příkazy níže spouštějte z této složky. Virtuální prostředí **nemusíte** aktivovat: při spuštění systémovým Pythonem se `python -m raspberry_pi` do `.venv` přepne samo. (Když hlásí, že `.venv` chybí nebo je neúplné, instalátor nedoběhl – spusťte znovu krok 5.)

### Krok 8 – Zkontrolujte zvuková zařízení
```bash
python -m raspberry_pi --list-audio
```
✅ **Kontrola:** vidíte `PipeWire: běží` a na řádcích **Mikrofon** i **Reproduktor** je `pipewire` (nebo karta `audioinjector…`, když PipeWire neběží).

### Krok 9 – Kompletní test zvuku
```bash
python -m raspberry_pi --test-audio
```
Postupujte podle něj: uslyšíte zvonění → český pozdrav → řeknete jednu větu a odmlčíte se → přehraje se vám váš hlas → text se rozpozná a přečte zpět.

✅ **Kontrola:** všechny 4 fáze skončí hláškou `Test zvuku úspěšně dokončen`.
- Moc potichu nebo nahlas: při každém spuštění se výstup nastaví na nejhlasitější úroveň **bez zkreslení** (HAT `Master` = 0 dB; 100 % by bylo +6 dB a špičky řeči by se ořezávaly) – hlasitost upravte knoflíkem na reproduktoru. Mikrofon se nastaví automaticky (`Input Mux` = `Mic`, mikrofon odtlumený, `Mic Boost` +20 dB). Když log hlásí „Mikrofon je přebuzený“, firmware sám vypne `Mic Boost`.

### Krok 10 – Otestujte tlačítko
```bash
python -m raspberry_pi --no-gpio -v      # nejdřív test klávesnicí (mezerník)
python -m raspberry_pi -v                # pak se skutečným tlačítkem na pinu 11
```
✅ **Kontrola:** po stisku tlačítka (nebo mezerníku) se v logu objeví stisk a začne poslech; v klidu LED slabě svítí. Ukončíte `Ctrl+C`.

---

## Část E – Připojení k HelpButton a spuštění

### Krok 11 – Spusťte server HelpButton (na počítači se serverem)
Na počítači, který ovládá telefon nebo počítač seniora, ve složce projektu:
```bash
python -m helpbutton.server --llm auto
```
Ve firewallu tohoto počítače povolte TCP port **8765**. *(Zkouška bez telefonu: `python -m helpbutton.agents.mock`.)*

### Krok 12 – Spusťte tlačítko na Pi
```bash
python -m raspberry_pi
```
Pi si server najde samo během několika sekund (mDNS). Když se to nepovede, zadejte adresu počítače ručně:
```bash
python -m raspberry_pi --server http://<IP_POCITACE>:8765
```
Server vůbec neběží? S `standalone_fallback = true` (výchozí) spustí Pi vlastní lokální mozek HelpButton podle `helpbutton.toml`.

**Server s párovacím tokenem** (`[helpbutton] token` v `helpbutton.toml` serveru, doporučeno ve sdílené síti): stejný token zapište do `raspberry_pi/rpi_button.toml`:
```toml
[server]
token = "stejný token jako v helpbutton.toml"
```
Než Pi cokoli odešle, ověří si, že server token zná (`/api/hello`). Zařízení v síti, které se za HelpButton jen vydává, nedostane žádný zvuk, text ani klíč. Log pak hlásí `se neprokázal párovacím tokenem` a tlačítko řekne, že server spojení odmítl.

✅ **Kontrola:** log ukazuje `Server … se prokázal párovacím tokenem` (s tokenem) a `Připojeno k HelpButton serveru …`; zmáčkněte tlačítko a řekněte příkaz.

---

## Část F – Automatické spuštění po startu

### Krok 13 – Nainstalujte uživatelskou službu systemd
Musí to být **uživatelská** služba, aby mohla používat zvukovou relaci PipeWire daného uživatele.
```bash
sudo loginctl enable-linger "$USER"
mkdir -p ~/.config/systemd/user
tee ~/.config/systemd/user/helpbutton.service > /dev/null <<EOF
[Unit]
Description=HelpButton Voice Button (Raspberry Pi 5 + Audio Injector Zero)
After=pipewire.service wireplumber.service network-online.target
Wants=pipewire.service wireplumber.service

[Service]
Type=simple
WorkingDirectory=$HOME/HelpButton
Environment="PYTHONUNBUFFERED=1"
ExecStart=$HOME/HelpButton/.venv/bin/python -m raspberry_pi
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now helpbutton.service
```
✅ **Kontrola:** `systemctl --user status helpbutton.service` ukazuje **active (running)**.

### Krok 14 – Závěrečný test
```bash
sudo reboot
```
Počkejte 1–2 minuty, **nepřihlašujte se** a zmáčkněte tlačítko.

✅ **Kontrola:** HelpButton pípne a odpoví. Logy: `journalctl --user -u helpbutton.service -f`.

🎉 **Hotovo.** Příkazy pro každý den:

```bash
systemctl --user restart helpbutton.service   # po úpravě raspberry_pi/rpi_button.toml
systemctl --user stop helpbutton.service      # zastavení (např. před ručním spuštěním testů)
journalctl --user -u helpbutton.service -f    # živé logy
```

---

## Pozdější aktualizace
```bash
cd ~/HelpButton && git pull
bash raspberry_pi/setup_rpi5.sh                 # doinstaluje nové knihovny (hotové kroky přeskočí)
systemctl --user restart helpbutton.service
```

---

## Přehled nastavení (`raspberry_pi/rpi_button.toml`)

Vytvoří se v kroku 5. Upravte ho a pak restartujte službu.

| Sekce | Volba | Výchozí | Význam |
|---|---|---|---|
| `[server]` | `url`, `standalone_fallback` | `"auto"`, `true` | hledání přes mDNS nebo pevná adresa; lokální záložní mozek |
| `[button]` | `gpio_pin`, `led_pin` | `17`, `27` | piny BCM (piny HATu se odmítnou) |
| `[audio]` | `backend` | `"auto"` | `auto` = PipeWire, jinak HAT · `pipewire` · `alsa` (obejde PipeWire, 48 kHz / 2 kanály) |
| `[audio]` | `output_volume_pct` | `100` | hlasitost výstupu nastavená při každém spuštění: `100` = nejhlasitěji bez zkreslení (HAT `Master` 0 dB), nižší = procenta, `0` = neměnit |
| `[audio]` | `hat_mixer_init` | `true` | při spuštění znovu nastavit cestu mikrofonu a vypnout příposlech |
| `[audio]` | `mic_device`, `speaker_device` | `"auto"` | číslo nebo část názvu pro vynucení konkrétního zařízení |
| `[stt]` | `whisper_model`, `whisper_threads` | `"small"`, `4` | `base` = rychlejší; na CPU v int8 |
| `[stt]` | `language` | `"cs"` | `cs` = jen čeština (nejpřesnější a nejrychlejší) · `auto` = i angličtina (demo) |
| `[stt]` | `vocabulary` | `[]` | jména a slova, která má Whisper psát správně, např. `["Denis", "Lenka"]` |
| `[audio]` | `output_loudness` | `true` | hlas HelpButton asi o 7 dB hlasitější (kompresor + limiter s předstihem), bez ořezávání |
| `[tts]` | `engine`, `speech_rate` | `"auto"`, `0.85` | `auto` = piper → edge → server → espeak |

Užitečné přepínače: `--server URL` · `--tts piper|edge|espeak` · `--whisper-model base` · `--language cs|auto` · `--audio-backend alsa` · `--bilingual` (zapne i rozpoznávání angličtiny) · `--no-gpio` · `-v`.
Přepsání proměnnými prostředí: `HELPBUTTON_SERVER`, `HELPBUTTON_RPI_TTS`, `HELPBUTTON_RPI_WHISPER`, `HELPBUTTON_RPI_LANGUAGE`, `HELPBUTTON_RPI_AUDIO_BACKEND`, `HELPBUTTON_RPI_CONFIG`, `HELPBUTTON_PIPEWIRE=0|1`.

Jak se zvuk zpracovává (mikrofon → Whisper → reproduktor), popisuje krok za krokem [AUDIO_PIPELINE.md](AUDIO_PIPELINE.md).

### Jak se tlačítko chová
- **Jeden stisk** → pípnutí, poslech (LED modrá). Skončí přirozenou pauzou (≈0,65 s po krátkém „Ano/Ne“, běžně 2,2 s, 2,8 s když věta dozní).
- **Stisk, zatímco HelpButton mluví nebo pracuje** → vše se okamžitě zastaví (`POST /api/stop`).
- HelpButton se zeptá na ano/ne → **mikrofon otevře sama**; stisk ze zvyku navíc se ignoruje.
- Ochranná doba 450 ms ignoruje nechtěné dvojité stisky; „Cože?“ / „Zopakuj pomaleji“ zopakuje odpověď pomaleji.
- LED: modrá = poslouchá · bliká = pracuje · zelená = hotovo · červená = odmítnuto/chyba · slabě svítí = klid.

---

## Řešení potíží

| Příznak | Řešení |
|---|---|
| Po restartu chybí `audioinjectorpi` (krok 6) | `grep audioinjector /boot/firmware/config.txt` musí ukázat `dtoverlay=audioinjector-wm8731-audio` pod `[all]`; vypněte Pi a znovu nasaďte HAT; restartujte. |
| `--list-audio` nenajde žádná zařízení | `sudo apt install libportaudio2 alsa-utils`; zkontrolujte, že `groups` obsahuje `audio`; odhlaste se a znovu přihlaste. |
| Nahrávka je tichá / „timeout“ | Mikrofon je po zapnutí ztlumený, dokud ho firmware nebo instalátor neodtlumí. Ručně: `amixer -c audioinjectorpi cset name='Input Mux' Mic`, `cset name='Mic Capture Switch' on`, `cset name='Mic Boost Volume' 1`; ve `wpctl status` musí být výchozí vstup `Built-in Audio Stereo`. |
| Pískání / slyšíte vlastní hlas | `Sidetone` = 0, `Output Mixer Mic Sidetone` a `Line Bypass` vypnuté (děje se automaticky); posuňte reproduktor dál od mikrofonu HATu. |
| Zvuk je moc hlasitý nebo zkreslený | Firmware nastaví HAT na 0 dB, nejhlasitější úroveň bez ořezávání. Když jste v `alsamixer` zvedli `Master` nad 0 dB, špičky řeči se ořezávají – restartujte tlačítko (nastaví znovu 0 dB). Jinak ztlumte reproduktor nebo snižte `output_volume_pct`. |
| Zvuk jde do HDMI / do jiného zařízení | `wpctl status`, pak `wpctl set-default <ID zařízení Built-in Audio Stereo>`; nebo `--speaker audioinjector`. |
| `Device or resource busy` s `backend = "alsa"` | Kartu používá PipeWire – použijte `backend = "auto"`. |
| Ručně funguje, ale po startu je ticho | Služba musí být **uživatelská** a se zapnutým linger (krok 13). |
| Tlačítko nic nedělá | Kabely na pinech 9 a 11; `groups` obsahuje `gpio`; `.venv` vytvořil instalátor (potřebuje `python3-lgpio`); hledejte v logu „GPIO … je obsazen“ (nastavili jste pin HATu). |
| Server se automaticky nenajde | Stejná síť; port 8765 otevřený na počítači; použijte `--server http://<IP_POCITACE>:8765`. |
| Český hlas mlčí | `ls raspberry_pi/voices/` musí obsahovat `.onnx` a `.json`; jinak `--tts edge` (přes internet) nebo `sudo apt install espeak-ng`. |
| Odpovědi jsou pomalé | `python -m raspberry_pi --test-audio` vypíše, jak dlouho trval přepis. `small` je kompromis mezi přesností a rychlostí; `--whisper-model base` je rychlejší, ale méně přesný. |
| HelpButton často říká „nerozuměla jsem“ | Mluvte 20–40 cm od HATu. Zkontrolujte, že je zapnutý `Mic Boost` (`amixer -c audioinjectorpi cget name='Mic Boost Volume'` → `values=1`). Jména, která používáte, přidejte do `[stt] vocabulary`. Nechte `language = "cs"`, pokud nepotřebujete angličtinu. Když Pi slyší řeč, ale nedostane text, zeptá se i Whisperu na serveru (pokud má server `faster-whisper`). |
| Řeč zní zkresleně nebo trhaně | Aktualizujte firmware (způsoboval to starý noise gate a limiter). Když log hlásí „Mikrofon je přebuzený“, mluvte o kousek dál od HATu – firmware navíc sám vypne `Mic Boost`. |
| HelpButton je moc potichu / nahlas | `output_loudness = true` (výchozí) zesílí řeč asi o 7 dB bez ořezávání; HAT už je na nejhlasitější úrovni bez zkreslení, takže výslednou hlasitost určuje knoflík reproduktoru. |
| `No module named 'httpx'` / „Chybí Python knihovny“ | Instalátor nedoběhl (`.venv` chybí nebo je neúplné). Spusťte znovu `bash raspberry_pi/setup_rpi5.sh` jako běžný uživatel a zkontrolujte poslední řádek `✓ Všechny Python knihovny jsou v .venv`. |
| Terminál po ukončení neukazuje psané znaky | Ve firmwaru opraveno (terminál se při ukončení obnoví); když se to stane se starou verzí, napište `reset` a Enter. |
| Log GPIO hlásí „pin už používá jiný proces“ | Tlačítko už běží (např. jako služba systemd). Nejdřív ho zastavte: `systemctl --user stop helpbutton.service`. |
| Server se spustil až po Pi | Nic nedělejte – Pi hledá server znovu každých 15 s a připojí se samo. |

<details>
<summary>Ruční instalace (jen když skript selže)</summary>

```bash
sudo apt-get install -y python3-venv python3-pip python3-dev python3-lgpio gpiod libgpiod-dev \
    libportaudio2 portaudio19-dev alsa-utils pipewire-alsa ffmpeg espeak-ng curl
printf '\n[all]\ndtoverlay=audioinjector-wm8731-audio\n' | sudo tee -a /boot/firmware/config.txt
sudo usermod -aG gpio,audio "$USER" && sudo reboot
# po restartu:
cd ~/HelpButton
python3 -m venv --system-site-packages .venv && source .venv/bin/activate
pip install --upgrade pip && pip install -e ".[dev,voice]"
pip install faster-whisper piper-tts edge-tts sounddevice gpiozero webrtcvad-wheels numpy scipy
mkdir -p raspberry_pi/voices && cd raspberry_pi/voices
B=https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/cs/cs_CZ/jirka/medium
curl -LO $B/cs_CZ-jirka-medium.onnx && curl -LO $B/cs_CZ-jirka-medium.onnx.json
cd ../.. && cp raspberry_pi/rpi_button.example.toml raspberry_pi/rpi_button.toml && cp helpbutton.example.toml helpbutton.toml
amixer -c audioinjectorpi sset Master 0dB
amixer -c audioinjectorpi cset name='Input Mux' Mic
amixer -c audioinjectorpi cset name='Mic Capture Switch' on
amixer -c audioinjectorpi cset name='Mic Boost Volume' 1
amixer -c audioinjectorpi cset name='Playback Deemphasis Switch' off
amixer -c audioinjectorpi cset name='Output Mixer HiFi Playback Switch' on
sudo alsactl store
```
</details>
