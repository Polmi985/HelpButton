"""Konfigurace serveru (helpbutton.toml + osobní helpbutton.user.toml). Vše má rozumné výchozí hodnoty.

Jeden server obsluhuje obě části projektu:
  * HelpButton – tlačítko pro seniora (sekce [helpbutton], [family], [llm], [vlm], [voice] …),
  * Jiřík – chat na počítači, který ovládá tento i spárované počítače (sekce [jirik] a [link]).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..common.safety import normalize


@dataclass
class Contact:
    name: str
    phone: str = ""
    aliases: list[str] = field(default_factory=list)
    dative: str = ""

    def matches(self, text: str) -> bool:
        t = normalize(text)
        words = set(t.split())
        names = [normalize(self.name)] + [normalize(a) for a in self.aliases]
        return any(n in words or (len(n) > 3 and n in t) for n in names if n)

    @property
    def to_whom(self) -> str:
        return self.dative or self.name


@dataclass
class App:
    name: str
    package: str
    aliases: list[str] = field(default_factory=list)

    def matches(self, text: str) -> bool:
        t = normalize(text)
        names = [normalize(self.name)] + [normalize(a) for a in self.aliases]
        return any(n and n in t for n in names)


DEFAULT_APPS = [
    App(
        "WhatsApp",
        "com.whatsapp",
        ["whatsapp", "whats app", "wotsap", "watsap", "vocap", "whatsup", "wocap", "vatsap"],
    ),
    App("Fotky", "com.google.android.apps.photos", ["fotky", "fotografie", "obrazky", "galerie"]),
    App("Počasí", "com.google.android.googlequicksearchbox", ["pocasi"]),
    App("Volání", "com.google.android.dialer", ["aplikace telefon", "aplikace na volani", "telefonovani"]),
    App("Zprávy", "com.google.android.apps.messaging", ["zpravy", "sms", "esemesky"]),
    App("Fotoaparát", "com.android.camera", ["fotoaparat", "foťák", "fotak", "kamera"]),
    App("YouTube", "com.google.android.youtube", ["youtube", "jutub", "jutube"]),
    App("Seznam", "cz.seznam.sbrowser", ["seznam prohlizec"]),
]

DEFAULT_SITES = {
    "seznam": "https://www.seznam.cz",
    "seznam zpravy": "https://www.seznamzpravy.cz",
    "seznamzpravy": "https://www.seznamzpravy.cz",
    "zpravy na seznamu": "https://www.seznamzpravy.cz",
    "zpravy ze seznamu": "https://www.seznamzpravy.cz",
    "zpravy seznam": "https://www.seznamzpravy.cz",
    "novinky": "https://www.novinky.cz",
    "idnes": "https://www.idnes.cz",
    "zpravy na idnesu": "https://www.idnes.cz/zpravy",
    "irozhlas": "https://www.irozhlas.cz",
    "google": "https://www.google.com",
    "youtube": "https://www.youtube.com",
    "pocasi": "https://pocasi.seznam.cz",
    "mapy": "https://mapy.cz",
    "jizdni rad": "https://idos.cz",
    "email": "https://email.seznam.cz",
    "e mail": "https://email.seznam.cz",
    "posta": "https://email.seznam.cz",
    "ceska televize": "https://www.ceskatelevize.cz",
    "ct24": "https://ct24.ceskatelevize.cz",
}

NEWS_FOR_SITE = {
    "seznam.cz": "https://www.seznamzpravy.cz",
    "seznamzpravy.cz": "https://www.seznamzpravy.cz",
    "idnes.cz": "https://www.idnes.cz/zpravy",
    "novinky.cz": "https://www.novinky.cz",
    "ceskatelevize.cz": "https://ct24.ceskatelevize.cz",
    "google.com": "https://news.google.com/?hl=cs&gl=CZ",
}


@dataclass
class Config:
    senior_name: str = "Paní Jano"
    senior_female: bool = True
    home_city: str = ""
    family: Contact = field(
        default_factory=lambda: Contact(
            "Petr", aliases=["petr", "petrovi", "syn", "synovi"], dative="Petrovi"
        )
    )
    contacts: list[Contact] = field(default_factory=list)
    apps: list[App] = field(default_factory=lambda: list(DEFAULT_APPS))
    sites: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_SITES))

    host: str = "0.0.0.0"
    port: int = 8765
    token: str = ""
    allowed_hosts: list[str] = field(default_factory=list)
    data_dir: Path = Path("data")
    agent_runs_keep_days: int = 14
    mdns: bool = True
    adb_watchdog: bool = True

    llm_provider: str = "auto"
    claude_model: str = "claude-opus-5-5"
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3-vl:2b-instruct"
    llm_local_first: bool = True
    gemini_model: str = "gemini-3.5-flash"

    vlm_provider: str = "auto"
    gemini_vlm_model: str = "gemini-3.5-flash"
    ollama_vlm_model: str = "qwen3-vl:2b-instruct"
    vlm_local_first: bool = True
    ollama_vlm_max_pixels: int = 1_000_000
    ollama_vlm_num_ctx: int = 8192
    agent_max_steps: int = 25
    agent_zoom_refine: bool = True
    providers: list = field(default_factory=list)

    whisper_model: str = "small"
    whisper_device: str = "auto"
    whisper_compute_type: str = "auto"
    whisper_beam_size: int = 1
    whisper_threads: int = 0
    whisper_warmup: bool = True
    piper_bin: str = "piper"
    piper_model: str = ""                     # prázdné = voices/cs_CZ-jirka-medium.onnx, pokud existuje
    speech_rate: float = 0.85                 # hlas pro seniora (pomaleji)

    notify_webhook: str = ""

    # Jiřík – chat na počítači (/chat a desktopová aplikace)
    user_name: str = ""                       # jméno uživatele (1. pád)
    greeting: str = ""                        # oslovení v 5. pádě („Adame“); prázdné = jméno
    user_female: bool = False
    model: str = "qwen3.5:4b"                 # Qwen3.5 4B: text + obrázky + nástroje, vejde se do 6 GB grafiky
    model_uncensored: str = "huihui_ai/qwen3.5-abliterated:4b"
    num_ctx: int = 8192                       # stejný kontext pro chat i jeho agenta – Ollama model nepřenačítá
    keep_alive: str = "30m"
    chat_speech_rate: float = 1.0             # Jiřík mluví běžnou rychlostí
    chat_warmup: bool = True                  # při startu načíst model chatu do grafiky (je-li stažený)

    # Jiřík Link – ovládání dalších počítačů v síti
    link_port: int = 8771                     # TLS brána pro vzdálené počítače
    link_udp_port: int = 8772                 # probouzení vzdálených počítačů
    pair_port: int = 8773                     # párovací stránka (jen během párování, max. 15 min)

    def g(self, female: str, male: str) -> str:
        """Tvar slova podle rodu seniora."""
        return female if self.senior_female else male

    @property
    def all_contacts(self) -> list[Contact]:
        names = {c.name for c in self.contacts}
        return ([self.family] if self.family.name not in names else []) + self.contacts

    def find_contact(self, text: str) -> Contact | None:
        for c in self.all_contacts:
            if c.matches(text):
                return c
        return None

    def find_app(self, text: str) -> App | None:
        for a in self.apps:
            if a.matches(text) or a.package == text:
                return a
        return None

    def find_site(self, text: str) -> str | None:
        import difflib

        t = normalize(text)
        words = t.split()
        hits = []
        for key, url in self.sites.items():
            k = normalize(key)
            if not k:
                continue
            if " " in k:
                ok = f" {k} " in f" {t} "
            else:
                stem = k[: max(4, len(k) - 2)]
                ok = k in t or (
                    len(k) >= 5
                    and any(
                        w.startswith(stem) or difflib.SequenceMatcher(None, w, k).ratio() >= 0.85
                        for w in words
                        if len(w) >= 4
                    )
                )
            if ok:
                hits.append((len(k), url))
        if hits:
            return max(hits)[1]
        import re

        m = re.search(r"\b((?:www\.)?[a-z0-9-]+\.(?:cz|com|sk|eu|org|net))\b", t)
        if m:
            host = m.group(1)
            return "https://" + (host if host.startswith("www.") else host)
        return None


def _contact(d: dict[str, Any]) -> Contact:
    return Contact(
        name=d["name"],
        phone=str(d.get("phone", "")),
        aliases=list(d.get("aliases", [])),
        dative=d.get("dative", ""),
    )


def load_config(path: str | os.PathLike | None = None) -> Config:
    cfg = Config()
    p = Path(path or os.environ.get("HELPBUTTON_CONFIG", "helpbutton.toml"))
    base = p.resolve().parent
    if p.exists():
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
        a = raw.get("helpbutton", {})
        cfg.senior_name = a.get("senior_name", cfg.senior_name)
        cfg.senior_female = bool(a.get("senior_female", cfg.senior_female))
        cfg.home_city = str(a.get("home_city", cfg.home_city) or "")
        cfg.host = a.get("host", cfg.host)
        cfg.port = int(a.get("port", cfg.port))
        cfg.token = a.get("token", cfg.token)
        cfg.allowed_hosts = [str(h) for h in a.get("allowed_hosts", cfg.allowed_hosts)]
        cfg.data_dir = Path(a.get("data_dir", cfg.data_dir))
        cfg.agent_runs_keep_days = int(a.get("agent_runs_keep_days", cfg.agent_runs_keep_days))
        cfg.mdns = bool(a.get("mdns", cfg.mdns))
        cfg.adb_watchdog = bool(a.get("adb_watchdog", cfg.adb_watchdog))
        if "family" in raw:
            cfg.family = _contact(raw["family"])
        cfg.contacts = [_contact(c) for c in raw.get("contacts", [])]
        if "apps" in raw:
            cfg.apps = [
                App(x["name"], x["package"], list(x.get("aliases", []))) for x in raw["apps"]
            ] + cfg.apps
        cfg.sites.update(raw.get("sites", {}))
        llm = raw.get("llm", {})
        cfg.llm_provider = llm.get("provider", cfg.llm_provider)
        cfg.claude_model = llm.get("claude_model", cfg.claude_model)
        cfg.ollama_url = llm.get("ollama_url", cfg.ollama_url)
        cfg.ollama_model = llm.get("ollama_model", cfg.ollama_model)
        cfg.llm_local_first = bool(llm.get("local_first", cfg.llm_local_first))
        cfg.gemini_model = llm.get("gemini_model", cfg.gemini_model)
        g = raw.get("vlm", {})
        cfg.vlm_provider = g.get("provider", cfg.vlm_provider)
        cfg.gemini_vlm_model = g.get("gemini_model", cfg.gemini_vlm_model)
        cfg.ollama_vlm_model = g.get("ollama_model", cfg.ollama_vlm_model)
        cfg.vlm_local_first = bool(g.get("local_first", cfg.vlm_local_first))
        cfg.ollama_vlm_max_pixels = int(g.get("ollama_max_pixels", cfg.ollama_vlm_max_pixels))
        cfg.ollama_vlm_num_ctx = int(g.get("ollama_num_ctx", cfg.ollama_vlm_num_ctx))
        cfg.agent_max_steps = int(g.get("max_steps", cfg.agent_max_steps))
        cfg.agent_zoom_refine = bool(g.get("zoom_refine", cfg.agent_zoom_refine))
        from .openai_compat import from_config

        cfg.providers = from_config(raw.get("providers", []))
        v = raw.get("voice", {})
        cfg.whisper_model = v.get("whisper_model", cfg.whisper_model)
        cfg.whisper_device = v.get("whisper_device", cfg.whisper_device)
        cfg.whisper_compute_type = str(v.get("whisper_compute_type", cfg.whisper_compute_type))
        cfg.whisper_beam_size = max(1, int(v.get("whisper_beam_size", cfg.whisper_beam_size)))
        cfg.whisper_threads = max(0, int(v.get("whisper_threads", cfg.whisper_threads)))
        cfg.whisper_warmup = bool(v.get("whisper_warmup", cfg.whisper_warmup))
        cfg.piper_bin = v.get("piper_bin", cfg.piper_bin)
        cfg.piper_model = v.get("piper_model", cfg.piper_model)
        cfg.speech_rate = float(v.get("speech_rate", cfg.speech_rate))
        cfg.notify_webhook = raw.get("notify", {}).get("webhook_url", cfg.notify_webhook)
        _load_jirik(cfg, raw.get("jirik", {}))
        lk = raw.get("link", {})
        cfg.link_port = int(lk.get("port", cfg.link_port))
        cfg.link_udp_port = int(lk.get("udp_port", cfg.link_udp_port))
        cfg.pair_port = int(lk.get("pair_port", cfg.pair_port))
        if not cfg.data_dir.is_absolute():
            cfg.data_dir = base / cfg.data_dir
        for key, env in (("gemini_api_key", "GEMINI_API_KEY"), ("anthropic_api_key", "ANTHROPIC_API_KEY")):
            value = str(raw.get("keys", {}).get(key, "")).strip()
            if value and not os.environ.get(env):
                os.environ[env] = value
    # osobní nastavení Jiříka (jméno, oslovení…) mimo git: helpbutton.user.toml vedle helpbutton.toml
    user = p.with_name("helpbutton.user.toml")
    if user.exists():
        _load_jirik(cfg, tomllib.loads(user.read_text(encoding="utf-8")).get("jirik", {}))
    if not cfg.piper_model and (base / "voices" / "cs_CZ-jirka-medium.onnx").exists():
        cfg.piper_model = str(base / "voices" / "cs_CZ-jirka-medium.onnx")
    elif cfg.piper_model and not Path(cfg.piper_model).is_absolute() and p.exists():
        cfg.piper_model = str(base / cfg.piper_model)
    cfg.token = os.environ.get("HELPBUTTON_TOKEN", cfg.token)
    cfg.llm_provider = os.environ.get("HELPBUTTON_LLM", cfg.llm_provider)
    cfg.vlm_provider = os.environ.get("HELPBUTTON_VLM", cfg.vlm_provider)
    return cfg


def _load_jirik(cfg: Config, j: dict[str, Any]) -> None:
    cfg.user_name = str(j.get("user_name", cfg.user_name) or "")
    cfg.greeting = str(j.get("greeting", cfg.greeting) or "")
    cfg.user_female = bool(j.get("user_female", cfg.user_female))
    cfg.model = str(j.get("model", cfg.model))
    cfg.model_uncensored = str(j.get("uncensored", cfg.model_uncensored))
    cfg.num_ctx = int(j.get("num_ctx", cfg.num_ctx))
    cfg.keep_alive = str(j.get("keep_alive", cfg.keep_alive))
    cfg.chat_speech_rate = float(j.get("speech_rate", cfg.chat_speech_rate))
    cfg.chat_warmup = bool(j.get("warmup", cfg.chat_warmup))
    if j.get("home_city") and not cfg.home_city:
        cfg.home_city = str(j["home_city"])


def chat_config(cfg: Config) -> Config:
    """Kopie nastavení pro Jiříka. Chat mění model a kontext za běhu (úrovně myšlení v Nastavení) –
    na kopii to neovlivní GUI agenta tlačítka pro seniora."""
    import dataclasses

    c = dataclasses.replace(cfg)
    c.ollama_vlm_model = c.model
    c.ollama_vlm_num_ctx = c.num_ctx
    c.speech_rate = c.chat_speech_rate
    return c
