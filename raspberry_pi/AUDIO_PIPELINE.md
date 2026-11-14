# Zpracování zvuku (software firmwaru)

Tento dokument popisuje, co **software** firmwaru dělá se zvukem od mikrofonu po reproduktor. Hardware (čipy, mixéry, HAT, piny, zesilovače) se tu nepopisuje.

Zdrojové soubory:

- [audio_io.py](audio_io.py): nahrávání, převzorkování, detekce řeči, čištění, filtr srozumitelnosti a hlasitosti, přehrávání, tóny
- [client.py](client.py): smyčka rozhovoru, řízení nahrávání, přepis na pozadí, volání serveru, zpracování odpovědi
- [stt.py](stt.py): Whisper, filtry přepisu, překlad, fonetické opravy
- [tts.py](tts.py): dělení na věty, hlasové syntezátory, mezipaměť na disku

Příklad, který se táhne celým dokumentem: senior řekne **„Napiš Denisovi“**.

Rychlé cesty používají **numpy** a **scipy** (instaluje je `setup_rpi5.sh`). Bez nich běží stejné algoritmy v čistém Pythonu, jen pomaleji. Test ověřuje, že obě cesty dávají stejný výsledek. Funkce volané pro každý rámec (úroveň, počet průchodů nulou, kontrola cvaknutí, stereo → mono, převzorkování) jsou s numpy 8–29× rychlejší, takže zbude víc výkonu procesoru pro Whisper.

---

## 0. Přehled

```
nahrávání (formát zařízení)
  -> mono -> 16 kHz (antialiasingový filtr) -> rámce po 30 ms
  -> detekce řeči v každém rámci (VAD)
  -> při první pauze 0,6 s: na pozadí se spustí Whisper v plné kvalitě
  -> nahrávání skončí (pravidla dozvuku; kratší, když věta vypadá dokončeně)
  -> šetrné čištění (cvaknutí, pásmová propust, jedno zesílení, měkká brána)
  -> text = výsledek z pozadí (bez druhého průchodu), jinak nový průchod Whisperem
  -> server (text, nebo oříznuté surové PCM)
  -> odpověď
  -> převod textu na řeč (surový hlas z mezipaměti, 22 050 Hz)
  -> filtr srozumitelnosti + hlasitost (kompresor + limiter s předstihem)
  -> přehrání (aplay, záložně sounddevice)
```

---

## 1. Formáty vzorků

| Místo | Formát | Velikost |
|---|---|---|
| Nahrávání | přednostně 16 kHz mono; záložně 48 kHz mono nebo stereo; int16 | 30 ms = 960 B při 16 kHz; 5 760 B při 48 kHz stereo |
| Zpracování (VAD, čištění, Whisper, server) | 16 kHz mono, int16 | rámec 30 ms = 480 vzorků = 960 B |
| Vstup Whisperu | pole float32, −1…1 (předává se přímo, bez dekódování) | — |
| Hlas TTS | vzorkovací frekvence hlasového modelu (22 050 Hz u středního českého hlasu), mono, int16 | podle délky |
| Tóny | 22 050 Hz mono, int16, generované v kódu | — |
| Přehrávání | formát, který výstup přijme; záložně 48 kHz stereo | blok 40 ms = 7 680 B |

---

## 2. Nahrávání (`MicrophoneStream`, `audio_io.py`)

1. **Výběr vstupního zařízení** (`select_best_device`): nastavený název nebo číslo, pak PipeWire, pak přímé zařízení ALSA, pak zařízení USB, pak jakékoli zařízení mimo HDMI.
2. **Dohodnutí formátu** (`_stream_format_candidates`). Zkouší se v tomto pořadí a použije se první přijatý:
   1. 16 kHz mono
   2. 48 kHz mono
   3. 48 kHz stereo
3. **Spuštění callbacku.** Každý blok jde do fronty bezpečné pro vlákna (nejvýš 600 bloků, asi 18 s). Callback nikdy nečeká, takže se nahrávání nezasekne ani během běhu Whisperu.
4. **Čtení fronty** ve vlákně zpracování. Když do **2,5 s** nepřijde žádný blok, vyvolá se chyba a stream se otevře znovu. Když chybí `sounddevice`, použije se místo něj `arecord`.
5. **Převod na mono**, pokud je kanálů víc (`stereo_to_mono_pcm16`):
   - signál je v obou kanálech → `(L + R) // 2`
   - jeden kanál je mrtvý (pod 15 % druhého) → použije se jen živý kanál, bez ztráty −6 dB
   - kanály v opačné fázi (L ≈ −R) → `(L − R) // 2`, aby se navzájem nevyrušily
6. **Převzorkování** na 16 kHz, je-li potřeba:
   - 48 000 Hz (s numpy): `Fir3Decimator`, 63koeficientová dolní propust typu windowed-sinc na 7,2 kHz, po které se ponechá každý 3. vzorek. Stav si drží mezi bloky, takže na hranách bloků nic necvaká. (Dřívější průměr ze 3 vzorků nechal šum nad 8 kHz přeložit zpět do pásma řeči.)
   - Bez numpy nebo pro jiné poměry: `resample_pcm16_mono` (průměr ze 3 vzorků nebo lineární interpolace).
7. **Rozdělení na rámce** po 480 vzorcích (960 bajtů, 30 ms), které jdou do detekce řeči.

---

## 3. Detekce řeči rámec po rámci (`process_vad_stream`, `VadDetector`)

Běží v pracovním vlákně. Každý rámec projde těmito kroky v tomto pořadí:

| Krok | Co kód dělá | Hodnoty |
|---|---|---|
| a. Kontrola reproduktoru | Rámec se zahodí, když přehrávač právě hraje | — |
| b. Úroveň šumu | První 4 rámce (pokud se nedrží tlačítko) a všechny tiché rámce aktualizují úroveň šumu | začíná na 104 |
| c. Úroveň | RMS (hlasitost) rámce se porovná s prahem | práh = max(160, úroveň šumu × 2,4) |
| d. Pravidlo blízkého hlasu | Když se nedávno objevil hlasitý blízký hlas, práh se zvedne (filtr proti televizi) | platí, když nedávná špička > 2,2 × základ; práh = 22 % té špičky |
| e. Kontrola cvaknutí | Krátké, velmi hlasité špičky se odmítnou (`is_transient_pop_frame`) | špička ≥ 2 400, crest faktor ≥ 5,5, ≤ 5,5 % vzorků nad 35 % špičky |
| f. Rozhodnutí o řeči | WebRTC VAD (agresivita 2). Velmi hlasitý rámec se počítá jako řeč, i když VAD řekne ne | RMS ≥ 1,45 × práh vynutí řeč |
| | Bez WebRTC: počet průchodů nulou mezi 0,01 a 0,55, nebo RMS ≥ 1,6 × práh | |
| g. Předstih (pre-roll) | Posledních 17 rámců (500 ms) se drží v kruhovém bufferu. Když začne řeč, přidají se do nahrávky | |
| h. Pravidlo začátku | Řeč se počítá až po nejméně 8 rámcích (0,25 s). Kratší záblesk se po 1 s ticha zapomene. 8 s bez řeči = vypršení času | |

Pozice **posledního rámce s řečí** se pamatuje kvůli ořezání (oddíl 5).

**Držení tlačítka (hold-to-talk):** dokud je tlačítko držené, čas nevyprší a ticho se nepočítá.

---

## 4. Kdy nahrávání skončí

| Pravidlo | Podmínka | Dozvuk po poslední řeči |
|---|---|---|
| Běžné | výchozí | 2,2 s (≈ 73 rámců) |
| Doplňující odpověď | HelpButton položila otázku | 1,6 s (≈ 53 rámců) |
| Krátká odpověď | doplňující odpověď a řeč trvala ≤ 1,05 s | 0,65 s (≈ 22 rámců) |
| Dokončená věta | přepis z pozadí končí `.` `!` `?` | 1,3 s (≈ 43 rámců), nebo jakmile dorazí přepis, pokud později |
| Zaváhání | přepis z pozadí končí spojkou („a“, „že“…), `…` nebo `,` | 2,8 s (≈ 93 rámců) |
| Úplný příkaz | známý krátký příkaz nebo odpověď („Ano“, „Zmizel mi WhatsApp“, „Cože?“) | skončí, jakmile dorazí přepis |
| Nový stisk tlačítka | během poslechu | skončí okamžitě |
| Maximum | vždy | 25 s |

### Přepis na pozadí během pauzy

1. Po **0,6 s** ticha (`rolling_stt_pause_s`) se dosud nahraná řeč (oříznutá na poslední rámec s řečí + 0,3 s) vyčistí (oddíl 5).
2. V event loopu se spustí přepis Whisperem v **plné kvalitě** (oddíl 6). Nahrávání na něj **nečeká**.
3. Každý další tichý rámec se ptá, jestli je přepis hotový (`poll_pause_check`). Když je, `classify_utterance_completeness` zařadí text jako **complete**, **likely_complete**, **hesitation** nebo **continue** a dozvuk se upraví podle tabulky výše.
4. Když senior znovu promluví, úloha se zruší (`on_pause_cancel`). Úloha, která ještě nezačala, se úplně přeskočí. Nová úloha začne při další pauze.
5. Když nahrávání skončí a úloha ještě běží, počká se na její výsledek místo spuštění nového přepisu.

---

## 5. Čištění nahrávky (`apply_mic_speech_cleanup_pcm16`)

Cílem je čistý a dost hlasitý signál **bez artefaktů**. Whisper si s šumem v pozadí poradí mnohem líp než s usekanou nebo zkreslenou řečí, takže každý krok je šetrný a plynulý.

1. **Potlačení cvaknutí** (`suppress_transients_pcm16`): okna po 4 ms (64 vzorků). Okno se bere jako mechanický nebo elektrický ťuk jen tehdy, když má špičku aspoň **8 000** a je **7,5× hlasitější než celé široké okolí ±24 ms** (±6 oken). Ztlumí se na úroveň okolí, s plynulými náběhy na obou stranách. Explozivy („p“, „t“, „k“, „č“) na začátku i konci slov (i přes mezery přídechu 15–25 ms) a přirozené zvuky úst zůstanou na 100 % nedotčené.
2. **Horní propust**:
   - dvě za sebou zapojené horní propusti prvního řádu na **75 Hz** (brum, dunění, stejnosměrná složka) v plné přesnosti s plovoucí čárkou
   - při 16 kHz se nepoužívá dolní propust prvního řádu (vyhne se mírnému poklesu od 1,5 kHz, který dřív otupoval sykavky „s“, „š“, „c“, „č“)
3. **Automatické řízení zesílení (AGC) odolné vůči lupnutím**:
   - Bloky řeči (20 ms) jsou ty nad `max(120, úroveň šumu × 1,75)`.
   - Referencí je jejich **75. percentil** RMS a celkové zesílení omezuje **85. percentil** trvalé špičky řeči (`sustained_peak <= 24 000`), ne jediný odlehlý vzorek.
   - Strop s předstihem 3 rámců plynule stáhne křivku bloku kolem každého osamoceného lupnutí úst u mikrofonu, takže žádný vzorek nepřekročí **24 000**, a zbytek tiché promluvy přitom dostane plné zesílení AGC.
4. **Měkká šumová brána** mezi slovy: bloky tišší než `max(60, úroveň šumu × 1,35)` se jemně ztlumí, nejvýš na **0,72** (≈ −2,8 dB), takže zůstanou nádechy, neznělé frikativy i tiché konce slov.

Kontroluje se i přebuzení samotného mikrofonu. Když je víc než 0,2 % vzorků na limitu, zapíše se do logu varování. Nad 1,5 % firmware jednou vypne zesílení předzesilovače mikrofonu (−20 dB, `reduce_mic_gain`); krok zesílení výše pak úroveň dorovná digitálně, bez zkreslení.

**Ořezání:** `stt_pcm_bytes` / `stt_wav_bytes` obsahují jen řeč plus **0,45 s** (`speech_trim_tail_ms`) a `last_speech_len` sleduje i tiché souhlásky a zvuky úst hned po samohláskách.

---

## 6. Převod řeči na text (`SpeechToText`, `stt.py`)

### Nastavení každého běhu Whisperu

1. Model `small`, int8, 4 vlákna procesoru. Vždy jen jeden běh najednou (zámek), s neblokujícími spekulativními kontrolami pauzy (`fast_speculative=True`), které se okamžitě přeskočí, když je zámek obsazený.
2. **Zvuk se předává jako pole float32 s oříznutým tichem**, takže se nic nedekóduje z WAV přes FFmpeg.
3. **Jazyk je pevně čeština** (`language = "cs"`, výchozí). Automatická detekce často označila krátké české věty za slovenštinu nebo polštinu, což dávalo špatný text a pomalý online překlad.
4. **Jednoprůchodové hladové dekódování** (`beam_size = 1`, `best_of = 1`, `temperature = 0.0`, `condition_on_previous_text = False`), které je na CPU asi 3× rychlejší (~0,4–1,0 s) a vyhne se zdržením při záložních teplotách.
5. `without_timestamps = True`: méně tokenů k dekódování, takže je to rychlejší.
6. **Krátký přirozený prompt** („Senior mluví česky s asistentkou HelpButton…“). Dlouhý seznam frází způsoboval, že Whisper u nejasného zvuku prompt opisoval.
7. **Hotwords**: názvy aplikací (WhatsApp, SMS, Seznam, AnyDesk…), seznam `vocabulary` z konfigurace (třeba „Denis“) a jména rodiny, která pošle server („Lenka“, „Lence“).
8. **Vnitřní VAD Whisperu je vypnutý** (`whisper_vad_filter = false`). Zvuk už je oříznutý naším VAD a vnitřní VAD by mohl tichý hlas seniora zahodit.
9. **Úplné zahřátí při startu**: při spuštění aplikace (na Pi souběžně s hledáním serveru, na serveru v `lifespan` FastAPI) proběhne zkušební běh 0,5 s s plným `initial_prompt` a `hotwords`. Zahřeje všechna jádra kodéru a dekodéru CTranslate2, takže i úplně první hlasová interakce trvá pod sekundu.

### Jazykový režim `auto` (volitelný, pro anglické demo)

- Whisper rozpozná jazyk. Když je to čeština, nebo angličtina s pravděpodobností ≥ 0,7, ponechá se.
- Cokoli jiného (třeba „pl“ u české věty) se přepíše znovu jako čeština, ještě než proběhne jakékoli dekódování.

### Filtry a opravy

1. **Filtr halucinací** (`is_whisper_hallucination`): prázdný text, méně než 2 písmena, známé fráze z titulků nebo závěrečných titulků, osamocená webová adresa, šest a víc stejných slov, nebo **6 a víc slov opsaných z promptu**.
2. **Nový pokus místo „nerozuměla jsem“:** když je výsledek prázdný nebo halucinace a zvuk má aspoň 0,35 s, proběhne ještě jeden běh bez promptu, bez jakéhokoli VAD a s vypnutými prahy „žádná řeč“.
3. **Překlad** (`translate_to_czech`), jen když jazyk není čeština:
   1. slovník přesných frází
   2. anglické vzory
   3. online služba (časový limit 2 s, výsledky v mezipaměti, po selhání se na 5 minut přeskočí)

   Český text se online **nikdy** neposílá. Dřív kvůli slovu „WhatsApp“ nebo českému „my“ vypadala česká věta jako anglická.
4. **Fonetická oprava** (`normalize_senior_speech`): známá přeslechnutí („votsap“ → WhatsApp, „esemeska“ → SMS…) a blízké shody s WhatsApp, AnyDesk, TeamViewer, Seznam.

### Který text se použije (`_transcribe_utterance`)

1. Výsledek z pozadí, pokud dorazil během dozvuku.
2. Úloha na pozadí, pokud ještě běží a senior už nic neřekl. Počká se na ni, neopakuje se.
3. Hotový výsledek z pozadí, i prázdný: druhý běh na stejném zvuku by jen přidal čas.
4. Jinak nový přepis oříznuté nahrávky.

### Přepis na serveru a prázdný text

- Přepis na serveru (lokální Whisper vypnutý nebo nenačtený): na server se pošle **oříznuté** vyčištěné PCM.
- Prázdný přepis, ale senior opravdu mluvil (≥ 0,3 s řeči) a server hlásí, že umí přepis (`/api/state` → `voice.stt`): oříznutý WAV jde na `POST /api/audio?rate=16000&source=button` jako **druhý názor**. Server často běží na rychlejším počítači s větším modelem. Jeho odpověď se použije jako běžná odpověď z `/api/utterance`.
- Jinak firmware odpoví lokálně („Omlouvám se, nerozuměla jsem…“) bez volání serveru.

---

## 7. Komunikace se serverem (`client.py`)

| Případ | Požadavek |
|---|---|
| Lokální text | `POST /api/utterance` s JSON `{"text": ..., "source": "button"}` |
| Surový zvuk | `POST /api/button/audio?rate=16000`, tělo = oříznuté surové PCM int16 mono, `application/octet-stream` |
| Druhý názor (lokální text prázdný) | `POST /api/audio?rate=16000&source=button`, tělo = oříznutý WAV, `audio/wav` |

Před prvním požadavkem na adresu serveru firmware ověří, že server zná párovací token (`GET /api/hello?nonce=…`, server odpoví HMAC z nonce). Teprve pak pošle API klíč, text nebo zvuk. Server, který token neprokáže, nedostane nic.

Odpovědi na surový zvuk mají volitelné tělo WAV a tyto hlavičky: `X-HelpButton-Led`, `X-HelpButton-Tone`, `X-HelpButton-Screen`, `X-HelpButton-Expects`, `X-HelpButton-Say` (URL-kódované).

- **Lokální opakování („Cože?“)**: fráze žádosti o zopakování (nejvýš 5 slov) přehraje poslední odpověď lokálně rychlostí 0,68 a hlasitěji. Na server se nic neposílá.
- **Signál práce**: tichý `working_tick` každých 2,5 s během čekání, jen když nic jiného nehraje.

---

## 8. Zpracování odpovědi (`run_interaction`, `speak_text`)

1. LED se nastaví podle `led` a tón podle `tone` (`double`, `low` nebo `beep`).
2. `say` se přečte větu po větě.
3. Když je `expects_answer` pravda: zazní tón `listen_followup`, pak pauza 0,2 s a mikrofon se znovu otevře (nejvýš 2×).
4. U odpovědí na surový zvuk se WAV přehraje přímo a `say` a `tone` se vymažou, aby se neopakovaly.

---

## 9. Převod textu na řeč (`TextToSpeech`, `tts.py`)

1. **Rozdělení na věty** u `. ! ?`. Kousky kratší než 22 znaků se spojí s dalším a poslední kousek pod 14 znaků s předchozím.
2. **Souběžná syntéza**: všechny věty začnou najednou. Volání Piperu řadí zámek a přehrávání drží původní pořadí.
3. **Řetězec syntezátorů**: Piper → Edge-TTS → server `/api/tts` → espeak-ng. Syntezátor, který selže, se na 60 s přeskočí.
4. **Rychlost**: Piper `length_scale` = 1 / rychlost (0,85 → 1,18, opakování 0,68 → 1,47); Edge-TTS rychlost −15 % / −32 %; espeak-ng slov za minutu = max(85, 155 × rychlost).
5. **Piper** se volá přes Python API (piper-tts 1.3 `synthesize_wav`, nebo 1.2 `synthesize`) a model zůstává v RAM. Program z příkazové řádky je jen záloha.
6. **espeak-ng** dostane hlavičku WAV přepsanou se skutečnou délkou.
7. **Mezipaměť na disku** (`.tts_cache`): klíčem je SHA-256 ze syntezátoru a hlasu, jazyka, rychlosti a textu. Ukládá surový hlas, ještě před zpracováním srozumitelnosti a hlasitosti.

---

## 10. Filtr srozumitelnosti a hlasitost (`apply_senior_clarity_dsp_pcm16`)

Používá se pro mluvené odpovědi, WAV ze serveru a pozdrav. Nepoužívá se pro tóny.

1. Stereo se převede na mono.
2. **Ekvalizér** (`senior_clarity_dsp`):
   - horní propust prvního řádu na **140 Hz** (malý reproduktor nebude dunět)
   - **zesílení výšek (high-shelf)**: pásmo nad asi **2 200 Hz** × **1,55** (+3,8 dB) pro „s“, „š“, „č“, „t“, „k“
3. **Hlasitost** (`output_loudness`, ve výchozím stavu zapnutá):
   1. Normalizace špičky na **31 000** (≈ −0,5 dBFS).
   2. **Kompresor**: bloky 5 ms, náběh 5 ms, doběh 60 ms, práh −24 dBFS, poměr 3:1. Tiché slabiky se přiblíží hlasitým.
   3. Znovu normalizace špičky na 31 000.
   4. **Zesílení +9 dB do limiteru s předstihem**: v každém 1ms bloku zesílení klesne o jeden blok *před* špičkou a vrací se během 60 ms. Zesílení se mezi středy bloků interpoluje, takže žádný vzorek nepřekročí strop a průběh vlny se nikdy nedeformuje.
   5. Žádosti o zopakování (`extra_gain` 1,3) zesilují víc: +2,3 dB a nižší práh kompresoru.

   U zvuku podobného řeči je to asi o **+7 dB** hlasitější než výstup Piperu, bez jediného ořezaného vzorku (naměřené zkreslení tónu 120 Hz pod 0,2 %, u 220 Hz 0,01 %).
4. **Bez zpracování hlasitosti**: použije se `extra_gain` a když by špička přesáhla 31 000, celá věta se lineárně ztlumí. Dřívější okamžitý měkký limiter nad 22 000 deformoval průběh vlny, a to bylo to zkreslení.

---

## 11. Přehrávání (`AudioPlayer`, `audio_io.py`)

- Přehrávání je **sériové**: vždy jen jeden zvuk. `stop()` zruší i zvuky ve frontě.
- **`aplay`** (Linux, přednostně): WAV se zapíše do `/dev/shm` a přehraje. Každých 20 ms se kontroluje, jestli nepřišel pokyn k zastavení.
- **`sounddevice`** (když `aplay` selže; `aplay` se pak na 30 s přeskočí):
  - stejní kandidáti formátu zařízení jako u nahrávání
  - převzorkování přes `resample_pcm16_hq`: pásmově omezený převod celé věty založený na FFT (bez numpy lineární interpolace)
  - mono se podle potřeby zkopíruje do sterea
  - zápis po blocích 40 ms
- **Ostatní**: `winsound` na Windows, `ffplay` jako poslední možnost.

Chyby se ukládají do `last_error` a zapisují do logu.

---

## 12. Ozvěna (jen softwarově)

- **Potlačení ozvěny není.** Během přehrávání se rámce z mikrofonu zahazují.
- Po pozdravu a před doplňující otázkou je pauza **0,2 s**, než se mikrofon znovu otevře.
- Stisk tlačítka během přehrávání zvuk okamžitě zastaví.

---

## 13. Tóny (`synthesize_tone_wav`)

Generují se při 22 050 Hz, int16, jako sinusovky s náběhem a doběhem 12 ms (raised cosine). Amplituda **16 000** (dřív 9 500); signál práce **4 400** (dřív 2 600).

| Název | Noty | Použití |
|---|---|---|
| `beep` | 587,33 Hz na 80 ms, pak 880 Hz na 100 ms | začátek poslechu |
| `listen_followup` | 783,99 Hz na 90 ms | HelpButton čeká na odpověď |
| `double` | 659,25 Hz na 80 ms, ticho 40 ms, 880 Hz na 110 ms | zastavení nebo hotovo |
| `low` | 329,63 Hz na 160 ms, pak 261,63 Hz na 220 ms | chyba nebo nic neslyšela |
| `working_tick` | 392 Hz na 35 ms, ticho 20 ms, 523,25 Hz na 45 ms | pořád pracuje |

---

## 14. Průchod krok za krokem: „Napiš Denisovi“

Časová osa. Řeč dlouhá 1,5 s je příklad; čas Whisperu závisí na Pi.

| Čas | Co kód dělá |
|---|---|
| 0,00–0,12 s | První 4 rámce nastaví počáteční úroveň šumu |
| 0,30 s | Začne řeč, přidá se předstih (pre-roll) |
| 0,30–1,80 s | Rámce s řečí se ukládají v surové podobě |
| 1,80 s | Začne ticho |
| 2,40 s | 0,6 s ticha: řeč (oříznutá na 2,10 s) se vyčistí a na pozadí se spustí Whisper s češtinou, hladovým dekódováním a hotwords („Denis“, pokud je ve `vocabulary`). Nahrávání pokračuje |
| ≈ 2,4 s + čas Whisperu | Výsledek „Napiš Denisovi.“ končí tečkou → pravděpodobně dokončená věta → dozvuk se zkrátí na 1,3 s. Nahrávání skončí ve 3,10 s, nebo jakmile dorazí výsledek, pokud později |
| (když je Whisper pomalejší než 2,2 s) | Nahrávání skončí ve 4,00 s a na běžící úlohu se počká, neopakuje se |
| Konec | Čištění oříznuté nahrávky (s numpy v milisekundách). `POST /api/utterance` s `"Napiš Denisovi."` |
| Později | Příklad odpovědi „Co mu mám napsat?“ → TTS → srozumitelnost + hlasitost → přehrání → `listen_followup` → mikrofon se znovu otevře |

---

## 15. Další příklady

| Řečeno | Cesta kódem |
|---|---|
| „Ano“ (doplňující odpověď) | pravidlo krátké odpovědi: konec 0,65 s po řeči |
| „Ano“ (bez předchozí otázky) | známá úplná odpověď: konec, jakmile dorazí přepis z pozadí |
| „Cože?“ | žádost o zopakování: poslední odpověď se přehraje rychlostí 0,68 a hlasitěji. **Bez volání serveru** |
| „Napiš na votsap“ | fonetická oprava → „Napiš na WhatsApp“. Nikdy se neposílá do online překladu |
| „Zmizel mi a…“ a pak pauza | zaváhání: dozvuk se prodlouží na 2,8 s |
| Tichý hlas seniora, který Whisper napoprvé nezachytí | nový pokus bez promptu a prahů místo „nerozuměla jsem“ |

---

## 16. Známá omezení softwaru

1. **Bez potlačení ozvěny.** Senior nemůže HelpButton přerušit hlasem, jen tlačítkem.
2. **Pásmové filtry jsou prvního řádu**, takže mají pozvolné strmosti.
3. **Whisper vždy zpracuje 30s okno**, takže krátká věta trvá skoro stejně dlouho jako dlouhá. Na Pi je `small` kompromis mezi přesností a rychlostí; `base` je rychlejší, ale v češtině méně přesný.
4. **Úlohu na pozadí nejde zastavit uprostřed běhu.** Když se senior na 0,6 s odmlčí a pak pokračuje, úloha doběhne na pozadí a další úloha na ni počká.
5. **Výchozí jazyk je čeština.** Anglicky mluvící uživatel potřebuje `language = "auto"` (nebo `--bilingual` / `--language auto`).
6. **Hlasitost je relativní k plnému rozsahu.** Výsledná hlasitost závisí i na mixéru (`output_volume_pct`; 100 = nejhlasitější nastavení bez ořezávání).
7. Všechna čísla pocházejí z kódu. **Časy Whisperu na zařízení změřené nejsou**; vypíše je `--test-audio`.

---

## 17. Mapa funkcí

| Funkce | Soubor | Úloha |
|---|---|---|
| `select_best_device`, `_stream_format_candidates` | audio_io.py | výběr zařízení a formátu |
| `MicrophoneStream._iter_sounddevice` | audio_io.py | fronta callbacku, kontrola zaseknutí, převod na mono, převzorkování, rámce |
| `Fir3Decimator` | audio_io.py | 48 → 16 kHz s antialiasingovým filtrem |
| `process_vad_stream`, `VadDetector.is_speech` | audio_io.py | smyčka rámců, rozhodnutí o řeči, dozvuky, ořezání, dotazování úlohy na pozadí |
| `suppress_transients_pcm16` | audio_io.py | potlačení osamocených ťuků |
| `apply_mic_speech_cleanup_pcm16` | audio_io.py | pásmová propust, jedno zesílení, měkká brána |
| `clipped_ratio` | audio_io.py | varování před přebuzením mikrofonu |
| `RpiVoiceButtonClient._record_voice` | client.py | spouští přepis na pozadí a ptá se na něj |
| `RpiVoiceButtonClient._transcribe_utterance` | client.py | vybere výsledek z pozadí, nebo spustí nový průchod |
| `RpiVoiceButtonClient._server_second_opinion` | client.py | přepis na serveru, když Pi slyšelo řeč, ale nedostalo text |
| `SpeechToText.transcribe_wav` | stt.py | volání Whisperu, jazyk, hotwords, nový pokus, filtry |
| `is_whisper_hallucination`, `is_prompt_leak` | stt.py | zahazuje falešné přepisy |
| `classify_utterance_completeness` | stt.py | complete / likely_complete / hesitation / continue |
| `translate_to_czech`, `looks_english` | stt.py | překlad, jen pro nečeský text |
| `normalize_senior_speech` | stt.py | fonetické a přibližné opravy |
| `RpiVoiceButtonClient.speak_text` | client.py | rozdělení, syntéza, přehrání |
| `TextToSpeech.synthesize` | tts.py | řetězec syntezátorů a mezipaměť |
| `apply_senior_clarity_dsp_pcm16` | audio_io.py | ekvalizér, kompresor, limiter s předstihem |
| `resample_pcm16_hq` | audio_io.py | převzorkování pro přehrávání |
| `AudioPlayer.play_wav_bytes` | audio_io.py | řazení, srozumitelnost a hlasitost, přehrání |
| `synthesize_tone_wav` | audio_io.py | tóny |
