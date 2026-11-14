# HelpButton – jedno tlačítko, které babičce opraví telefon

**HelpButton** je hlasový pomocník pro seniory, který jen neradí, ale rovnou jedná. Senior zmáčkne fyzické
tlačítko a řekne, co se děje („zmizel mi WhatsApp“, „je to moc malé, nevidím“, „vyskočilo mi okno
a nejde zavřít“). HelpButton pak přes domácí síť převezme jeho telefon s Androidem nebo počítač s Windows
a problém opraví.

Většina starších lidí už chytrý telefon má. Drobnosti jako schovaná aplikace, ztlumený zvuk nebo
podvodné okno ale obvykle končí telefonátem vnoučeti. HelpButton je vyřeší hned. Než cokoli změní, zeptá se,
a když něco vypadá nebezpečně, upozorní rodinu.

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

## Architektura

```
 senior ──► tlačítko  (Raspberry Pi 5 / ESP32 Atom Echo / simulátor v prohlížeči)
             │  nahrávání → detekce řeči → čištění → Whisper ──► text
             │  HTTP API: /api/hello (server prokáže token), /api/utterance, /api/audio
             ▼
          server HelpButton  (FastAPI, helpbutton/server)
             │  brain:        věta → význam, záměr, plán           (Ollama / Claude / Gemini / klíčová slova)
             │  orchestrátor: stavový automat, potvrzování, úrovně bezpečnosti, upozornění rodině
             │  recepty a pevné postupy pro časté opravy │ GUI agent (vizuální model) pro zbytek
             ▼  WebSocket /ws/device  (výzva–odpověď se stejným tokenem)
          démoni na zařízeních:  telefon s Androidem (ADB)  ·  PC s Windows (UI Automation, OCR, myš a klávesnice)  ·  simulovaná zařízení
```

## Technologie

| Oblast | Technologie |
|---|---|
| Server | Python 3.11+, FastAPI, Uvicorn, WebSockets, Pydantic, httpx, zeroconf (mDNS) |
| AI | Ollama (`qwen3-vl`), Anthropic Claude, Google Gemini, služby kompatibilní s OpenAI API (OpenRouter) |
| Hlas | Faster-Whisper (řeč na text), Piper / edge-tts (text na řeč), vlastní detekce řeči a úprava zvuku |
| Android | ADB přes kabel i bezdrátově, s hlídačem, který spojení sám obnoví |
| Windows | pywinauto (UI Automation), Windows.Media.Ocr, mss, pyautogui |
| Hardware | Raspberry Pi 5 + Audio Injector Zero (WM8731), M5Stack Atom Echo (ESP32, Arduino) |
| Testy | pytest, pytest-asyncio (300+ testů včetně celé cesty tlačítko → server → agent) |

## Rychlý start

Potřebujete Python 3.11 nebo novější.

```powershell
git clone https://github.com/Polmi985/HelpButton.git
cd HelpButton
pip install -e ".[dev,claude,pc]"
Copy-Item helpbutton.example.toml helpbutton.toml     # pak upravte [family] a [[contacts]]
```

Spusťte server a simulovaná zařízení (bez hardwaru i bez klíče k AI):

```powershell
python -m helpbutton.server --llm keywords
python -m helpbutton.agents.mock               # druhý terminál: simulovaný telefon a počítač
```

Otevřete <http://localhost:8765/button> a napište nebo řekněte větu, třeba „Zmizel mi WhatsApp“.
Rodinná stránka je na <http://localhost:8765/>.

Pro ovládání **skutečného telefonu s Androidem** zapněte ladění přes USB nebo bezdrátové ladění a spusťte:

```powershell
python -m helpbutton.agents.android --all --name "Telefon babičky"
```

Pro GUI agenta stáhněte lokální vizuální model (`ollama pull qwen3-vl:8b-instruct`), nebo nastavte
`ANTHROPIC_API_KEY` či klíč ke Gemini v `helpbutton.toml`.

Spuštění testů:

```powershell
pytest
```

## Dokumentace

| Dokument | Obsah |
|---|---|
| [docs/NAVOD.md](docs/NAVOD.md) | Kompletní návod: ADB, modely, co vyzkoušet, skilly |
| [docs/RESEARCH_AGENT_AND_BUTTON.md](docs/RESEARCH_AGENT_AND_BUTTON.md) | Návrh systému: propojení částí, přesnost agenta, rychlost, bezpečnostní rozbor (anglicky) |
| [raspberry_pi/README_RPI5.md](raspberry_pi/README_RPI5.md) | Stavba hlasového tlačítka na Raspberry Pi 5 krok za krokem |
| [raspberry_pi/AUDIO_PIPELINE.md](raspberry_pi/AUDIO_PIPELINE.md) | Zpracování zvuku: nahrávání, detekce řeči, čištění, přepis, syntéza, hlasitost |
| [raspberry_pi/ENHANCEMENT_DESIGN.md](raspberry_pi/ENHANCEMENT_DESIGN.md) | Návrh dalších vylepšení tlačítka: rychlejší odezva, potlačení ozvěny, odolnost vůči televizi, spolehlivost |
| [homeassistant/helpbutton.yaml](homeassistant/helpbutton.yaml) | Volitelné napojení na hlasového asistenta Home Assistant |

## Struktura projektu

```
helpbutton/
  server/        aplikace FastAPI, orchestrátor, brain (LLM), GUI agent, recepty, pevné postupy, hlas
  agents/        démoni zařízení: android.py, pc.py (+ nástroje pro GUI a soubory), mock.py
  common/        protokol, bezpečnostní pravidla, kontakty, Set-of-Marks, vyhledání serveru přes mDNS
  skills/        vestavěné skilly (v každé složce SKILL.md)
raspberry_pi/    klient tlačítka pro Raspberry Pi 5: zvuk, detekce řeči, přepis, syntéza, GPIO
firmware/        firmware tlačítka pro ESP32 (M5Stack Atom Echo)
homeassistant/   ukázka napojení na Home Assistant
tests/           testy serveru a agentů
```

## Bezpečnost

HelpButton umí ovládat telefon i počítač, takže s ním zacházejte jako se softwarem pro vzdálený přístup:

- Provozujte ho jen v **důvěryhodné domácí síti** a v `helpbutton.toml` vždy nastavte párovací `token`
  (vygenerujete ho příkazem `python -c "import secrets; print(secrets.token_urlsafe(24))"`).
- Komunikace v místní síti zatím není šifrovaná (TLS je v plánu) a požadavkům z `127.0.0.1` server věří.
- Na zkoušky používejte testovací telefon. Bezdrátové ADB dává nad zařízením plnou kontrolu.

## Stav projektu

Jde o hackathonový prototyp. Recepty, agenti pro Android a Windows i tlačítko na Raspberry Pi byli
vyzkoušeni na skutečných zařízeních. Firmware pro ESP32 Atom Echo na hardwaru zatím ověřený **není**.

## Autoři

- David Polastik
- Adam Kolařík
- Tomáš Kašpárek
- Tomáš Zapletal
- Jan Dvořák

## Licence

[MIT](LICENSE)
