# HelpButton – jedno tlačítko, které babičce opraví telefon

**HelpButton** je hlasový pomocník pro seniory, který jen neradí, ale rovnou jedná. Senior zmáčkne fyzické
tlačítko a řekne, co se děje („zmizel mi WhatsApp“, „je to moc malé, nevidím“, „vyskočilo mi okno
a nejde zavřít“). HelpButton pak přes domácí síť převezme jeho telefon s Androidem nebo počítač s Windows
a problém opraví.

Většina starších lidí už chytrý telefon má. Drobnosti jako schovaná aplikace, ztlumený zvuk nebo
podvodné okno ale obvykle končí telefonátem vnoučeti. HelpButton je vyřeší hned. Než cokoli změní, zeptá se,
a když něco vypadá nebezpečně, upozorní rodinu.

Na stejném serveru běží i **Jiřík**: místní AI chat s hlasem pro toho, kdo u počítače sedí sám. Jiřík ovládá
tento počítač myší, klávesnicí i PowerShellem a přes **Jiřík Link** i další počítače v síti. Má webový chat
a desktopovou aplikaci pro Windows.

> Vzniklo v roce 2026 jako hackathonový projekt týmu **pan.png**.

---

## Co HelpButton umí

- **Fyzické tlačítko (push-to-talk).** Raspberry Pi 5 se zvukovou kartou (nebo M5Stack Atom Echo)
  nahraje řeč, vyčistí ji, přepíše ji lokálně pomocí Faster-Whisper a odpověď přečte nahlas.
- **Rozumí větě z kontextu.** Jazykový model dostane větu spolu s posledními větami rozhovoru a tím,
  co je právě na obrazovce. Pochopí význam, záměr a sestaví plán kroků. Když si není jistý, doptá se.
- **Recepty na časté potíže.** Zmizelý WhatsApp, nefunkční zvuk, internet, malé písmo, baterie nebo
  podvodné hovory řeší pevné, otestované postupy, ne jazykový model.
- **GUI agent na všechno ostatní.** Smyčka *pozoruj → přemýšlej → jednej → ověř*. Zařízení pošle snímek
  obrazovky s očíslovanými prvky (Set-of-Marks, UI Automation, OCR Windows), vizuální model vybere jeden
  krok, kód ho zkontroluje a zařízení ho provede. Opakuje se to nejvýš 25×.
- **Pojistky v kódu, ne jen v promptu.**
  - Agent nesáhne na bankovnictví, hesla ani nástroje pro vzdálený přístup jako AnyDesk.
  - Nikdy neklikne na *Zaplatit / Odinstalovat / Smazat / Obnovit tovární nastavení* a nenapíše PIN.
  - Text na obrazovce bere jen jako data, ne jako pokyny.
  - Akce mají úrovně zelená / oranžová / červená: riskantní čekají na vyslovené „ano“, nebezpečné
    HelpButton odmítne a nahlásí rodině.
- **Především lokálně.** Běží v domácí síti s lokálním modelem v Ollamě (`qwen3-vl`). Claude, Gemini
  a bezplatné modely z OpenRouteru jsou volitelná záloha.
- **Rozšiřitelné přes skilly.** Každý skill je složka se souborem `SKILL.md` (YouTube, soubory a složky,
  tisk, e-mail, počasí, podvodná okna a další). Rodina může přidat vlastní, aniž by sahala do kódu.
- **Bezpečné párování.** Zařízení i tlačítko se spárují sdíleným tokenem s ověřením výzva–odpověď (HMAC).
  API má povolené seznamy pro Host a Origin, blokování po opakovaných chybách a limity velikosti nahrávek.
- **Rodinná stránka.** Webová stránka jen pro čtení ukazuje, co HelpButton udělal. Simulátor tlačítka
  v prohlížeči umožní vše vyzkoušet bez hardwaru.

## Co umí Jiřík

- **Chat jako běžný chatbot**, s živými odpověďmi, tabulkami, kódem a vloženými obrázky.
- **Úrovně myšlení** od Nízké (`qwen3.5:4b`, 3,4 GB) po Ultra (`qwen3.5:27b`). U každé ukáže, jestli se vejde
  do grafiky tvého počítače, a chybějící model stáhne jedním kliknutím.
- **Režim Agent** pro dlouhé úkoly: seznam úkolů, karta plánu, průběžný postup a navázání až do ověřeného konce.
- **Ovládá počítač jako člověk:** vidí obrazovku, hýbe myší, píše, používá zkratky a po každém kroku si výsledek
  ověří. Umí i PowerShell, otevírání aplikací, webů a souborů, hledání na webu, počasí a YouTube.
- **Mluví** českým hlasem Jirka (Piper). Řeč přehrává server, takže i s více okny zazní každá věta jednou.
- **Jiřík Link:** další počítače v síti se přidají za minutu (šestimístný kód, TLS s připnutým certifikátem),
  spící počítač se probudí přes Wake-on-LAN.
- **Desktopová aplikace** (Electron) spustí Ollamu, server i ovládání počítače a při práci orámuje obrazovku.

Podrobnosti o Jiříkovi jsou v [docs/JIRIK.md](docs/JIRIK.md).

## Architektura

```
 senior ──► tlačítko  (Raspberry Pi 5 / ESP32 Atom Echo / simulátor v prohlížeči /button)
             │  nahrávání → detekce řeči → čištění → Whisper ──► text
             │  HTTP API: /api/hello (server prokáže token), /api/utterance, /api/audio
             ▼
          server HelpButton  (FastAPI, helpbutton/server, port 8765)
             │  brain:        věta → význam, záměr, plán           (Ollama / Claude / Gemini / klíčová slova)
             │  orchestrátor: stavový automat, potvrzování, úrovně bezpečnosti, upozornění rodině
             │  recepty a pevné postupy pro časté opravy │ GUI agent (vizuální model) pro zbytek
             │
             │  Jiřík (chat.py): /chat, /ws/chat, úrovně myšlení (Qwen3.5), hlas Jirka, nástroje pro PC
             ▲     └─ aplikace Jiřík (Electron, app/) nebo prohlížeč
 uživatel ───┘
             ▼  WebSocket /ws/device  (výzva–odpověď se stejným tokenem)
          démoni na zařízeních:  telefon s Androidem (ADB)  ·  PC s Windows (UI Automation, OCR, myš a klávesnice)
                                 ·  simulovaná zařízení
             ▼  Jiřík Link (TLS brána 8771, probouzení UDP 8772, párování 8773)
          další počítače v síti:  malý hlídač → při práci plné ovládání PC
```

## Technologie

| Oblast | Technologie |
|---|---|
| Server | Python 3.11+, FastAPI, Uvicorn, WebSockets, Pydantic, httpx, zeroconf (mDNS) |
| AI | Ollama (`qwen3-vl` pro tlačítko, `qwen3.5` pro Jiříka), Anthropic Claude, Google Gemini, služby kompatibilní s OpenAI API (OpenRouter) |
| Hlas | Faster-Whisper (řeč na text), Piper / edge-tts (text na řeč), vlastní detekce řeči a úprava zvuku |
| Android | ADB přes kabel i bezdrátově, s hlídačem, který spojení sám obnoví |
| Windows | pywinauto (UI Automation), Windows.Media.Ocr, mss, pyautogui, PowerShell |
| Jiřík Link | cryptography (X25519, AES-256-GCM), TLS s připnutým certifikátem, Windows DPAPI, Wake-on-LAN |
| Aplikace | Electron (okno chatu, rámeček a stavová lišta při práci) |
| Hardware | Raspberry Pi 5 + Audio Injector Zero (WM8731), M5Stack Atom Echo (ESP32, Arduino) |
| Testy | pytest, pytest-asyncio (390+ testů včetně celé cesty tlačítko → server → agent) |

## Rychlý start

### Windows jedním kliknutím (Jiřík + server tlačítka)

1. Stáhněte repozitář a dvakrát klikněte na **`Setup.bat`**. Doinstaluje Python, Node.js, Ollamu, knihovny,
   hlas Jirka, model `qwen3.5:4b`, vytvoří `helpbutton.toml` a na ploše ikonu **Jiřík**.
2. Spusťte **Jiřík** z plochy (nebo `Jirik.bat`). Bez aplikace jde i `Start Jirik.bat` – chat se otevře
   v prohlížeči.

Aplikace spustí server na portu 8765, takže na stejném počítači hned běží i rodinná stránka a simulátor tlačítka.

### Ručně (vývoj, Raspberry Pi, Linux)

Potřebujete Python 3.11 nebo novější.

```powershell
git clone https://github.com/Polmi985/HelpButton.git
cd HelpButton
pip install -e ".[dev,claude,pc,jirik]"
Copy-Item helpbutton.example.toml helpbutton.toml     # pak upravte [family] a [[contacts]]
```

Spusťte server a simulovaná zařízení (bez hardwaru i bez klíče k AI):

```powershell
python -m helpbutton.server --llm keywords
python -m helpbutton.agents.mock               # druhý terminál: simulovaný telefon a počítač
```

Otevřete <http://localhost:8765/button> a napište nebo řekněte větu, třeba „Zmizel mi WhatsApp“.

Pro ovládání **skutečného telefonu s Androidem** zapněte ladění přes USB nebo bezdrátové ladění a spusťte:

```powershell
python -m helpbutton.agents.android --all --name "Telefon babičky"
```

Pro ovládání **tohoto počítače** (tlačítko i Jiřík) spusťte démona:

```powershell
python -m helpbutton.agents.pc --server ws://127.0.0.1:8765/ws/device --name "Tento počítač"
```

Pro GUI agenta tlačítka stáhněte lokální vizuální model (`ollama pull qwen3-vl:8b-instruct`), nebo nastavte
`ANTHROPIC_API_KEY` či klíč ke Gemini v `helpbutton.toml`. Jiřík potřebuje `ollama pull qwen3.5:4b`.
Přepis řeči na serveru (simulátor v prohlížeči, ESP32) zapnete balíčkem `pip install -e ".[voice]"`.

Spuštění testů:

```powershell
pytest
```

## Stránky serveru

| Adresa | Co tam je |
|---|---|
| <http://localhost:8765/> | rodinná stránka: co HelpButton udělal (jen čtení) |
| <http://localhost:8765/button> | simulátor tlačítka a obrazovka seniora |
| <http://localhost:8765/chat> | chat Jiřík (z jiných počítačů jen s tokenem přes `/login`) |

## Dokumentace

| Dokument | Obsah |
|---|---|
| [docs/NAVOD.md](docs/NAVOD.md) | Kompletní návod: ADB, modely, co vyzkoušet, skilly |
| [docs/JIRIK.md](docs/JIRIK.md) | Jiřík: úrovně myšlení, instalace na Windows, Jiřík Link, zabezpečení, řešení potíží |
| [docs/RESEARCH_AGENT_AND_BUTTON.md](docs/RESEARCH_AGENT_AND_BUTTON.md) | Návrh systému: propojení částí, přesnost agenta, rychlost, bezpečnostní rozbor (anglicky) |
| [raspberry_pi/README_RPI5.md](raspberry_pi/README_RPI5.md) | Stavba hlasového tlačítka na Raspberry Pi 5 krok za krokem |
| [raspberry_pi/AUDIO_PIPELINE.md](raspberry_pi/AUDIO_PIPELINE.md) | Zpracování zvuku: nahrávání, detekce řeči, čištění, přepis, syntéza, hlasitost |
| [raspberry_pi/ENHANCEMENT_DESIGN.md](raspberry_pi/ENHANCEMENT_DESIGN.md) | Návrh dalších vylepšení tlačítka: rychlejší odezva, potlačení ozvěny, odolnost vůči televizi, spolehlivost |
| [homeassistant/helpbutton.yaml](homeassistant/helpbutton.yaml) | Volitelné napojení na hlasového asistenta Home Assistant |

## Struktura projektu

```
helpbutton/
  server/        aplikace FastAPI, orchestrátor, brain (LLM), GUI agent, recepty, pevné postupy, hlas,
                 chat Jiřík (chat.py), úrovně myšlení (hardware.py, models.py), Jiřík Link (link.py)
  agents/        démoni zařízení: android.py, pc.py (+ nástroje pro GUI a soubory), mock.py
  common/        protokol, bezpečnostní pravidla, kontakty, Set-of-Marks, vyhledání serveru přes mDNS, DPAPI
  link/          Jiřík Link pro druhý počítač: hlídač, párování, instalátor
  skills/        vestavěné skilly (v každé složce SKILL.md)
app/             desktopová aplikace Jiřík (Electron)
raspberry_pi/    klient tlačítka pro Raspberry Pi 5: zvuk, detekce řeči, přepis, syntéza, GPIO
firmware/        firmware tlačítka pro ESP32 (M5Stack Atom Echo)
homeassistant/   ukázka napojení na Home Assistant
tools/           ikona na ploše, balíček pro GitHub Release
tests/           testy serveru, chatu, Jiřík Link a agentů
Setup.bat, setup.ps1, Jirik.bat, Start Jirik.bat, run.py    instalace a spuštění na Windows
```

## Bezpečnost

HelpButton i Jiřík umí ovládat telefon i počítač, takže s nimi zacházejte jako se softwarem pro vzdálený přístup:

- Provozujte server jen v **důvěryhodné domácí síti** a v `helpbutton.toml` vždy nastavte párovací `token`
  (vygenerujete ho příkazem `python -c "import secrets; print(secrets.token_urlsafe(24))"`).
- Chat Jiřík spouští PowerShell, proto jde z jiných počítačů otevřít jen s tokenem. Bez tokenu funguje jen
  na počítači, kde běží server.
- Nástroje Jiříka (PowerShell, volná myš a klávesnice) nedostane GUI agent ani plánovač tlačítka pro seniora.
- Spojení přes Jiřík Link je šifrované (TLS s připnutým certifikátem). Komunikace tlačítka a démonů v místní síti
  zatím šifrovaná není (TLS je v plánu) a požadavkům z `127.0.0.1` server věří.
- Na zkoušky používejte testovací telefon. Bezdrátové ADB dává nad zařízením plnou kontrolu.

## Stav projektu

Jde o hackathonový prototyp. Recepty, agenti pro Android a Windows i tlačítko na Raspberry Pi byli
vyzkoušeni na skutečných zařízeních. Jiřík vznikl jako samostatný projekt a teď běží na stejném serveru
jako tlačítko. Firmware pro ESP32 Atom Echo na hardwaru zatím ověřený **není**.

## Autoři

- David Polastik
- Adam Kolařík
- Tomáš Kašpárek
- Tomáš Zapletal
- Jan Dvořák

## Licence

[MIT](LICENSE)
