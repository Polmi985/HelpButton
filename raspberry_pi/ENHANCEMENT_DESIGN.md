# Technický návrh: další generace firmwaru a vylepšení UX hlasového tlačítka HelpButton na Raspberry Pi 5

## Shrnutí

Současné hlasové tlačítko na Raspberry Pi 5 (`raspberry_pi/`) má kompletní, funkční hlasovou smyčku „odolnou i pro dědu“ (automatické vyhledání zvuku přes USB, VAD s předstihem 500 ms a tolerancí pauzy 2,2 s, přepis Faster-Whisper, vícestupňové TTS a automatické doplňující otázky). Z **funkčního prototypu** z něj ale můžeme udělat **okamžitě reagující spotřebič pro péči o seniory bez jakéhokoli tření**.

Optimalizací zvukové cesty, plánování inference, zpracování akustického signálu a ergonomie pro člověka můžeme zkrátit **vnímanou dobu odezvy z ~2,8–3,7 sekundy pod 350 milisekund**. Zároveň výrazně zlepšíme srozumitelnost řeči při věkem podmíněné nedoslýchavosti (*presbyakuzi*) a odolnost vůči hluku televize v obýváku.

---

## 1. Zkrácení odezvy od začátku do konce (z ~3,5 s pod 350 ms)

### Rozpad současných úzkých míst
V běžné tahové smyčce se zpoždění po dokončení řeči sčítají za sebou:
- **Dozvuk ticha VAD:** `2200 ms` (čekání na jistotu, že děda větu dořekl)
- **Inference Whisper STT:** `850 ms` (dávkové kódování a dekódování na CPU)
- **Server + diagnostika přes ADB:** `350 ms` (síť a dotaz na telefon)
- **Syntéza celého WAV v TTS:** `250 ms` (syntéza celého odstavce před přehráním)
- **Otevření streamu ALSA:** `80 ms` (otevření zvukové karty USB)
- **Celkové vnímané čekání:** `~3,73 sekundy`

### 1.1 Průběžný přepis po malých dávkách během řeči (nulové zpoždění přepisu po řeči)
- **Jak to funguje:** Místo čekání, až děda domluví, a teprve pak spuštění Faster-Whisperu běží na 4 jádrech Cortex-A76 v Raspberry Pi 5 pracovník na pozadí, který **každých 1,2 sekundy přepisuje posuvné okno, zatímco děda ještě mluví** (i během zamyšlené pauzy uprostřed věty).
- **Dopad:** Když děda větu dořekne, kodér Whisperu už má 90 % zvuku zpracovaných. Dokončení posledního slova trvá **pod 120 ms** místo 850+ ms.

### 1.2 Sémantické rozpoznání konce promluvy (EoT) místo pevného limitu ticha
- **Problém:** Pevný limit ticha `2,2 s` je skvělý, když se děda odmlčí uprostřed věty (*„Zmizel mi… počkej…“*), ale působí líně, když řekne jediné jednoznačné slovo jako *„Ano“*, *„Ne“* nebo *„Stop“*.
- **Řešení – třístupňový dynamický dozvuk ticha:**
  1. **Stupeň okamžitého příkazu (dozvuk `450 ms`):** Když průběžný přepis při `expects_answer=True` odpovídá úplné jednoslovné odpovědi (`ano`, `ne`, `stop`, `hotovo`), čekání na ticho se zkrátí z `1600 ms` na `450 ms`.
  2. **Stupeň úplné věty (dozvuk `1100 ms`):** Když průběžný přepis už odpovídá úplnému záměru s vysokou jistotou (`Zmizel mi WhatsApp`, `Nic neslyším`, `Je to moc malé`) a melodie na konci fráze klesá, ukončí se po `1,1 s`.
  3. **Stupeň zaváhání / otevřené věty (dozvuk `2800 ms`):** Když přepis končí spojkou, předložkou nebo vycpávkou (*„a“*, *„že“*, *„na“*, *„ehm“*, *„počkej“*), čekání se automaticky prodlouží na `2,8 s`, aby děda nikdy nebyl pod tlakem.

### 1.3 Trvalý výstupní zvukový stream v paměti a TTS po větách
- **Problém:** Spouštění `aplay` nebo otevírání a zavírání streamu PortAudio pro každé zazvonění a větu přidává `60–100 ms` režie systému a na zvukových kartách USB způsobuje jemná „cvaknutí“ od stejnosměrné složky.
- **Řešení:**
  - Držet na zvukové kartě USB **jediný trvale otevřený výstupní stream 22,05 kHz, 16 bitů, mono**, plněný kruhovým bufferem bez zámků (v klidu se do něj zapisují nuly).
  - Držet relaci ONNX `PiperVoice` připnutou v RAM (`mlockall`) a posílat surové rámce PCM přímo do kruhového bufferu **větu po větě**, jakmile se vygeneruje první část věty (`čas do prvního zvuku < 65 ms`).

| Fáze zpracování | Současná implementace | Vylepšená architektura | Ušetřený čas |
|---|---|---|---|
| **Rozpoznání konce řeči** | pevně `2,2 s` (`1,6 s` u doplňující odpovědi) | sémantické EoT (`0,45 s` pro *Ano/Ne*, `1,1 s` úplná věta, `2,8 s` zaváhání) | **~1 100 ms** |
| **Inference Whisper STT** | dávková inference po skončení řeči (`~850 ms`) | průběžná inference po malých dávkách během řeči (dozvuk `~120 ms`) | **~730 ms** |
| **Generování TTS** | syntéza celé zprávy do souboru WAV (`~250 ms`) | streamování surového PCM po větách v RAM (`~60 ms` do prvního slova) | **~190 ms** |
| **Start zvukové karty USB** | otevření streamu / procesu pro každou promluvu (`~80 ms`) | trvalý zahřátý kruhový buffer ALSA/PortAudio (`<5 ms`) | **~75 ms** |
| **Celkové vnímané zpoždění** | **~3,3–3,7 sekundy** | **~0,35–0,9 sekundy** | **o 2,1+ sekundy rychlejší** |

---

## 2. Akustická vylepšení a DSP pro skutečné domácnosti seniorů

### 2.1 Plně duplexní potlačení ozvěny (přerušení hlasem bez stisku tlačítka)
- **Současné omezení:** Všesměrový USB mikrofon „chobotnice“ leží vedle USB reproduktoru, takže mikrofon musí být během řeči HelpButton ztlumený (poloduplex) a přerušit ji jde jen fyzickým stiskem tlačítka.
- **Vylepšení:** Zapojit na Raspberry Pi 5 `webrtc-audio-processing` (nebo `speexdsp`) a jako **referenční signál vzdálené strany** mu dávat přesně ten digitální stream PCM, který jde do zvukové karty USB.
- **Výsledek:** HelpButton v reálném čase odstraní ze vstupu mikrofonu 99 % vlastního hlasu. Když HelpButton začne dělat něco špatně a děda přes celý pokoj zavolá *„Stůj!“* nebo *„Stop!“*, **aniž by se dotkl tlačítka**, VAD zachytí jeho hlas přes ten její a okamžitě zastaví.

### 2.2 „Odolnost vůči televizi v obýváku“ (Silero VAD v5 + brána blízkého pole)
- **Problém:** Senioři mají často v pozadí puštěné české zprávy v televizi. Běžný VAD nebo Whisper zachytí mluvícího moderátora a přepíše ho jako součást dědova příkazu.
- **Vylepšení:**
  1. Nahradit WebRTC VAD modelem **Silero VAD v5 (ONNX)** (`<1 ms` na 30ms rámec na ARM CPU v Pi 5).
  2. Přidat **bránu blízkého pole podle crest faktoru a SNR**: člověk, který mluví 30–80 cm od mikrofonu „chobotnice“, má vysoký poměr přímé a odražené energie a ostré přechody souhlásek, na rozdíl od reproduktoru televize 3–4 metry daleko. Jakmile děda začne mluvit, zachytit jeho základní obálku RMS a odmítat hlasy v pozadí, které jsou o víc než 12 dB pod jeho počáteční špičkou.

### 2.3 Zvukový mastering „Senior Clarity“ (DSP kompenzující presbyakuzi)
- **Problém:** Věkem podmíněná nedoslýchavost (*presbyakuze*) snižuje citlivost mezi **2 kHz a 6 kHz** – přesně v pásmu, kde české neznělé souhlásky (*s, š, c, č, t, k, p*) nesou význam slov. Malé USB reproduktory navíc zahlcují spodní středy (`150–300 Hz`).
- **Vylepšení:** Před odesláním do zvukové karty USB poslat všechno TTS a všechna zazvonění přes lehký třístupňový řetězec DSP:
  1. **Horní propust na `140 Hz` (12 dB/okt.):** odstraní dunivou rezonanci skříňky malých reproduktorů.
  2. **Zdůraznění prezence (`+4,5 dB` mezi `2,2 kHz – 5,0 kHz`):** české souhlásky budou ostré a snadno rozlišitelné.
  3. **Kompresor dynamiky (poměr `3:1`, dorovnání na `-14 LUFS`):** každá slabika bude stejně hlasitá a čistá, bez ořezávání.

---

## 3. Kognitivní ergonomie a vylepšení UX hardwaru

### 3.1 Přizpůsobivé tempo řeči a automatické „zopakuj pomaleji“ (obsluha „Cože?“)
- Když senior řekne *„Cože?“*, *„Prosím?“*, *„Nerozuměl jsem“* nebo *„Mluv pomaleji“*, klient na Raspberry Pi to zachytí **lokálně za 0 ms** (bez cesty na server) a okamžitě přehraje poslední řečenou větu **rychlostí `0,70×`**, **o `+3 dB` hlasitěji** a zjednodušeně.

### 3.2 Uklidňující zvukový „tep“ během dlouhých úloh GUI agenta
- **Problém:** Když vizuálně-jazykový GUI agent (`helpbutton/server/agent.py`) 10–20 sekund pořizuje snímky obrazovky a proklikává se nabídkami Androidu, ticho seniorům napovídá, že se krabička rozbila.
- **Vylepšení:** Když je klient ve stavu `ClientState.PROCESSING` déle než 2,5 sekundy, přehrávat velmi tiché, teplé dřevěné ťuknutí (jako klidné hodinky nebo marimba na 15 % hlasitosti každé 2,5 sekundy) pod hlasovým komentářem z `/ws/ui` (*„Dívám se na obrazovku…“*). Senior hned ví, že HelpButton pracuje.

### 3.3 Kroužek NeoPixel WS2812B / dýchající LED s PWM
- Nahradit jedinou LED na GPIO 27 **kroužkem 12 LED WS2812B** (nebo hardwarově řízenou dýchající LED s PWM) uvnitř velkého podsvíceného 60mm arkádového tlačítka:
  - **Noční světlo v klidu:** teplé jantarové světlo na 5 %, aby děda tlačítko na stole snadno našel i v šeru.
  - **Ukazatel hlasitosti při poslechu:** když děda mluví, jas modré LED nebo oblouk kroužku se v reálném čase mění podle úrovně RMS mikrofonu – okamžitý vizuální důkaz, že mluví dost nahlas.
  - **Potvrzovací puls:** jemný zeleno-modrý tep při čekání na *„Ano / Ne“*.

---

## 4. Spolehlivost na úrovni spotřebiče a samoopravná architektura

### 4.1 Záložní režim „HelpButton v krabičce“ na okraji sítě (díky 8 GB RAM v Pi 5)
- **Postřeh:** Raspberry Pi 5 má **8 GB RAM** a rychlý čtyřjádrový procesor Cortex-A76, zatímco `faster-whisper (small, int8)` + `piper-tts` zaberou jen ~650 MB RAM.
- **Vylepšení:**
  - Spustit přímo na Raspberry Pi 5 odlehčenou kopii `helpbutton.server` (s `adb_watchdog` a `helpbutton.agents.android`) jako záložní nebo hlavní server.
  - Když je hlavní počítač se serverem vypnutý, Raspberry Pi 5 dokáže **samo opravit dědův telefon s Androidem přes bezdrátové ADB** (obnovit WhatsApp, opravit hlasitost a režim Nerušit, zvětšit písmo, zkontrolovat baterii, zablokovat podvody a upozornit rodinu přes webhook) úplně bez pomoci.

### 4.2 Samooprava po odpojení USB a automatická obnova ALSA
- **Problém:** Když kočka nebo vnouče zatáhne za USB kabel mikrofonu „chobotnice“ nebo zvukové karty, Linux ALSA zneplatní popisovač souboru (`-EPIPE` / `-ENODEV`) a běžné zvukové skripty v Pythonu natrvalo spadnou.
- **Vylepšení:** Obalit `MicrophoneStream` a `AudioPlayer` dohledem nad připojením za chodu, který zachytí `PortAudioError` / `BrokenPipeError`, každou 1 sekundu znovu volá `list_audio_devices()`, dokud se zařízení USB neobjeví, přehraje jemné zazvonění na znamení obnovy a pokračuje bez ztráty stavu.

### 4.3 Kořenový souborový systém jen pro čtení (`overlayfs`) + hardwarový watchdog
- **Problém:** Senioři vytahují spotřebiče rovnou ze zásuvky bez `sudo shutdown -h now`, což časem poškodí karty MicroSD se systémem ext4.
- **Vylepšení:**
  - Připojit kořenový souborový systém Raspberry Pi OS **jen pro čtení (`overlayfs`)** a `.tts_cache/` držet v `/dev/shm` (RAM disk) nebo na vyhrazeném odolném oddílu `/data`.
  - Zapnout hardwarový watchdog BCM2712 (`/dev/watchdog`, časový limit 10 s), aby se Pi 5 samo zotavilo z jakéhokoli zaseknutí jádra nebo poklesu napájení.

---

## 5. Plán implementace podle priorit

| Priorita | Vylepšení | Pracnost | Dopad |
|---|---|---|---|
| **P0 (rychlá výhra)** | **Třístupňový sémantický dozvuk ticha** (`0,45 s` pro *Ano/Ne*, `1,1 s` úplná věta, `2,8 s` zaváhání) + lokální okamžité pomalé zopakování *„Cože? / Zopakuj“* | ~2 hodiny | Zkrátí odezvu potvrzení o **víc než 1,1 sekundy** a přidá okamžité zopakování pomaleji |
| **P1 (velký dopad)** | **Ekvalizér a kompresor „Senior Clarity“ proti presbyakuzi** (high-shelf `+4,5 dB` + horní propust `140 Hz` v čistém Pythonu) + **tep během práce** | ~3 hodiny | Výrazně srozumitelnější čeština na levných USB zvukových kartách a žádné úzkostné ticho |
| **P2 (skok v rychlosti)** | **Trvalý kruhový buffer výstupu + TTS streamované po větách** a **průběžný přepis Whisperem po malých dávkách** | ~1 den | Celková odezva klesne **pod 350 ms** (jako rozhovor se skutečným člověkem) |
| **P3 (spotřebič)** | **Plně duplexní WebRTC AEC** (přerušení hlasem bez rukou) + **Silero VAD v5** (odolnost vůči televizi) + **samostatný záložní server na Pi 5** | ~2 dny | Z Raspberry Pi 5 se stane samostatný domácí spotřebič komerční úrovně |
