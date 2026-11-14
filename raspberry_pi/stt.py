"""Převod řeči na text (STT) pro Raspberry Pi 5 + sémantický konec věty, filtrování halucinací a překlad EN/SK -> CS.

Vlastnosti:
  * Běží lokálně na Raspberry Pi 5 přes `faster-whisper` (int8, 4 vlákna Cortex-A76),
  * Obsahuje slovníkovou nápovědu (`initial_prompt`) pro výrazy českých seniorů,
  * Filtruje typické titulkové halucinace Whisperu při tichu nebo šumu v místnosti,
  * Detekuje lokální žádost o pomalejší zopakování ("Cože?", "Prosím?", "Mluv pomaleji", "Zopakuj to"),
  * Klasifikuje sémantickou úplnost věty během pomlk (`classify_utterance_completeness`):
    okamžitě ukončí poslech u kompletního příkazu a naopak prodlouží čekání, pokud věta končí spojkou ("a...", "že..."),
  * Automaticky překládá anglické a slovenské hlasové příkazy do češtiny.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import io
import logging
import re
import threading
import time
import wave
from dataclasses import dataclass
from typing import Iterable

import httpx

from .config import RpiConfig

log = logging.getLogger("helpbutton.rpi.stt")

SENIOR_PROMPT_CS = (
    "Senior mluví česky s asistentkou HelpButton o telefonu a počítači. "
    "Zmizel mi WhatsApp. Napiš zprávu Lence. Zavolej Petrovi. Nic neslyším. "
    "Otevři Seznam. Volal mi někdo z banky kvůli AnyDesku."
)

BASE_VOCABULARY_CS = (
    "WhatsApp",
    "SMS",
    "Seznam",
    "AnyDesk",
    "TeamViewer",
    "Messenger",
    "Facebook",
    "YouTube",
    "e-mail",
    "Wi-Fi",
    "internet",
    "heslo",
    "PIN",
    "baterie",
    "hlasitost",
    "fotky",
    "zpráva",
)

FOLLOWUP_PROMPT_CS = (
    "Ano. Ne. Určitě. Zrušit. Prosím. Dobře. Jo. Nechci. Cože? Zopakuj to pomaleji. Vrať to zpátky."
)
FOLLOWUP_HOTWORDS_CS = ("ano", "ne", "stop", "cože", "hotovo")


def select_whisper_prompt(is_followup: bool = False, extra_prompt: str = "") -> str:
    """Vybere kontextově cílený `initial_prompt` pro Whisper podle stavu dialogu."""
    base = FOLLOWUP_PROMPT_CS if is_followup else SENIOR_PROMPT_CS
    return f"{base} {extra_prompt}".strip()


SENIOR_PHONETIC_REPLACEMENTS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"\b(vot\s*sap\w*|vat\s*sap\w*|vacap\w*|vocap\w*|watsap\w*|wotzap\w*|"
            r"whatsap\b|whatsappu|vojt[eě]ch\s*sap\w*)\b",
            re.IGNORECASE,
        ),
        "WhatsApp",
    ),
    (re.compile(r"\b(zmizel\s+mi)\s+vojt[eě]ch\b", re.IGNORECASE), r"\1 WhatsApp"),
    (
        re.compile(r"\b(es\s*-?\s*em\s*-?\s*es\w*|esemesk[aouy]|smsk[aouy])\b", re.IGNORECASE),
        "SMS",
    ),
    (
        re.compile(r"\b(any\s*desk\w*|eny\s*desk\w*|anidesk\w*|enydesk\w*)\b", re.IGNORECASE),
        "AnyDesk",
    ),
    (
        re.compile(r"\b(team\s*viewer\w*|t[yý]m\s*v[iíj][uů]v\w*|tym\s*vjuvr\w*)\b", re.IGNORECASE),
        "TeamViewer",
    ),
    (re.compile(r"\b(internety)\b", re.IGNORECASE), "internet"),
    (re.compile(r"\b(sezn[aá]mek)\b", re.IGNORECASE), "Seznam"),
]

CANONICAL_FUZZY_TERMS: dict[str, str] = {
    "whatsapp": "WhatsApp",
    "anydesk": "AnyDesk",
    "teamviewer": "TeamViewer",
    "seznam": "Seznam",
}


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            curr.append(min(curr[-1] + 1, prev[j] + 1, prev[j - 1] + cost))
        prev = curr
    return prev[-1]


def normalize_senior_speech(text: str) -> str:
    """Opraví fonetické komolení technických pojmů ("votsap" -> "WhatsApp",
    "esemeska" -> "SMS", "enydesk" -> "AnyDesk") a drobné překlepy Whisperu
    z levného mikrofonu pomocí Levenshteinovy vzdálenosti.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return ""

    for rx, repl in SENIOR_PHONETIC_REPLACEMENTS:
        cleaned = rx.sub(repl, cleaned)

    tokens = cleaned.split()
    out_tokens: list[str] = []
    for tok in tokens:
        m = re.match(r"^([^\w]*)(\w+)([^\w]*)$", tok, re.UNICODE)
        if not m:
            out_tokens.append(tok)
            continue
        prefix, word, suffix = m.group(1), m.group(2), m.group(3)
        low = word.lower()
        repaired = word
        if len(low) >= 6 and low not in CANONICAL_FUZZY_TERMS:
            for target_low, canonical in CANONICAL_FUZZY_TERMS.items():
                max_dist = 2 if len(target_low) >= 7 else 1
                if abs(len(low) - len(target_low)) <= max_dist and _levenshtein(low, target_low) <= max_dist:
                    repaired = canonical
                    break
        elif low in CANONICAL_FUZZY_TERMS:
            repaired = CANONICAL_FUZZY_TERMS[low]
        out_tokens.append(f"{prefix}{repaired}{suffix}")

    return " ".join(out_tokens)


HALLUCINATION_PATTERNS = (
    r"titulky\s*(vytvoril|vytvořil|přeložil|prelozil|napsal|uprava|úprava)",
    r"johnyx",
    r"d[eě]kuji\s+za\s+(pozornost|shl[eé]dnut[ií]|sledov[aá]n[ií])",
    r"odeb[ií]rejte",
    r"subtitles?\s+by",
    r"amara\.org",
    r"thank\s+you\s+for\s+watching",
    r"please\s+subscribe",
    r"^\W*www\.[a-z0-9-]+\.(cz|com|org|sk)\W*$",
    r"^[♪♫\s\.\,\!\?\-\–\—…]+$",
)
RX_HALLUCINATION = re.compile("|".join(HALLUCINATION_PATTERNS), re.IGNORECASE)

RX_REPEAT_SLOWER = re.compile(
    r"^(co[zž]e|pros[ií]m|nerozum[eě]l\w*|nerozum[ií]m|nesly[sš]el\w*|nesly[sš][ií]m\s+t[eě]|"
    r"zopakuj\w*|opakuj\w*|[rř]ekni\s+to\s+znovu|[rř]ekn[eě]te\s+to\s+znovu|"
    r"je[sš]t[eě]\s+jednou|mluv\w*\s+pomaleji|pomaleji\s+pros[ií]m|"
    r"what|pardon|repeat(\s+that|\s+please)?|say\s+that\s+again|speak\s+slower)\b",
    re.IGNORECASE,
)

HESITATION_ENDINGS = {
    "a",
    "i",
    "že",
    "ze",
    "protože",
    "protoze",
    "nebo",
    "ale",
    "když",
    "kdyz",
    "aby",
    "abych",
    "jak",
    "kde",
    "kdo",
    "co",
    "který",
    "ktery",
    "která",
    "ktera",
    "na",
    "do",
    "v",
    "ve",
    "z",
    "ze",
    "s",
    "se",
    "k",
    "ke",
    "o",
    "po",
    "pro",
    "při",
    "pri",
    "před",
    "pred",
    "za",
    "mezi",
    "mi",
    "ti",
    "si",
    "mu",
    "jí",
    "ji",
    "ho",
    "tě",
    "te",
    "nám",
    "nam",
    "vám",
    "vam",
    "bych",
    "bys",
    "by",
    "bychom",
    "byste",
    "jsem",
    "jsi",
    "jsme",
    "jste",
    "můj",
    "muj",
    "moje",
    "mého",
    "meho",
    "mému",
    "memu",
    "svůj",
    "svuj",
    "svoje",
    "napiš",
    "napis",
    "pošli",
    "posli",
    "zavolej",
    "otevři",
    "otevri",
    "zapni",
    "vypni",
    "řekni",
    "rekni",
    "ehm",
    "hmm",
    "počkej",
    "pockej",
    "počkejte",
    "pockejte",
    "moment",
    "vlastně",
    "vlastne",
    "and",
    "or",
    "that",
    "because",
    "wait",
    "uh",
    "um",
}

RX_COMPLETE_UTTERANCE = re.compile(
    r"^(ano|ano\s+pros[ií]m|jo|jojo|tak\s+jo|jasn[eě]|jist[eě]|ur[cč]it[eě]|dob[rř]e|dob[rř]e\s+d[eě]kuji|"
    r"souhlas[ií]m|m[uů][zž]e[sš]|ud[eě]lej\s+to|ne|ne\s+d[eě]kuji|nechci|ned[eě]lej|zru[sš]|"
    r"stop|st[uů]j|zastav|hotovo|vra[tť]\s+to\s+zp[aá]tky|"
    r"zmizel\s+mi\s+whatsapp|nic\s+nesly[sš][ií]m.*|je\s+to\s+moc\s+mal[eé].*|"
    r"p[ií]smo\s+je\s+moc\s+velk[eé].*|rychle\s+se\s+mi\s+to\s+vyb[ií]j[ií].*|"
    r"otev[rř]i\s+mi\s+fotky|zavolej\s+petrovi|sma[zž]\s+m[uů]j\s+z[aá]znam)$",
    re.IGNORECASE,
)

EXACT_TRANSLATIONS_TO_CS: dict[str, str] = {
    "yes": "ano",
    "yeah": "ano",
    "yep": "ano",
    "sure": "ano",
    "ok": "ano",
    "okay": "ano",
    "do it": "ano",
    "please do": "ano",
    "yes please": "ano",
    "áno": "ano",
    "hej": "ano",
    "no": "ne",
    "nope": "ne",
    "cancel": "ne",
    "do not": "ne",
    "don't": "ne",
    "no thank you": "ne",
    "nie": "ne",
    "stop": "stop",
    "stop it": "stop",
    "halt": "stop",
    "zastav": "stop",
    "undo": "vrať to zpátky",
    "undo that": "vrať to zpátky",
    "go back": "vrať to zpátky",
    "revert": "vrať to zpátky",
    "revert it": "vrať to zpátky",
    "vráť to späť": "vrať to zpátky",
    "done": "hotovo",
    "what": "cože",
    "pardon": "cože",
    "repeat": "zopakuj to",
    "repeat please": "zopakuj to",
    "say that again": "zopakuj to",
    "speak slower": "mluv pomaleji",
}

PATTERN_TRANSLATIONS_EN_TO_CS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"\b(whatsapp)\b.*\b(missing|disappeared|gone|lost|can'?t find)\b|\b(missing|disappeared|lost|can'?t find)\b.*\b(whatsapp)\b",
            re.I,
        ),
        "Zmizel mi WhatsApp",
    ),
    (
        re.compile(r"\b(can'?t hear|no sound|not ringing|too quiet|muted|silent)\b", re.I),
        "Nic neslyším, nezvoní mi to",
    ),
    (
        re.compile(
            r"\b(internet|seznam|website|web|wifi|wi-fi)\b.*\b(not working|broken|can'?t open|doesn'?t work|offline)\b|\b(can'?t open)\b.*\b(seznam)\b",
            re.I,
        ),
        "Na počítači mi nejde otevřít Seznam",
    ),
    (
        re.compile(
            r"\b(too small|can'?t read|can'?t see|bigger font|bigger text|increase font|larger text)\b", re.I
        ),
        "Je to moc malé, nevidím",
    ),
    (
        re.compile(r"\b(too big|too large|smaller font|smaller text|decrease font)\b", re.I),
        "Písmo je moc velké",
    ),
    (re.compile(r"\b(battery|drains? fast|low battery|charging)\b", re.I), "Rychle se mi to vybíjí"),
    (re.compile(r"\b(open)\b.*\b(photos|pictures|gallery)\b", re.I), "Otevři mi fotky"),
    (re.compile(r"\b(call)\b.*\b(petr|peter|son)\b", re.I), "Zavolej Petrovi"),
    (
        re.compile(r"\b(write|send|message|text)\b.*\b(lenka|granddaughter)\b.*\b(later|late)\b", re.I),
        "Napiš Lence, že přijdu později",
    ),
    (
        re.compile(r"\b(bank|police|anydesk|teamviewer|scam|password|pin code)\b", re.I),
        "Volal mi někdo z banky, ať si nainstaluju AnyDesk",
    ),
    (re.compile(r"\b(delete|clear|erase)\b.*\b(log|history|record)\b", re.I), "Smaž můj záznam"),
    (
        re.compile(r"\b(pop-?up|window|dialog)\b.*\b(close|stuck|won'?t close)\b", re.I),
        "Vyskočilo mi okno a nejde zavřít",
    ),
]


@dataclass
class TranscriptionResult:
    text: str
    raw_text: str
    language: str
    translated: bool = False
    elapsed_s: float = 0.0


def _norm_words(text: str) -> list[str]:
    return re.sub(r"[^\w\s]", " ", (text or "").lower()).split()


def is_prompt_leak(text: str, prompt: str) -> bool:
    """Whisper na nejasném zvuku někdy jen „opíše“ úvodní prompt. Takový přepis je k ničemu."""
    words = _norm_words(text)
    if len(words) < 6 or not prompt:
        return False
    return f" {' '.join(words)} " in f" {' '.join(_norm_words(prompt))} "


def is_whisper_hallucination(text: str, prompt: str = "") -> bool:
    """Vrátí True, pokud je text prázdný nebo jde o známou halucinaci Whisperu na tichu."""
    cleaned = (text or "").strip()
    if not cleaned:
        return True
    alnum = re.sub(r"[^a-zA-Z0-9áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ]", "", cleaned)
    if len(alnum) < 2:
        return True
    if RX_HALLUCINATION.search(cleaned):
        return True
    words = [w.lower() for w in cleaned.split()]
    if len(words) >= 6 and len(set(words)) == 1:
        return True
    if prompt and is_prompt_leak(cleaned, prompt):
        return True
    return False


def is_repeat_request(text: str) -> bool:
    """Vrátí True, pokud senior žádá o zopakování nebo zpomalení poslední věty ('Cože?', 'Zopakuj to')."""
    cleaned = re.sub(r"[\.\!\?\,\;]+$", "", (text or "").strip())
    if not cleaned or len(cleaned.split()) > 5:
        return False
    return bool(RX_REPEAT_SLOWER.search(cleaned))


def classify_utterance_completeness(text: str, is_followup: bool = False) -> str:
    """Rozhodne, zda průběžně přepsaná věta během pomlky:
    - končí nerozhodně / spojkou -> 'hesitation' (prodloužíme čekání na 2.8 s),
    - tvoří známý kompletní příkaz / odpověď -> 'complete' (ukončíme nahrávání ihned),
    - vypadá jako ukončená věta (Whisper ji uzavřel tečkou/otazníkem) -> 'likely_complete'
      (zkrátíme čekání na `likely_complete_tail_s`, ale nesekneme hned – senior může pokračovat),
    - jinak -> 'continue' (standardní 2.2 s ticho).
    """
    raw = (text or "").strip()
    if not raw:
        return "continue"

    ends_with_ellipsis = raw.endswith("...") or raw.endswith("…")
    cleaned = re.sub(r"[\.\!\?\,\;\…\-]+$", "", raw).strip()
    words = cleaned.split()
    if not words:
        return "continue"

    last_word = words[-1].lower()
    if last_word in HESITATION_ENDINGS or ends_with_ellipsis or raw.endswith(","):
        return "hesitation"

    if is_repeat_request(cleaned):
        return "complete"

    if RX_COMPLETE_UTTERANCE.match(cleaned):
        return "complete"

    if (
        is_followup
        and len(words) <= 2
        and last_word in ("ano", "jo", "ne", "stop", "hotovo", "dobře", "dobre")
    ):
        return "complete"

    if raw[-1] in ".!?" and (len(words) >= 2 or is_followup) and len(last_word) >= 2:
        return "likely_complete"

    return "continue"


RX_ENGLISH_WORDS = re.compile(
    r"\b(the|is|are|your|can'?t|cannot|don'?t|doesn'?t|won'?t|please|open|missing|disappeared|sound|hear|"
    r"small|big|battery|call|write|send|undo|phone|computer|help|what|how|it|and|of|with|have|has)\b",
    re.IGNORECASE,
)
RX_CZECH_LETTERS = re.compile(r"[áčďéěíňóřšťúůýž]", re.IGNORECASE)
_TRANSLATION_CACHE: dict[tuple[str, str], str] = {}
_ONLINE_DOWN_UNTIL = 0.0
ONLINE_TIMEOUT = httpx.Timeout(2.0, connect=1.0)
ONLINE_COOLDOWN_S = 300.0


def looks_english(text: str) -> bool:
    if RX_CZECH_LETTERS.search(text or ""):
        return False
    words = _norm_words(text)
    hits = len(RX_ENGLISH_WORDS.findall(text or ""))
    return hits >= 2 or (hits >= 1 and len(words) <= 3)


async def _online_translate(text: str, sl: str, tl: str) -> str:
    """Online překlad s cache a 5min pauzou po výpadku (bez internetu se nečeká u každé věty)."""
    global _ONLINE_DOWN_UNTIL
    key = (f"{sl}>{tl}", text)
    if key in _TRANSLATION_CACHE:
        return _TRANSLATION_CACHE[key]
    if time.monotonic() < _ONLINE_DOWN_UNTIL:
        return ""
    try:
        async with httpx.AsyncClient(timeout=ONLINE_TIMEOUT) as client:
            resp = await client.get(
                "https://translate.googleapis.com/translate_a/single",
                params={"client": "gtx", "sl": sl, "tl": tl, "dt": "t", "q": text},
            )
        if resp.status_code != 200:
            return ""
        data = resp.json()
        out = "".join(part[0] for part in data[0] if part and part[0]).strip()
    except Exception as e:
        _ONLINE_DOWN_UNTIL = time.monotonic() + ONLINE_COOLDOWN_S
        log.debug("Online překlad nedostupný (%s) – %d s ho nepoužívám", e, int(ONLINE_COOLDOWN_S))
        return ""
    if len(_TRANSLATION_CACHE) > 256:
        _TRANSLATION_CACHE.clear()
    _TRANSLATION_CACHE[key] = out
    return out


async def translate_to_czech(text: str, source_lang: str = "auto") -> tuple[str, bool]:
    """Přeloží cizojazyčný (např. anglický nebo slovenský) příkaz do češtiny pro HelpButton.

    Česká věta se nikdy neposílá na internet. Pořadí: přesná fráze -> vzory -> online (2 s, s cache).
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return "", False

    norm = re.sub(r"[\.\!\?\,\;]+$", "", cleaned).lower()
    if norm in EXACT_TRANSLATIONS_TO_CS:
        target = EXACT_TRANSLATIONS_TO_CS[norm]
        return target, (target.lower() != norm)

    english = looks_english(cleaned)
    if source_lang == "cs" and not english:
        return cleaned, False

    for rx, cs_sentence in PATTERN_TRANSLATIONS_EN_TO_CS:
        if rx.search(cleaned):
            return cs_sentence, True

    if source_lang in ("cs", "sk") and not english:
        return cleaned, False

    translated = await _online_translate(cleaned, "auto", "cs")
    if translated and translated.lower() != cleaned.lower():
        log.info("Přeloženo (%s -> cs): %r -> %r", source_lang, cleaned, translated)
        return translated, True
    return cleaned, False


async def translate_cs_to_en(text_cs: str) -> str:
    """Volitelný překlad odpovědi HelpButton z češtiny do angličtiny (pro `--bilingual` demo režim)."""
    cleaned = (text_cs or "").strip()
    if not cleaned:
        return ""
    return await _online_translate(cleaned, "cs", "en")


MIN_FOREIGN_LANG_PROB = 0.7
AUTO_ALLOWED_FOREIGN = ("en",)


def _trim_float32_silence(audio, rate: int = 16000):
    """Ořízne hluché ticho na úplném začátku a konci float32 pole, ale nechá štědrou rezervu
    (150 ms před a 220 ms po řeči), aby nikdy neuseklo nádech, souhlásky ani doznění úst."""
    try:
        import numpy as np

        n = int(audio.size)
        min_keep = int(rate * 0.80)
        if n <= min_keep:
            return audio
        win = max(160, int(rate * 0.020))
        num = n // win
        if num < 6:
            return audio
        frames = audio[: num * win].reshape(num, win)
        rms = np.sqrt(np.mean(frames * frames, axis=1))
        floor = float(np.percentile(rms, 15))
        thresh = max(0.0020, floor * 1.45)
        active = np.flatnonzero(rms >= thresh)
        if active.size == 0:
            return audio
        lead_pad = int(rate * 0.250)
        tail_pad = int(rate * 0.300)
        start = max(0, int(active[0]) * win - lead_pad)
        end = min(n, int(active[-1] + 1) * win + tail_pad)
        if end - start < min_keep:
            return audio
        return np.ascontiguousarray(audio[start:end], dtype=np.float32)
    except Exception:
        return audio


def _resample_audio_16k(audio, src_rate: int):
    """Pásmově omezené převzorkování float32 na 16 kHz bez aliasingu a okrajových lupnutí."""
    import math
    import numpy as np

    if src_rate == 16000 or audio.size <= 1:
        return audio
    try:
        from scipy.signal import resample_poly

        g = math.gcd(16000, src_rate)
        return resample_poly(audio.astype(np.float64), 16000 // g, src_rate // g).astype(np.float32)
    except Exception:
        pass
    n_in = int(audio.size)
    target_len = max(1, int(round(n_in * 16000.0 / src_rate)))
    pad = min(n_in - 1, max(32, src_rate // 40))
    padded = np.concatenate([audio[pad:0:-1], audio, audio[-2 : -2 - pad : -1]]).astype(np.float64)
    padded_out_len = max(1, int(round(padded.size * 16000.0 / src_rate)))
    spec = np.fft.rfft(padded)
    out_spec = np.zeros(padded_out_len // 2 + 1, dtype=np.complex128)
    k = min(spec.size, out_spec.size)
    out_spec[:k] = spec[:k]
    res = np.fft.irfft(out_spec, n=padded_out_len) * (padded_out_len / padded.size)
    start_out = int(round(pad * 16000.0 / src_rate))
    sliced = res[start_out : start_out + target_len]
    if sliced.size < target_len:
        sliced = np.pad(sliced, (0, target_len - sliced.size))
    return sliced.astype(np.float32)


def wav_to_float32(wav_bytes: bytes):
    """WAV (16 bit) -> numpy float32 (16 kHz mono) pro Whisper bez dekódování přes FFmpeg/PyAV.

    Zvládne i jiné vzorkovací frekvence než 16 kHz (pásmově omezeně v numpy/scipy) a ořízne přebytečné
    okrajové ticho se zachováním náběhu a doznění hlásek. Vrací None jen když chybí numpy
    nebo není platný 16-bit WAV."""
    try:
        import numpy as np

        with wave.open(io.BytesIO(wav_bytes), "rb") as w:
            if w.getsampwidth() != 2:
                return None
            rate = w.getframerate()
            channels = w.getnchannels()
            raw = w.readframes(w.getnframes())
        if not raw or rate <= 0:
            return None
        audio = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        if channels > 1:
            n_frames = audio.size // channels
            audio = audio[: n_frames * channels].reshape(n_frames, channels).mean(axis=1)
        if rate != 16000 and audio.size > 1:
            audio = _resample_audio_16k(audio, rate)
        return _trim_float32_silence(np.ascontiguousarray(audio, dtype=np.float32), 16000)
    except Exception:
        return None


class _Cancelled(Exception):
    pass


class SpeechToText:
    """Lokální Faster-Whisper STT na Raspberry Pi 5 se zálohou na server HelpButton."""

    def __init__(self, cfg: RpiConfig):
        self.cfg = cfg
        self._model = None
        self._lock = asyncio.Lock()
        self._run_lock = threading.Lock()
        self._transcribe_params: set[str] | None = None
        self._context_words: list[str] = []
        self.load_error: str | None = None
        self.last_elapsed_s: float = 0.0

    @property
    def local_available(self) -> bool:
        if self.cfg.stt_mode == "server" or self.load_error:
            return False
        return importlib.util.find_spec("faster_whisper") is not None

    @property
    def busy(self) -> bool:
        return self._run_lock.locked()

    def set_context_words(self, words: Iterable[str]) -> None:
        """Jména ze serveru (např. rodina „Lenka“, „Lence“) – Whisper je pak píše správně."""
        clean = [w.strip() for w in words if w and len(w.strip()) >= 2]
        self._context_words = list(dict.fromkeys(clean))[:20]

    def vocabulary(self) -> list[str]:
        words = [*BASE_VOCABULARY_CS, *(self.cfg.stt_vocabulary or []), *self._context_words]
        return list(dict.fromkeys(w for w in words if w))

    async def warmup(self) -> None:
        """Přednačte Whisper model do RAM a zahřeje kompletní dekódovací cestu včetně promptu a hotwords."""
        if not self.local_available:
            if self.load_error:
                log.warning("Lokální Whisper nelze použít (%s) – použije se STT na serveru.", self.load_error)
            else:
                log.info(
                    "Faster-Whisper není lokálně nainstalován – použije se STT na serveru (/api/button/audio)"
                )
            return
        model = await self._ensure_model()
        if model is None:
            return
        try:
            import numpy as np

            noise = (np.random.default_rng(0).standard_normal(4000) * 0.003).astype(np.float32)
            prompt = select_whisper_prompt(is_followup=False)
            hotwords = ", ".join(self.vocabulary())
            t0 = time.monotonic()

            def _warm() -> None:
                with self._run_lock:
                    segments, _ = model.transcribe(
                        noise,
                        **self._filter_kwargs(
                            model,
                            {
                                "language": "cs",
                                "beam_size": max(1, int(self.cfg.whisper_beam_size)),
                                "best_of": 1,
                                "temperature": 0.0,
                                "without_timestamps": True,
                                "vad_filter": False,
                                "condition_on_previous_text": False,
                                "suppress_blank": True,
                                "max_new_tokens": 32,
                                "language_detection_segments": 1,
                                "initial_prompt": prompt,
                                "hotwords": hotwords or None,
                            },
                        ),
                    )
                    for _ in segments:
                        pass

            await asyncio.to_thread(_warm)
            log.info("Whisper zahřátý (%.2f s) – první přepis bude okamžitý.", time.monotonic() - t0)
        except Exception as e:
            log.debug("Zahřátí Whisperu přeskočeno: %s", e)

    @staticmethod
    def _filter_init_kwargs(cls: object, kwargs: dict) -> dict:
        try:
            params = inspect.signature(cls).parameters  # type: ignore[arg-type]
            if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
                return kwargs
            return {k: v for k, v in kwargs.items() if k in params}
        except Exception:
            return kwargs

    async def _ensure_model(self):
        async with self._lock:
            if self._model is None and not self.load_error:
                log.info(
                    "Načítám Faster-Whisper model '%s' (%s, %d vláken) na Raspberry Pi 5…",
                    self.cfg.whisper_model,
                    self.cfg.whisper_compute_type,
                    self.cfg.whisper_threads,
                )
                try:
                    from faster_whisper import WhisperModel  # type: ignore[import-untyped]

                    def _load_whisper() -> object:
                        base_kwargs = {
                            "device": self.cfg.whisper_device,
                            "compute_type": self.cfg.whisper_compute_type,
                            "cpu_threads": self.cfg.whisper_threads,
                            "num_workers": 1,
                        }
                        try:
                            params = inspect.signature(WhisperModel).parameters
                            supports_local_only = "local_files_only" in params or any(
                                p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
                            )
                        except Exception:
                            supports_local_only = True
                        if supports_local_only:
                            try:
                                local_kw = self._filter_init_kwargs(
                                    WhisperModel, {**base_kwargs, "local_files_only": True}
                                )
                                return WhisperModel(self.cfg.whisper_model, **local_kw)
                            except Exception as cache_err:
                                log.debug(
                                    "Model '%s' není v lokální cache (%s) – zkouším stáhnout z HuggingFace.",
                                    self.cfg.whisper_model,
                                    cache_err,
                                )
                        return WhisperModel(
                            self.cfg.whisper_model,
                            **self._filter_init_kwargs(WhisperModel, base_kwargs),
                        )

                    self._model = await asyncio.to_thread(_load_whisper)
                    log.info("Faster-Whisper model '%s' připraven.", self.cfg.whisper_model)
                except Exception as e:
                    self.load_error = f"model '{self.cfg.whisper_model}' se nepodařilo načíst: {e}"
                    log.error(
                        "Faster-Whisper: %s. Přepínám na rozpoznávání řeči na serveru HelpButton. "
                        "(Model se stáhne při příštím startu s internetem.)",
                        self.load_error,
                    )
        return self._model

    def _filter_kwargs(self, model, kwargs: dict) -> dict:
        """Starší verze faster-whisper neznají např. `hotwords` – takové parametry se vynechají."""
        if self._transcribe_params is None:
            try:
                params = inspect.signature(model.transcribe).parameters
                if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
                    self._transcribe_params = set()
                else:
                    self._transcribe_params = set(params)
            except (TypeError, ValueError):
                self._transcribe_params = set()
        if not self._transcribe_params:
            return kwargs
        return {k: v for k, v in kwargs.items() if k in self._transcribe_params}

    async def transcribe_wav(
        self,
        wav_bytes: bytes,
        extra_prompt: str = "",
        *,
        is_followup: bool = False,
        cancel_event: threading.Event | None = None,
        fast_speculative: bool = False,
    ) -> TranscriptionResult:
        """Převede nahraný WAV (16 kHz mono) na český text s kontextovým promptem a fonetickou opravou."""
        if not wav_bytes or len(wav_bytes) < 1600:
            return TranscriptionResult("", "", "cs")
        if not self.local_available:
            return TranscriptionResult("", "", "cs")
        if fast_speculative and self._run_lock.locked():
            return TranscriptionResult("", "", "cs")
        model = await self._ensure_model()
        if model is None:
            return TranscriptionResult("", "", "cs")

        prompt = select_whisper_prompt(is_followup=is_followup, extra_prompt=extra_prompt)
        vocab = list(FOLLOWUP_HOTWORDS_CS) if is_followup else self.vocabulary()
        hotwords = ", ".join(vocab)
        if self._filter_kwargs(model, {"hotwords": hotwords}).get("hotwords") is None:
            prompt = f"{prompt} {hotwords}."
            hotwords = ""
        lang_cfg = (self.cfg.whisper_language or "cs").strip().lower()
        beam = 1 if fast_speculative else max(1, int(self.cfg.whisper_beam_size))
        audio = wav_to_float32(wav_bytes)
        if audio is None:
            audio_src = lambda: io.BytesIO(wav_bytes)  # noqa: E731
        else:
            audio_src = lambda: audio  # noqa: E731
        audio_s = (len(wav_bytes) - 44) / 32000.0
        has_voiced_energy = True
        if audio is not None and audio.size > 0:
            import numpy as np

            has_voiced_energy = float(np.max(np.abs(audio))) >= 0.004

        def _kwargs(language: str | None, *, relaxed: bool) -> dict:
            active_beam = max(2, beam) if relaxed else beam
            kw = {
                "language": language,
                "task": "transcribe",
                "beam_size": active_beam,
                "best_of": 1,
                "temperature": 0.0,
                "condition_on_previous_text": False,
                "without_timestamps": True,
                "suppress_blank": True,
                "compression_ratio_threshold": 2.4,
                "no_speech_threshold": None if relaxed else 0.78,
                "log_prob_threshold": None if relaxed else -1.2,
                "language_detection_segments": 1,
                "max_new_tokens": 24 if is_followup else 128,
                "initial_prompt": None if relaxed else prompt,
                "hotwords": None if relaxed else (hotwords or None),
                "vad_filter": False if relaxed else bool(self.cfg.whisper_vad_filter),
                "vad_parameters": {
                    "threshold": 0.35,
                    "min_speech_duration_ms": 120,
                    "min_silence_duration_ms": 350,
                    "speech_pad_ms": 200,
                },
            }
            return self._filter_kwargs(model, kw)

        def _decode(language: str | None, relaxed: bool) -> tuple[str, str, float]:
            if cancel_event is not None and cancel_event.is_set():
                raise _Cancelled()
            segments, info = model.transcribe(audio_src(), **_kwargs(language, relaxed=relaxed))
            detected = getattr(info, "language", language or "cs") or "cs"
            prob = float(getattr(info, "language_probability", 1.0) or 0.0)
            if language is None and not (
                detected == "cs" or (detected in AUTO_ALLOWED_FOREIGN and prob >= MIN_FOREIGN_LANG_PROB)
            ):
                segments, info = model.transcribe(audio_src(), **_kwargs("cs", relaxed=relaxed))
                detected, prob = "cs", 1.0
            parts = []
            for seg in segments:
                if cancel_event is not None and cancel_event.is_set():
                    raise _Cancelled()
                parts.append(seg.text.strip())
            return " ".join(p for p in parts if p).strip(), detected, prob

        def _run() -> tuple[str, str, bool]:
            if fast_speculative and self._run_lock.locked():
                raise _Cancelled()
            with self._run_lock:
                if cancel_event is not None and cancel_event.is_set():
                    raise _Cancelled()
                language = None if lang_cfg == "auto" else lang_cfg
                raw, detected, _prob = _decode(language, relaxed=False)
                retried = False
                if (
                    not fast_speculative
                    and has_voiced_energy
                    and is_whisper_hallucination(raw, prompt)
                    and (audio_s >= 0.35 or (is_followup and audio_s >= 0.16))
                ):
                    retry_lang = detected if detected in ("cs", *AUTO_ALLOWED_FOREIGN) else "cs"
                    raw2, detected2, _ = _decode(retry_lang, relaxed=True)
                    if not is_whisper_hallucination(raw2, prompt):
                        raw, detected, retried = raw2, detected2, True
                return raw, detected, retried

        t0 = time.monotonic()
        try:
            raw_text, detected_lang, retried = await asyncio.to_thread(_run)
        except _Cancelled:
            return TranscriptionResult("", "", "cs")
        except Exception as e:
            log.error("Whisper přepis selhal: %s", e)
            return TranscriptionResult("", "", "cs")
        elapsed = time.monotonic() - t0
        self.last_elapsed_s = elapsed
        log.info(
            "Whisper přepis (%s, %.2f s%s): %r",
            detected_lang,
            elapsed,
            ", druhý pokus" if retried else "",
            raw_text,
        )

        if is_whisper_hallucination(raw_text, prompt):
            log.info("Odfiltrována halucinace Whisperu: %r", raw_text)
            return TranscriptionResult("", raw_text, detected_lang, elapsed_s=elapsed)

        cs_text = raw_text
        was_translated = False
        if self.cfg.auto_translate_to_cs and detected_lang != "cs":
            try:
                cs_text, was_translated = await translate_to_czech(raw_text, detected_lang)
            except Exception as e:
                log.debug("Překlad přeskočen: %s", e)

        if self.cfg.fuzzy_senior_repair:
            cs_text = normalize_senior_speech(cs_text)

        return TranscriptionResult(
            cs_text, raw_text, detected_lang, translated=was_translated, elapsed_s=elapsed
        )
