<p align="center"><img src="../app/assets/jirik.png" width="112" alt="Jiřík"></p>

# Jiřík – chat, který ovládá tvůj počítač

**Jiřík** je druhá tvář HelpButton: místní AI asistent pro toho, kdo u počítače sedí sám. Běží na stejném serveru
jako tlačítko pro seniora, má vlastní webový chat (`/chat`) a desktopovou aplikaci (Electron, složka `app/`).
Model, hlas i ovládání běží u tebe, nic neodchází do cloudu.

## Co Jiřík umí

- **Chatovat** jako běžný chatbot. Odpovědi proudí živě, umí tabulky, kód a vložené obrázky (Ctrl+V nebo přetažení).
- **Přemýšlet.** U vyšších úrovní ukazuje své uvažování živě (rozbalovací blok „Přemýšlím…“).
- **Pracovat samostatně na dlouhých úkolech.** Přepínač **Agent** (v poli zprávy i v Nastavení) ho udrží v práci
  přes desítky kroků. Vytvoří si seznam úkolů (`task_plan`), u složitých zadání i kartu plánu (`create_plan`),
  ukazuje postup a při předčasném zastavení sám naváže, dokud není výsledek ověřený.
- **Hlídat kontext a paměť.** V Nastavení se volí velikost kontextu (`4k`–`64k` nebo vlastní počet tokenů),
  v záhlaví je vidět, jak je zaplněný. Když se zaplní, Jiřík uvolní nejstarší tahy i staré vzpomínky (`remember`)
  a neztratí přitom aktuální cíl ani plán.
- **Ovládat počítač jako člověk.** Vidí obrazovku (snímek s očíslovanými prvky a souřadnicemi), hýbe myší, kliká,
  přetahuje, píše, používá zkratky a po každém kroku si ověří nový snímek.
- **Rovnou dělat věci.** PowerShell (soubory, nastavení, procesy, instalace přes winget), otevírání aplikací,
  webů a souborů, hledání a čtení webu, počasí, YouTube.
- **Mluvit** českým hlasem Jirka (Piper). Řeč přehrává server, takže i s více otevřenými okny zazní každá věta jednou.
- **Ovládat další počítače** v záložce **Zařízení** (Jiřík Link). Přidání zabere minutu, spící počítač se sám probudí.

## Úrovně myšlení

Vybírají se v okně zprávy (tlačítko s pruhy). Jiřík u každé úrovně ukáže, jestli se vejde do grafiky tvého
počítače, jak je rychlá a kolikrát je pomalejší než Nízká. Chybějící úroveň se stáhne jedním kliknutím.

| Úroveň | Model | Stažení | Pro co |
|---|---|---|---|
| **Nízká** | `qwen3.5:4b` | 3,4 GB | okamžité odpovědi, běžné úkoly |
| **Střední** | `qwen3.5:9b` | 6,6 GB | chytřejší odpovědi, spolehlivější klikání |
| **Chytrá** | `qwen3.5:9b` + přemýšlení | (stejný) | plánování, složitější úkoly |
| **Ultra** | `qwen3.5:27b` + přemýšlení | 17 GB | nejlepší výsledky, potřebuje silný počítač |

Všechny velikosti Qwen3.5 umí text, obrázky, nástroje i přemýšlení a používají stejné souřadnice obrazovky,
takže ovládání myší funguje stejně na každé úrovni. **Necenzurovaný režim** (přepínač v nabídce i v Nastavení)
přepne na abliterated verze stejných modelů (`huihui_ai/qwen3.5-abliterated`).

Na grafice se 6 GB (např. RTX 3050) běží Nízká celá v grafice (~45 tokenů/s), Střední a Chytrá částečně v RAM
(~9 tokenů/s). Ultra je na takový počítač příliš velká.

## Instalace na Windows

1. Stáhni z [Releases](https://github.com/Polmi985/HelpButtonJirik/releases/latest) soubor
   `HelpButtonJirik-v0.2.0-win.zip` a rozbal ho.
2. Dvojklik na **`Setup.bat`**. Doinstaluje Python, Node.js a Ollamu (přes winget), knihovny, hlas Jirka,
   model pro úroveň Nízká, vytvoří `helpbutton.toml` a na ploše ikonu **Jiřík**.
3. Spusť Jiříka ikonou na ploše nebo přes **`Jirik.bat`**. Když instalace ještě neproběhla, spustí se sama.

Požadavky: Windows 10/11, doporučeno 16 GB RAM, grafika NVIDIA výhodou (jde to i bez ní, jen pomaleji),
asi 15 GB volného místa. Záložní spuštění bez aplikace (chat v prohlížeči): **`Start Jirik.bat`**.

Jméno a oslovení si nastavíš v `helpbutton.user.toml` (git ho ignoruje):

```toml
[jirik]
user_name = "Adam"
greeting = "Adame"
```

Aplikace spustí server HelpButton na portu 8765, takže na stejném počítači funguje i tlačítko pro seniora,
rodinná stránka (`/`) a simulátor tlačítka (`/button`).

## Další počítače (záložka Zařízení, Jiřík Link)

1. V Jiříkovi otevři **Zařízení → Přidat zařízení → Začít**.
2. Na druhém počítači ve stejné síti otevři v prohlížeči zobrazenou adresu a spusť stažený instalátor.
3. Oba počítače ukážou **stejný šestimístný kód**. Potvrď v Jiříkovi „Kódy se shodují“. Hotovo.

Pak stačí vybrat počítač vlevo a napsat, co na něm udělat. Náhled jeho obrazovky je pod seznamem.

Na druhém počítači běží jen malý hlídač (~13 MB RAM, nulové CPU). Když ho Jiřík potřebuje, pošle mu podepsanou
zprávu a hlídač spustí plné ovládání, které se po 10 minutách bez práce zase ukončí. Když příchozí probouzení
blokuje firewall nebo Veřejná síť, hlídač přejde na odchozí TLS spojení. Instalátor si sám najde nebo nainstaluje
Python 3.11+, opraví poškozené `.venv`, po souhlasu přepne síť na Soukromou a zapíše záznam do
`%LOCALAPPDATA%\JirikLink\logs\install.log`. Spící počítač Jiřík probudí přes **Wake-on-LAN**.

## Zabezpečení

- **Chat jde z jiných počítačů otevřít jen s klíčem.** Bez `[helpbutton] token` v `helpbutton.toml` je `/chat`
  a jeho API dostupné jen z tohoto počítače (`127.0.0.1`), s tokenem jen po přihlášení na `/login`.
- **Párování bez důvěry v síť:** výměna klíčů X25519 a ověření šestimístným kódem. Klíč zařízení se předá
  až po potvrzení, šifrovaný AES-256-GCM. Párovací odkaz platí 15 minut a jen jednou, instalační balíček
  neobsahuje žádná tajemství a instalátor ověřuje jeho SHA-256.
- **Spojení:** TLS 1.2+ s připnutým certifikátem serveru a vzájemné ověření HMAC-SHA256 klíčem zařízení.
  Probouzení je podepsané, s časovým razítkem a ochranou proti přehrání.
- **Klíče na disku** jsou šifrované Windows DPAPI. Každé zařízení má vlastní 256bit klíč, odebráním v aplikaci
  okamžitě přestane platit.
- **Pojistky v kódu**, které model nepřebije: žádné formátování disků, mazání systémových složek, vypínání
  antiviru či firewallu, změny uživatelských účtů ani spouštění kódu staženého z internetu. Hesla a ověřovací
  kódy Jiřík nepíše, do bankovnictví, správců hesel a vzdáleného přístupu nesahá.
- Nástroje Jiříka (PowerShell, volná myš a klávesnice) nedostane GUI agent ani plánovač tlačítka pro seniora.

**Proč Jiřík neodemyká zamčený počítač:** odemknutí obrazovky na dálku by znamenalo uložit heslo k Windows
a vložit do přihlašovací obrazovky komponentu běžící jako SYSTEM. Přesně to dělá malware. Jiřík místo toho
počítač probudí, během práce ho nenechá usnout ani zamknout, a když je zamčený, řekne to.

## Řešení potíží

| Potíž | Řešení |
|---|---|
| „Ollama neběží“ | spusť aplikaci Ollama, nebo znovu `Setup.bat` |
| „Model chybí“ | v nabídce Myšlení klikni u úrovně na stažení |
| Zařízení je „Nedostupné“ | je zapnuté, přihlášené a ve stejné síti? Síť musí být „Soukromá“ |
| Pomalé odpovědi | nižší úroveň myšlení; zavři hry a programy, které zabírají grafiku |
| Tlačítku pro seniora chybí paměť grafiky | `[jirik] warmup = false` v `helpbutton.toml` |
| Záznamy | `data/logs/` (aplikace, server, ovládání, instalace) |

## Kód Jiříka

| Cesta | Co to je |
|---|---|
| `app/` | Electron: okno, spuštění všeho, rámeček a lišta při práci, ikona |
| `helpbutton/server/chat.py` | konverzace: úrovně, přemýšlení, nástroje, ovládání obrazovky, dva prostory |
| `helpbutton/server/hardware.py`, `models.py` | úrovně myšlení, odhad toho, co počítač zvládne, stahování modelů |
| `helpbutton/server/link.py` | Jiřík Link: párování, TLS brána, probouzení, Wake-on-LAN |
| `helpbutton/server/static/chat.html` | webový chat |
| `helpbutton/link/` | hlídač, párovací klient a instalátor pro druhý počítač |
| `helpbutton/agents/pc.py` | ovládání počítače (společné pro Jiříka i tlačítko) |
| `run.py`, `Setup.bat`, `setup.ps1`, `Jirik.bat` | spuštění a instalace na Windows |
