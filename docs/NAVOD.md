# Návod: vyzkoušení HelpButton na počítači a skutečném telefonu

Tlačítko zatím není potřeba. Větu napíšete do terminálu (`python -m helpbutton.cli`) nebo
do stránky <http://localhost:8765/button>.

## 1. Instalace na počítači (server)

V PowerShellu ve složce repozitáře:

```powershell
pip install -e ".[dev,claude,pc]"
Copy-Item helpbutton.example.toml helpbutton.toml
```

V `helpbutton.toml` upravte `[family]` a `[[contacts]]` – jména a čísla, kterým smí HelpButton psát a volat.

## 2. ADB na počítači

1. Stáhněte **SDK Platform-Tools for Windows**: <https://developer.android.com/tools/releases/platform-tools>
2. Rozbalte do `C:\platform-tools`.
3. Přidejte do PATH (jednou, pak otevřete nový terminál):
   ```powershell
   [Environment]::SetEnvironmentVariable("Path", [Environment]::GetEnvironmentVariable("Path", "User") + ";C:\platform-tools", "User")
   ```
4. Ověřte: `adb version`

## 3. Telefon (Android)

**Zapnout možnosti pro vývojáře:** Nastavení → O telefonu → (Samsung: Informace o softwaru)
→ 7× klepnout na **Číslo sestavení**.

### Varianta A – kabel (nejjednodušší na první zkoušku)

1. Nastavení → Možnosti pro vývojáře → zapnout **Ladění USB**.
2. Připojte telefon kabelem, na telefonu potvrďte **Povolit ladění USB** (zaškrtněte „Vždy povolit“).
3. `adb devices` → uvidíte např. `R58N12ABCDE    device` (příklad). To je **serial** vašeho telefonu.

### Varianta B – bezdrátově (Android 11+, telefon i PC na stejné Wi-Fi)

1. Možnosti pro vývojáře → **Bezdrátové ladění** → zapnout.
2. Klepněte na **Spárovat zařízení pomocí párovacího kódu** – ukáže IP:port a šestimístný kód.
3. `adb pair 192.168.1.50:37123` (IP:port z párovacího okna) a zadejte kód.
4. Na hlavní obrazovce Bezdrátového ladění je **IP adresa a port** (jiný port než párovací):
   `adb connect 192.168.1.50:41234`
5. `adb devices` → serial je `192.168.1.50:41234`.

Port se po restartu telefonu mění – server ho hlídá přes mDNS a sám se znovu připojí.

Rychlá kontrola, že ADB funguje: `adb shell pm list packages com.whatsapp`

## 4. Spuštění (tři terminály)

```powershell
python -m helpbutton.server --llm keywords
```

```powershell
python -m helpbutton.agents.android --all --name "Telefon babičky"
```

`--all` vezme telefon, který ukazuje `adb devices` (stejný telefon tam bývá dvakrát – to nevadí).
Konkrétní telefon vyberete přes `--serial` a **skutečný** serial z `adb devices`
(např. `--serial 192.168.1.50:42461`). `R58N12ABCDE` v tomto návodu je jen příklad.

```powershell
python -m helpbutton.cli
```

Volitelně počítač jako druhé zařízení (`--dry-run` nic nemění, jen vypisuje):

```powershell
python -m helpbutton.agents.pc --name "Můj počítač" --dry-run
```

Rodinná stránka: <http://localhost:8765/>. Windows se při prvním spuštění serveru zeptá na
firewall – povolte pro soukromou síť (potřeba jen pro ostatní zařízení v LAN).

## 5. Co vyzkoušet (od nejbezpečnějšího)

| Věta | Co se stane | Jak vrátit |
|---|---|---|
| Rychle se mi to vybíjí | jen přečte baterii | – |
| Je to moc malé, nevidím → ano | zvětší písmo | `adb shell settings put system font_scale 1.0` |
| Nic neslyším → ano | vypne Nerušit, zesílí zvuk | ručně v telefonu |
| Zmizel mi WhatsApp → ano | zapne a otevře WhatsApp | – |
| Napiš Lence, že přijdu později → ano | otevře WhatsApp s textem, **neodesílá** | zavřít chat |
| Zavolej Petrovi → ano | **opravdu zavolá** | zavěsit |
| Volal mi někdo z banky, ať si nainstaluju AnyDesk | odmítne, upozornění rodině | – |

Simulace zmizelého WhatsAppu (aplikace ani data se nesmažou, jen zmizí z plochy):

```powershell
adb shell pm disable-user --user 0 com.whatsapp
```

Pak řekněte „Zmizel mi WhatsApp“ → „ano“. Ruční vrácení: `adb shell pm enable com.whatsapp`

Zkouška pojistky hovoru: během telefonátu řekněte „Je to moc malé“ → HelpButton odmítne.
Režim letadlo nezkoušejte přes bezdrátové ADB – telefon by se odpojil. Přes kabel to jde.

## 6. Jak funguje AI

HelpButton je **hybrid** (TDD v2):

0. **Porozumění větě dělá model z kontextu**, ne podle klíčových slov. Dostane posledních pár vět
   rozhovoru (co HelpButton udělala a kde), která zařízení jsou zapnutá a co je na nich právě otevřené.
   Nejdřív napíše význam vlastními slovy („meaning“, je v logu serveru), pak záměr. „Seznam“ tak
   může být web i seznam věcí, „zprávy“ novinky i SMS – podle situace. Když si není jistý, zeptá se.
   Klíčová slova se použijí jen bez modelu (offline); pevná v kódu zůstává jen ochrana před podvodem.
   Měření (22 vět, jen model): `qwen3-vl:2b-instruct` 21/22 za ~4 s, `qwen2.5vl:3b` 20/22 za ~6,5 s.
   U úkolů napíše model i **plán kroků** (senior ho uslyší v „Mám začít?“) a první krok – stránku
   hledání nebo Nastavení – otevře rovnou kód.
1. **Recepty** – na časté věci (zmizelý WhatsApp, zvuk, internet, písmo, baterie, podvod) má
   pevný, rychlý a spolehlivý postup. AI tu jen rozumí větě a vrátí záměr jako JSON.
2. **GUI agent** – na všechno ostatní („vyskočilo mi okno a nejde zavřít“, „zapni tmavý režim“,
   „najdi v nastavení…“) se HelpButton zeptá „Mám začít?“ a pak ovládá obrazovku sama:
   - **Observe:** telefon/PC pošle snímek obrazovky s očíslovanými prvky [1], [2], [3]…
     K tomu stav prvků (zapnuto/vypnuto, zaškrtnuto, co je v poli napsané), texty z obrazovky
     (nadpisy, hlášky – malý lokální model je ze snímku nepřečte) a na PC seznam otevřených oken.
     Na začátku úkolu dostane model popis zařízení (verze systému, jazyk menu, výchozí prohlížeč).
   - **Think:** Vision model vybere jeden krok („klikni na [3]“, „napiš…“, „posuň dolů“, nebo
     zavolá systémový nástroj, třeba `set_font_scale`).
   - **Act:** zařízení klikne / napíše / posune.
   - **Verify:** nový snímek – a znovu, dokud není hotovo (max. 25 kroků).
   Když si agent neví rady, zeptá se („Mám otevřít Fotky, nebo Obchod Play?“) a vaše odpověď
   ho posune dál. „Stop“ ho kdykoli zastaví.
3. **Vrátit zpět** – „vrať to zpátky“ vrátí poslední změnu (písmo, nebo co agent udělal).
   „Zmenši písmo“ / „Je to moc velké“ písmo zmenší.

Pojistky v kódu (ne jen v promptu): agent nesáhne na bankovnictví, hesla ani AnyDesk,
neklikne na Zaplatit / Odinstalovat / Smazat / Obnovit tovární nastavení, nenapíše PIN ani
kód, nesmí volat ani psát zprávy a text na obrazovce bere jen jako data. Zamčený telefon
neodemyká – požádá seniora.

### Zapnutí GUI agenta (potřebuje Vision model – jeden z nich)

První řádek `python -m helpbutton.cli` / stránka `/button` ukazuje „GUI agent: …“.

- **Hlavní: Ollama lokálně** (zdarma, bez denního limitu). Doporučený je `qwen3-vl:8b-instruct` (~6 GB,
  chce grafiku s 8 GB) – jeden model na porozumění větám, plán i ovládání obrazovky. Rodina Qwen3-VL je
  učená na ovládání počítače a mobilu:
  ```powershell
  ollama pull qwen3-vl:8b-instruct
  python -m helpbutton.server
  ```
  V `helpbutton.toml` je pak v `[llm]` i `[vlm]` `ollama_model = "qwen3-vl:8b-instruct"`. Když je model stažený,
  je v řetězci první (`GUI agent: ollama:qwen3-vl:8b-instruct → openrouter → …`). `ollama ps` má ukazovat
  `100% GPU` – když ukazuje i CPU, grafiku zabírá něco dalšího (zavřete prohlížeč/hry) nebo zmenšete
  `[vlm] ollama_max_pixels`. Cloudové služby jsou záloha, převezmou krok, když lokální model selže.
  **Grafika jen 4 GB:** `qwen3-vl:2b-instruct` (1,9 GB) – věty pochopí dobře, ale složitější úkoly s víc
  kroky jen s nápovědou (první krok vybere z nabídky nástrojů). Bez `-instruct` stáhnete „thinking“
  variantu, která je pomalejší. Cloud jako hlavní: `[vlm] local_first = false`.

  **Kontext lokálního modelu.** Snímek (~1000 tokenů) + pravidla (~2500) + seznam prvků se musí vejít do
  `[vlm] ollama_num_ctx` (výchozí 8192). HelpButton text kroku sama zkracuje (méně prvků, textů a starších kroků),
  aby se vešel. Když se přesto odpověď usekne, je v logu `neplatný JSON (… done_reason=length …)` a HelpButton
  kontext jednou zdvojnásobí (`zvyšuji na 16384`). Natrvalo pak dejte `ollama_num_ctx = 16384` – chce to
  ~2 GB grafiky navíc, s `OLLAMA_KV_CACHE_TYPE=q8_0` (proměnná prostředí pro Ollamu) polovinu.

  **Jak HelpButton „vidí“ obrazovku (jakákoli aplikace).** Prvky oken z UI Automation + **nápisy ze snímku přes
  vestavěné OCR Windows** (`Windows.Media.Ocr`, jen pip balíčky `winrt-*` z `.[pc]`, do Windows se nic
  neinstaluje). Každý viditelný nápis – obsah webu v Chromu, plátno HP Smart, dokument ve Wordu – dostane
  číslo jako tlačítko (`(Text, OCR)`), model nehádá souřadnice. Klik na x,y mimo všechny prvky (ikona bez
  nápisu) se ověří na 2× zvětšeném výřezu (`[vlm] zoom_refine = false` vypne). Model ke každému kliku píše,
  na co kliká – když číslo prvku nesedí, kód ho opraví nebo klik neprovede. OCR čte česky, když ji Windows
  mají jako jazyk; vypnout jde proměnnou `HELPBUTTON_OCR=0` u démona počítače.

  **Když to HelpButton nedokončí**, řekne, co opravdu udělala (z výsledků nástrojů), a na obrazovce (`/button`,
  `helpbutton.cli`) ukáže očíslovaný postup, jak to senior dokončí sám. Postup napíše Vision model z posledního
  snímku obrazovky – pro jakoukoli aplikaci, bez šablon. Kód zahodí postup s příkazovým řádkem, hesly nebo
  placením, zacyklený postup a nápisy, které na obrazovce nejsou (model je dostane zpět a opraví se). Bez
  modelu jen obecná rada. Teprve pak nabídne napsat rodině.
- **Claude** (nejspolehlivější, placené za použití):
  ```powershell
  $env:ANTHROPIC_API_KEY = "sk-ant-..."
  python -m helpbutton.server --llm claude
  ```
- **Gemini** (zdarma s limity): klíč z aistudio.google.com do `helpbutton.toml` → `[keys] gemini_api_key`.
  Při přetížení HelpButton sama zkouší `gemini-3.5-flash` → `gemini-3.5-flash-lite` → `flash-lite-latest` → `flash-latest`.
- **OpenRouter – bezplatné modely jako záloha** (klíč z openrouter.ai → Keys, limit ~50 dotazů/den):
  v `helpbutton.toml` jsou bloky `[[providers]]`, do všech stejný `api_key`. Ověřené modely (11/2026):

  | Blok | Model | Na co | Rychlost |
  |---|---|---|---|
  | `openrouter` | `nvidia/nemotron-3.5-super-vl:free` | obrazovka | ~4 s |
  | `openrouter-gemma` | `google/gemma-4-31b-it:free` | obrazovka | často přetížený (429), pak se přeskočí |
  | `openrouter-dots` | `dots-studio/dots-3-note-preview:free` | obrazovka | ~15 s |
  | `openrouter-auto` | `openrouter/free` | obrazovka, poslední záloha | 5–18 s |
  | `openrouter-text` | `baidu/ernie-5.5-flash-preview:free` | porozumění větám, otázky | ~2 s |

  Pořadí: Gemini → bloky v pořadí v `helpbutton.toml`. Když model zmizí nebo přestane být zdarma, server vypíše
  do logu dostupné modely. Konkrétní model vynutíte `--vlm openrouter` / `--llm openrouter-text`.
  Kód umí i jiné služby ve stylu OpenAI API (Groq, Z.ai, NVIDIA…) – stačí přidat další `[[providers]]`.

Bez Vision modelu běží jen recepty a na ostatní HelpButton nabídne napsat rodině.

Na telefonu: odemkněte ho (nebo nastavte zámek bez PINu na testovacím telefonu) – agent displej
sám rozsvítí, ale PIN nikdy nezadá. Psaní na telefonu umí jen bez diakritiky (omezení `adb input`).

Volba porozumění větě (první řádek `python -m helpbutton.cli` ukazuje, co běží):

- `--llm keywords` – bez AI, klíčová slova, offline, zdarma. Na zkoušení stačí.
- Claude (placené za použití):
  ```powershell
  $env:ANTHROPIC_API_KEY = "sk-ant-..."
  python -m helpbutton.server --llm claude
  ```
- **Hlavní: Ollama lokálně** (zdarma, bez denního limitu): stejný `qwen3-vl:2b-instruct` jako GUI agent,
  takže v grafice je jen jeden model. Když je stažený, použije se sám, bez parametrů. OpenRouter a Gemini
  jsou pak záloha. Cloud jako hlavní: `[llm] local_first = false`.

## 6a. Pevné postupy – časté úkoly bez modelu

Nejčastější úkoly dělá kód sám nástroji (`helpbutton/server/routines.py`), spolehlivě a rychle – model kliká až
když postup nesedí nebo selže (a dostane, co už kód udělal):

| Věta (příklad) | Co se stane |
|---|---|
| „Pusť mi nejoblíbenější video od Vidraila“ (PC i mobil) | vyhledá jako data, vybere video kanálu, otevře přímo jeho adresu |
| „Vytvoř na ploše složku pejsek a dej tam dnešní stažené soubory“ | složka → dnešní soubory (bez instalátorů) → přesun → otevře složku |
| „Vytvoř v Dokumentech složku Recepty“ | vytvoří a otevře |
| „Kde mám ten dopis pro Janu?“ | najde (i bez diakritiky, jiný pád) a otevře nejnovější |
| „Otevři mi kalkulačku“ | spustí aplikaci (česky i na anglických Windows) |
| „Počítač je pomalý“ / „Tiskárna netiskne“ | zjistí stav a řekne srozumitelně, co s tím |
| „Vyfoť to“ (mobil) | vyfotí JEDNOU a ověří, že fotka přibyla v galerii |

Kontakty se hledají i podle pádu, zdrobněliny a s překlepem („Jirkovi“ → Jiří, „Honzovi“ → Jan,
„sestře Lence“ → Lenka); když nic nesedí, HelpButton nabídne podobná jména. Na „Mám začít?“ stačí i „no tak
dobře“; nejasná odpověď příkaz nezruší (HelpButton se zeptá znovu), po třetí ho odloží a „zkus to znovu“ ho do
5 minut vrátí. Agent nikdy nezopakuje stejný krok počtvrté a před opakováním úspěšného kroku se zeptá,
jestli už není hotovo.

## 6b. Skilly – co HelpButton umí a jak ji naučit nové věci

Skill je složka se souborem `SKILL.md`: popis, kdy se použije (klíčová slova), postup pro HelpButton a postup
pro seniora (když to HelpButton nedokončí). Do promptu jde jen skill, který k větě sedí. Vestavěné jsou
v `helpbutton/skills/`, vlastní (rodina) dejte do `data/skills/<jmeno>/SKILL.md` – stejné jméno přepíše vestavěný.

| Skill | Co umí | Nástroje (bez klikání) |
|---|---|---|
| `youtube` | pustit video, nejsledovanější / nejnovější / od kanálu | `web_youtube_search` → `open_and_fix_browser` |
| `stazene-soubory` | dnešní stažené soubory do složky | `list_files`, `create_folder`, `move_files` |
| `hledani-souboru` | „kde mám ten dopis“ – najít a otevřít | `find_files`, `open_item` |
| `slozky` | nová složka, otevřít složku, uklidit plochu | `create_folder`, `move_files`, `open_item` |
| `aplikace` | spustit Word, Fotky, Kalkulačku, HP Smart… | `start_app`, `focus_window` |
| `prohlizec` | stránky, hledání, větší písmo, cookies, podvodné stránky | `open_and_fix_browser` |
| `tisk` | vytisknout, tiskárna offline, HP Smart | `list_printers`, `print_file` |
| `word-dopis` | napsat a uložit dopis | `start_app` |
| `pocasi` | počasí (odpoví hned, bez klikání) | `web_weather` |
| `wikipedie` | „kdo byl Karel Čapek“ (odpoví hned) | `web_wikipedia` |
| `pomaly-pocitac` | pomalý / plný počítač, baterie | `pc_health` |
| `fotky` | prohlížet fotky od vnoučat | `list_files`, `open_item` |
| `podvod-okna` | „mám virus“, „vyhrál jsem“ – bezpečně zavřít | – |
| `email` | přečíst poštu (nikdy se nepřihlašuje) | `open_and_fix_browser` |
| `pismo-a-obrazovka` | větší písmo, lupa, tmavý režim | `open_settings`, `start_app` |

**Nástroje** jsou pevné funkce démona a serveru – model nikdy nepíše vlastní příkazy do PowerShellu.
Některé uvnitř pouští předem napsaný PowerShell skript (`start_app` = `Get-StartApps`, `list_printers` =
`Win32_Printer`). Soubory jen v uživatelských složkách, nic se nemaže ani nepřepisuje, programy (`.exe`,
`.bat`…) se nespouští. Webové nástroje běží na serveru jen proti pevným službám (YouTube, Open-Meteo,
Wikipedie); jejich výsledek je pro model jen DATA. Pro počasí bez města dejte do `[helpbutton]` `home_city = "Brno"`.

Vlastní skill (příklad `data/skills/tiskarna-dedy/SKILL.md`):
```markdown
---
name: tiskarna-dedy
title: Tisk na dědově Canonu
description: Vytisknout cokoli na tiskárně Canon v obýváku
keywords: tisk, tiskn, canon
devices: pc
tools: list_printers, print_file
first_tool: list_printers
---
## Postup pro HelpButton
1. list_printers – tiskárna se jmenuje „Canon MG3600“; když je offline, řekni dědovi, ať ji zapne vzadu.
## Pro seniora
1. Zapněte tiskárnu tlačítkem vpravo nahoře, rozsvítí se zeleně.
```

## 7. Bezpečnost v síti

- **Párovací token** (doporučeno): v `helpbutton.toml` → `[helpbutton] token = "…"` a stejný token démonům
  (`--token …` nebo `$env:HELPBUTTON_TOKEN`). Server a démon se jím ověří navzájem (challenge–response,
  token po síti nikdy nejde). Démon tak ignoruje podvržený server ohlášený v síti přes mDNS
  a server nepustí cizí „zařízení“. Bez tokenu to obě strany hlásí varováním v logu.
- **Stránky a API z jiných počítačů jen s klíčem** (když je token nastavený). Ze stejného počítače
  (`localhost`) se nic nemění. Z jiného počítače:
  - **prohlížeč** (`/`, `/button`) přesměruje na `/login` – zadáte párovací token, prohlížeč z něj spočítá
    klíč (token po síti nejde) a zapamatuje si ho v cookie,
  - **`helpbutton.cli`**: `python -m helpbutton.cli --server http://IP:8765 --token "…"` (nebo `$env:HELPBUTTON_TOKEN`),
  - **Raspberry Pi**: `rpi_button.toml` → `[server] token = "…"` (nebo `HELPBUTTON_TOKEN`),
  - **ESP32 a Home Assistant** dostanou přímo klíč: `python -m helpbutton.server --api-key` a vložit do
    `HELPBUTTON_API_KEY` ve firmwaru, resp. do hlavičky `Authorization: Bearer …` v `homeassistant/helpbutton.yaml`.

  Po 10 špatných klíčích z jedné adresy server 10 minut nic nepustí. HTTP v síti není šifrované: kdo by
  klíč odposlechl, mohl by HelpButton psát věty, ale vydávat se za server a ovládat zařízení chce token. V cizí
  síti (hackathon, kavárna) je proto bezpečnější hotspot z mobilu.
- Webové stránky z internetu na server nedosáhnou: cizí `Origin` (CSRF, cizí WebSocket) i cizí
  doménové jméno v `Host` (DNS rebinding) dostanou 403. IP adresy, `localhost`, jména bez tečky
  a `*.local` projdou; vlastní doménu přidejte do `[helpbutton] allowed_hosts`.
- Na počítači agent nepíše do konzole, PowerShellu, registrů ani Správce úloh (smí z nich jen
  přepnout jinam), nepoužije Win+R / Win+X / Win+L / Ctrl+Shift+Esc, Alt+F4 na ploše (vypnutí)
  a nenapíše „powershell“, „cmd“… do hledání. Neklikne na Vypnout / Restartovat / Napájení.
- Historie běhů agenta (`data/agent_runs`, jsou v ní snímky obrazovky) se po 14 dnech sama maže
  (`[helpbutton] agent_runs_keep_days`).

## 8. Když něco nejde

- Démona spusťte s `-v` a pošlete výpis chyby.
- `adb devices` ukazuje `unauthorized` → potvrďte dialog na telefonu.
- `offline` v `adb devices` → `adb disconnect` a znovu `adb connect`.
- HelpButton říká, že telefon není připojený → démon telefonu neběží, nebo běží se špatným `--serial`
  (na rodinné stránce je telefon odpojený). Spusťte ho s `--all`.
- Agent říká „Telefon je zamčený“ → odemkněte ho a řekněte „hotovo“.
- Agent na počítači hlásí „V systémovém okně … nic nedělá“ → v popředí je konzole (třeba ta se
  serverem). Minimalizujte ji, nebo agent sám přepne jinam.
- Agent se ztratí nebo zacyklí → otevřete `data/agent_runs/<čas>-<úkol>/report.html`: u každého kroku
  je snímek, který model viděl, jeho myšlenka, poznámky, akce a výsledek. Stejné údaje jsou
  v `steps.jsonl`. Ze skončených běhů si agent ukládá zkušenosti do `data/agent_memory.jsonl` a
  u podobného úkolu je příště dostane („minule se povedlo takhle“ / „tohle minule selhalo“).
  Smazáním souboru ji vynulujete.
- „Došel mi dnešní limit bezplatné umělé inteligence“ → OpenRouter bez kreditů dovolí jen omezený
  počet dotazů na modely `:free` za den a všechny bloky se stejným klíčem sdílí jeden limit.
  Dobijte 10 kreditů (limit stoupne na 1000/den), nebo přidejte službu s jiným klíčem (Gemini, Groq).
- Některé příkazy pro Android (zvuk, archivované aplikace) se mezi verzemi a výrobci liší.
