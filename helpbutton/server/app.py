"""HTTP/WebSocket rozhraní – jeden server pro tlačítko HelpButton i chat Jiřík.

HelpButton (tlačítko pro seniora):
  /                rodinná stránka (jen čtení)
  /button          simulátor tlačítka + obrazovka seniora
  /ws/ui           živé události pro tyto stránky
  POST /api/press, /api/utterance, /api/audio, /api/button/audio, /api/stop

Jiřík (chat, který ovládá tento a spárované počítače):
  /chat            chat (i v desktopové aplikaci app/)
  /ws/chat         živé události pro všechna otevřená okna chatu (jedna konverzace, jeden hlas)
  POST /api/chat, /api/chat/stop, /api/clear, /api/settings, /api/models/*, /api/link/*

Společné:
  /ws/device       démony na zařízeních (MCP přes WebSocket)
  /api/hello       server se prokáže tlačítku tokenem, /api/ping = běží server?
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..common.discovery import Advertiser
from ..common.protocol import AUTH_SCHEME, api_key, auth_proof, new_nonce, proof_ok
from .adb_watchdog import AdbWatchdog
from .brain import Brain
from .chat import Chat
from .config import Config, chat_config, load_config
from .family import Event, FamilyLog
from .hardware import LEVELS, Hardware
from .link import Link, firewall_rule_present, network_is_public, open_firewall
from .models import Models
from .orchestrator import Orchestrator
from .registry import Registry, dumps
from .settings import MAX_CTX, MIN_CTX, SettingsStore, clamp_ctx
from .voice import Speaker, Voice

log = logging.getLogger("helpbutton.server")
STATIC = Path(__file__).parent / "static"


class Hub:
    """Rozesílá živé události otevřeným stránkám."""

    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()

    async def send(self, msg: dict[str, Any]) -> None:
        data = dumps(msg)
        for ws in list(self.clients):
            try:
                await ws.send_text(data)
            except Exception:  # noqa: BLE001
                self.clients.discard(ws)


class ChatHub:
    """Rozesílá živé události všem otevřeným oknům chatu Jiřík."""

    def __init__(self) -> None:
        # okno → None (živé), nebo seznam událostí, které čekají, než okno dostane úvodní snímek stavu
        self.clients: dict[WebSocket, list[str] | None] = {}

    async def send(self, msg: dict[str, Any]) -> None:
        if not self.clients:
            return
        data = dumps(msg)
        for ws, buf in list(self.clients.items()):
            if buf is not None:
                buf.append(data)
                continue
            try:
                await ws.send_text(data)
            except Exception:  # noqa: BLE001
                self.clients.pop(ws, None)

    async def join(self, ws: WebSocket, snapshot: Callable[[], dict[str, Any]]) -> None:
        """Snímek stavu jako první zpráva; co se mezitím stalo, přijde hned za ním (nic se neztratí ani nezdvojí)."""
        self.clients[ws] = []
        await ws.send_text(dumps(snapshot()))
        while self.clients.get(ws):
            pending, self.clients[ws] = self.clients[ws], []
            for d in pending:
                await ws.send_text(d)
        if ws in self.clients:
            self.clients[ws] = None


LAN_SUFFIXES = (".local", ".localhost", ".lan", ".home", ".home.arpa", ".internal")


def _hostname(host: str) -> str:
    host = (host or "").strip().lower()
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    if host.count(":") == 1:
        host = host.split(":", 1)[0]
    return host.rstrip(".")


def host_allowed(host_header: str, extra: list[str] | tuple[str, ...] = ()) -> bool:
    """Ochrana proti DNS rebinding: server odpovídá jen na IP adresy a jména z domácí sítě.

    Útočníkova doména (evil.example → 192.168.1.10) má v hlavičce Host svoje jméno s tečkou,
    jména bez tečky (localhost, helpbutton-pc) a .local/.lan projdou.
    """
    host = _hostname(host_header)
    if not host or host in {h.lower() for h in extra}:
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    return "." not in host or host.endswith(LAN_SUFFIXES)


def origin_allowed(origin: str | None, host_header: str) -> bool:
    """Prohlížeč posílá Origin u POST a WebSocketů. Cizí stránka (CSRF, cross-site WebSocket)
    tak nemůže mačkat tlačítko, posílat věty ani se vydávat za zařízení. Bez Origin = ne-prohlížeč."""
    if origin is None:
        return True
    if origin == "null":
        return False
    return urlsplit(origin).netloc.lower() == (host_header or "").strip().lower()


COOKIE = "helpbutton_key"
OPEN_PATHS = ("/static/", "/login", "/api/login", "/ws/device", "/api/hello", "/api/ping")
# Jiřík spouští PowerShell a ovládá počítače – z jiných počítačů jen s klíčem, i když token chybí (pak vůbec)
CHAT_PATHS = ("/chat", "/api/chat", "/api/hush", "/api/clear", "/api/settings", "/api/context", "/api/models",
              "/api/link", "/api/devices", "/ws/chat")
SERVER_PROOF_ROLE = "server-api"
MAX_AUDIO_BYTES = 8_000_000
MAX_TEXT_CHARS = 2000
CHAT_MAX_TEXT = 16000
CHAT_MAX_IMAGE = 6_000_000
PAGES = ("/", "/button", "/chat")


def is_local(client: Any) -> bool:
    """Požadavek ze stejného počítače (helpbutton.cli, tlačítko u seniora, záložní server na Raspberry Pi)."""
    host = getattr(client, "host", "") or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class Lockout:
    """Po opakovaně špatném klíči z jedné adresy chvíli nic nepouštět (hádání krátkého tokenu)."""

    def __init__(self, limit: int = 10, window_s: float = 600):
        self.limit, self.window = limit, window_s
        self.fails: dict[str, list[float]] = {}

    def blocked(self, ip: str) -> bool:
        now = time.time()
        recent = [t for t in self.fails.get(ip, []) if now - t < self.window]
        self.fails[ip] = recent
        return len(recent) >= self.limit

    def fail(self, ip: str) -> None:
        self.fails.setdefault(ip, []).append(time.time())
        if len(self.fails) > 1000:
            self.fails = dict(list(self.fails.items())[-500:])


class Login(BaseModel):
    key: str


class Utterance(BaseModel):
    text: str = Field(max_length=MAX_TEXT_CHARS)
    source: str = Field("web", max_length=40)


class Press(BaseModel):
    source: str = "web"


# --- Jiřík ---------------------------------------------------------------------------------------
class ChatIn(BaseModel):
    text: str = Field("", max_length=CHAT_MAX_TEXT)
    images: list[str] = Field(default_factory=list, max_length=4)
    space: str = Field("chat", max_length=12)
    target: str = Field("", max_length=60)


class SpaceIn(BaseModel):
    space: str = Field("", max_length=12)


class SettingsIn(BaseModel):
    voice: bool | None = None
    uncensored: bool | None = None
    level: str | None = None
    autonomous: bool | None = None
    num_ctx: int | None = None


class LevelIn(BaseModel):
    level: str
    uncensored: bool | None = None


class ModelIn(BaseModel):
    model: str = Field(max_length=120)


class PairIn(BaseModel):
    name: str = Field(max_length=40)


class ConfirmIn(BaseModel):
    ok: bool
    name: str = Field("", max_length=40)


class FirewallIn(BaseModel):
    switch_private: bool = False


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or load_config()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    hub = Hub()
    registry = Registry()
    family = FamilyLog(cfg.data_dir, cfg.notify_webhook)
    brain = Brain(cfg)
    voice = Voice(cfg)
    orch = Orchestrator(cfg, registry, brain, family, emit=hub.send)

    # --- Jiřík: chat na počítači (vlastní kopie nastavení – úrovně myšlení mění model za běhu) ---
    ccfg = chat_config(cfg)
    chat_hub = ChatHub()
    settings = SettingsStore(cfg.data_dir)
    if settings.value.level not in LEVELS:
        settings.update({"level": "low"})
    if "num_ctx" in settings.explicit:
        ccfg.num_ctx = ccfg.ollama_vlm_num_ctx = clamp_ctx(settings.value.num_ctx)
    else:
        ccfg.num_ctx = ccfg.ollama_vlm_num_ctx = clamp_ctx(ccfg.num_ctx)
        settings.value.num_ctx = ccfg.num_ctx
    speaker = Speaker(voice, chat_hub.send, cfg.data_dir, rate=cfg.chat_speech_rate)
    speaker.enabled = settings.value.voice
    link = Link(ccfg, registry, chat_hub.send)
    hardware = Hardware(cfg.data_dir)
    models = Models(ccfg, hardware, chat_hub.send)
    spaces = {
        s: Chat(ccfg, registry, link, speaker, settings, hardware, models, chat_hub.send, space=s)
        for s in ("chat", "devices")
    }
    chat = spaces["chat"]

    def space(name: str) -> Chat:
        return spaces.get(name) or chat

    def chat_devices() -> list[dict[str, Any]]:
        """Počítače pro Jiříka: tento počítač a počítače spárované přes Jiřík Link."""
        return [
            {
                "id": d.device_id,
                "name": d.name,
                "online": d.available,
                "remote": d.device_id.startswith("link-"),
                "locked": bool((d.status or {}).get("locked")),
            }
            for d in registry.devices.values()
            if d.kind == "pc" and (d.local or d.device_id.startswith("link-"))
        ]

    last_chat_devices: list[Any] = [None]

    async def devices_changed() -> None:
        await hub.send({"type": "devices", "devices": registry.public()})
        now = chat_devices()
        if now != last_chat_devices[0]:  # stav chodí každých 10 s – do chatu posílat jen změny
            last_chat_devices[0] = now
            await chat_hub.send({"type": "devices", "devices": now, "nodes": link.public()})

    async def models_info() -> dict[str, Any]:
        names = await models.installed()
        return {
            "hardware": hardware.probe(),
            "levels": hardware.describe(names, settings.value.uncensored),
            "pulls": models.pulling(),
            "installed": sorted(names),
        }

    async def on_event(e: Event) -> None:
        await hub.send({"type": "event", "event": e.to_dict(), "stats": family.stats()})

    registry.on_change = devices_changed
    family.on_event = on_event

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN202
        adv = Advertiser(cfg.port) if cfg.mdns else None
        dog = AdbWatchdog() if cfg.adb_watchdog else None
        if adv:
            await adv.start()
        if dog:
            await dog.start()
        if not cfg.token and cfg.host not in ("127.0.0.1", "localhost", "::1"):
            log.warning(
                "Párovací token není nastavený – připojit se jako zařízení a ovládat HelpButton přes API může "
                "kdokoli v síti. Nastavte [helpbutton] token v helpbutton.toml (stejný i démonům přes --token / "
                "HELPBUTTON_TOKEN)."
            )
        elif cfg.token:
            log.info(
                "Přístup z jiných počítačů jen s klíčem HelpButton: prohlížeč se přihlásí tokenem na /login, "
                "ostatní klienti hlavičkou Authorization (klíč vypíše: python -m helpbutton.server --api-key)"
            )
        warm_task = None
        if getattr(cfg, "whisper_warmup", True) and voice.stt_available:
            warm_task = asyncio.create_task(voice.warmup())
        speaker.start()
        await link.start()
        chat_tasks = [asyncio.create_task(chat.warm())] if cfg.chat_warmup else []
        if voice.available and voice._piper_lib():
            try:  # hlas načíst předem v hlavním vlákně (~1 s; ve vlákně ~12 s)
                voice._synth("Ahoj.")
            except Exception as e:  # noqa: BLE001
                log.warning("Hlas se nepodařilo načíst: %s", e)
        provider = await brain.provider()
        vlm = await orch.vlm_ready()
        log.info(
            "HelpButton server běží na portu %s · porozumění: %s · GUI agent: %s · STT: %s · TTS: %s",
            cfg.port,
            provider,
            vlm.name if vlm else "vypnutý",
            voice.stt_available,
            voice.tts_available,
        )
        log.info("Jiřík (chat): http://127.0.0.1:%s/chat · úroveň %s", cfg.port, settings.value.level)
        yield
        if warm_task is not None and not warm_task.done():
            warm_task.cancel()
        for t in chat_tasks:
            t.cancel()
        for sp in spaces.values():
            await sp.stop()
        await speaker.close()
        await link.stop()
        if adv:
            await adv.stop()
        if dog:
            await dog.stop()

    app = FastAPI(title="HelpButton LAN", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.cfg, app.state.registry, app.state.orch, app.state.family, app.state.voice = (
        cfg,
        registry,
        orch,
        family,
        voice,
    )
    app.state.chat, app.state.link, app.state.spaces = chat, link, spaces
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    def _request_ok(headers: Any) -> bool:
        host = headers.get("host", "")
        return host_allowed(host, cfg.allowed_hosts) and origin_allowed(headers.get("origin"), host)

    lockout = Lockout()

    def _presented(headers: Any, cookies: Any) -> str:
        auth = str(headers.get("authorization") or "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return str(cookies.get(COOKIE) or "")

    def _key_ok(given: str) -> bool:
        return (
            bool(cfg.token) and bool(given) and hmac.compare_digest(given.encode(), api_key(cfg.token).encode())
        )

    def _needs_key(client: Any, path: str) -> bool:
        if path.startswith(OPEN_PATHS) or is_local(client):
            return False
        return path.startswith(CHAT_PATHS) or bool(cfg.token)

    @app.middleware("http")
    async def same_origin_only(request: Request, call_next):  # noqa: ANN001, ANN202
        if not _request_ok(request.headers):
            log.warning(
                "Odmítnut požadavek %s %s (Host=%s, Origin=%s)",
                request.method,
                request.url.path,
                request.headers.get("host"),
                request.headers.get("origin"),
            )
            return JSONResponse({"error": "cizí původ požadavku"}, status_code=403)
        path = request.url.path
        if _needs_key(request.client, path):
            ip = getattr(request.client, "host", "?")
            if lockout.blocked(ip):
                return JSONResponse(
                    {"error": "příliš mnoho špatných pokusů, zkuste to za 10 minut"}, status_code=429
                )
            given = _presented(request.headers, request.cookies)
            if not _key_ok(given):
                if given:
                    lockout.fail(ip)
                    log.warning("Odmítnut %s %s z %s: špatný klíč", request.method, path, ip)
                if request.method == "GET" and path in PAGES:
                    if not cfg.token:
                        return JSONResponse(
                            {"error": "Jiřík jde z jiných počítačů otevřít jen s párovacím tokenem "
                                      "([helpbutton] token v helpbutton.toml)."},
                            status_code=403,
                        )
                    return RedirectResponse(f"/login?next={path}", status_code=303)
                return JSONResponse(
                    {
                        "error": "chybí klíč HelpButton (Authorization: Bearer …)",
                        "say": "Sem se dá jen s klíčem HelpButton.",
                    },
                    status_code=401,
                )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/login")
    async def login_page() -> FileResponse:
        return FileResponse(STATIC / "login.html")

    @app.post("/api/login")
    async def login(body: Login, request: Request) -> Response:
        """Prohlížeč pošle klíč (odvodí ho z tokenu sám, token po síti nejde) a dostane cookie."""
        ip = getattr(request.client, "host", "?")
        if not cfg.token:
            return JSONResponse({"ok": True, "open": True})
        if lockout.blocked(ip):
            return JSONResponse(
                {"error": "příliš mnoho špatných pokusů, zkuste to za 10 minut"}, status_code=429
            )
        if not _key_ok(body.key.strip()):
            lockout.fail(ip)
            log.warning("Špatné přihlášení z %s", ip)
            return JSONResponse({"error": "špatný kód"}, status_code=401)
        r = JSONResponse({"ok": True})
        r.set_cookie(
            COOKIE, api_key(cfg.token), max_age=365 * 24 * 3600, httponly=True, samesite="strict", path="/"
        )
        log.info("Přihlášen prohlížeč z %s", ip)
        return r

    @app.get("/")
    async def family_page() -> FileResponse:
        return FileResponse(STATIC / "family.html")

    @app.get("/button")
    async def button_page() -> FileResponse:
        return FileResponse(STATIC / "button.html")

    @app.get("/chat")
    async def chat_page() -> FileResponse:
        return FileResponse(STATIC / "chat.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/ping")
    async def ping() -> dict[str, Any]:
        """Běží server? (desktopová aplikace a run.py; nic neprozradí)"""
        return {"ok": True, "helpbutton": True, "jirik": True}

    @app.get("/api/hello")
    async def hello(nonce: str = "") -> dict[str, Any]:
        """Server se prokáže tlačítku: podepíše náhodné číslo klienta párovacím tokenem.

        Tlačítko (Raspberry Pi) hledá server přes mDNS – ohlásit se „jako HelpButton“ může v síti kdokoli. Bez
        tohoto ověření by podvržený server dostal klíč API, zvuk z mikrofonu a mohl by seniorovi z tlačítka
        cokoli „říct“. Token po síti nejde, podpis platí jen pro jedno číslo (nejde přehrát jinde)."""
        if not cfg.token:
            return {"helpbutton": True, "auth": "none"}
        if not (16 <= len(nonce) <= 128 and all(c in "0123456789abcdef" for c in nonce)):
            raise HTTPException(400, "nonce musí být 16–128 hexadecimálních znaků")
        return {
            "helpbutton": True,
            "auth": AUTH_SCHEME,
            "proof": auth_proof(cfg.token, SERVER_PROOF_ROLE, nonce, ""),
        }

    @app.get("/api/state")
    async def state() -> dict[str, Any]:
        return {
            "state": orch.state.value,
            "reply": orch.current.to_dict(),
            "devices": registry.public(),
            "events": family.recent(100),
            "stats": family.stats(),
            "senior": cfg.senior_name,
            "family": cfg.family.name,
            "family_dative": cfg.family.to_whom,
            "voice": {"stt": voice.stt_available, "tts": voice.tts_available},
            "llm": await brain.provider(),
            "vlm": orch.vlm.name if orch.vlm else None,
        }

    @app.post("/api/press")
    async def press(p: Press | None = None) -> dict[str, Any]:
        return (await orch.press((p or Press()).source)).to_dict()

    @app.post("/api/stop")
    async def stop() -> dict[str, Any]:
        return (await orch.stop("web")).to_dict()

    @app.post("/api/utterance")
    async def utterance(u: Utterance) -> dict[str, Any]:
        return (await orch.handle(u.text, u.source)).to_dict()

    @app.post("/api/audio")
    async def audio(request: Request, rate: int = 16000, source: str = "web-audio") -> dict[str, Any]:
        """Push-to-talk z prohlížeče (a druhý pokus o přepis z tlačítka na Pi): audio -> Whisper -> orchestrátor."""
        text = await _transcribe(request, rate)
        await hub.send({"type": "transcript", "text": text})
        return (
            await orch.handle(text, source if source in ("web-audio", "button") else "web-audio")
        ).to_dict()

    @app.post("/api/button/audio")
    async def button_audio(request: Request, rate: int = 16000) -> Response:
        """Fyzické tlačítko (ESP32): surové PCM 16 bit mono -> odpověď jako WAV.

        Stav pro LED a bzučák je v hlavičkách X-HelpButton-*.
        """
        try:
            text = await _transcribe(request, rate)
        except HTTPException as e:
            if e.status_code in (400, 413):
                raise
            text = ""
        await hub.send({"type": "transcript", "text": text})
        reply = await orch.handle(text, "button")
        headers = {
            "X-HelpButton-Led": reply.led,
            "X-HelpButton-Tone": reply.tone,
            "X-HelpButton-Screen": reply.screen,
            "X-HelpButton-Expects": "1" if reply.expects_answer else "0",
            "X-HelpButton-Say": quote(reply.say),
        }
        wav = await voice.synthesize(reply.say)
        if wav:
            return Response(wav, media_type="audio/wav", headers=headers)
        return Response(status_code=204, headers=headers)

    @app.post("/api/button/press")
    async def button_press() -> Response:
        reply = await orch.press("button")
        wav = await voice.synthesize(reply.say)
        headers = {"X-HelpButton-Led": reply.led, "X-HelpButton-Tone": reply.tone}
        return (
            Response(wav, media_type="audio/wav", headers=headers)
            if wav
            else Response(status_code=204, headers=headers)
        )

    @app.get("/api/tts")
    async def tts(text: str) -> Response:
        wav = await voice.synthesize(text[:500])
        if not wav:
            raise HTTPException(404, "TTS není k dispozici")
        return Response(wav, media_type="audio/wav")

    async def _read_audio(request: Request) -> bytes:
        """Tělo požadavku s limitem (zvládne i chunked upload z ESP32). Velikost se hlídá průběžně –
        obří nebo nekonečné nahrávání nevyčerpá paměť serveru."""
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_AUDIO_BYTES:
            raise HTTPException(413, "nahrávka je příliš dlouhá")
        buf = bytearray()
        async for chunk in request.stream():
            buf.extend(chunk)
            if len(buf) > MAX_AUDIO_BYTES:
                raise HTTPException(413, "nahrávka je příliš dlouhá")
        return bytes(buf)

    async def _transcribe(request: Request, rate: int) -> str:
        if not voice.stt_available:
            raise HTTPException(501, "Faster-Whisper není nainstalovaný – použijte textový vstup")
        if not 4000 <= rate <= 96000:
            raise HTTPException(400, "nepodporovaná vzorkovací frekvence")
        data = await _read_audio(request)
        if len(data) < 1600:
            return ""
        return await voice.transcribe(data, request.headers.get("content-type", "audio/wav"), rate)

    # --- Jiřík: chat ------------------------------------------------------------------------------
    def chat_snapshot(status: dict[str, Any], minfo: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "snapshot",
            "settings": settings.value.to_dict(),
            "status": status,
            "voice": voice.available,
            "spaces": {
                k: {"messages": sp.messages, "current": sp._current, "context": sp.context_info()}
                for k, sp in spaces.items()
            },
            "models": minfo,
            "devices": chat_devices(),
            "nodes": link.public(),
            "pairing": link.pair_public(),
            "user_name": cfg.greeting or cfg.user_name,
        }

    @app.get("/api/chat/state")
    async def chat_state() -> dict[str, Any]:
        return chat_snapshot(await chat.status(), await models_info())

    @app.post("/api/chat")
    async def chat_send(body: ChatIn) -> dict[str, Any]:
        if sum(len(i) for i in body.images) > CHAT_MAX_IMAGE:
            raise HTTPException(413, "obrázky jsou moc velké")
        images = [i.split(",", 1)[1] if i.startswith("data:") else i for i in body.images]
        return {"id": await space(body.space).send(body.text, images, body.target)}

    @app.post("/api/chat/stop")
    async def chat_stop(body: SpaceIn | None = None) -> dict[str, Any]:
        if body and body.space:
            await space(body.space).stop()
        else:
            for sp in spaces.values():
                await sp.stop()
        return {"ok": True}

    @app.post("/api/hush")
    async def hush() -> dict[str, Any]:
        speaker.hush()
        return {"ok": True}

    @app.post("/api/clear")
    async def clear(body: SpaceIn | None = None) -> dict[str, Any]:
        await space(body.space if body else "chat").clear()
        return {"ok": True}

    @app.post("/api/settings")
    async def set_settings(body: SettingsIn) -> dict[str, Any]:
        before_model, *_ = await chat.pick()
        before_ctx = ccfg.num_ctx
        changes = {k: v for k, v in body.model_dump().items() if v is not None}
        if "level" in changes and changes["level"] not in LEVELS:
            raise HTTPException(400, "neznámá úroveň")
        if "num_ctx" in changes:
            raw_ctx = changes["num_ctx"]
            if isinstance(raw_ctx, bool) or not (MIN_CTX <= int(raw_ctx) <= MAX_CTX):
                raise HTTPException(400, f"velikost kontextu musí být {MIN_CTX} až {MAX_CTX}")
            changes["num_ctx"] = int(raw_ctx)
        new = settings.update(changes)
        speaker.enabled = new.voice
        if not new.voice:
            speaker.hush()
        after_model, *_ = await chat.pick()
        ccfg.ollama_vlm_model = after_model
        if new.num_ctx != before_ctx:
            ccfg.num_ctx = new.num_ctx
            ccfg.ollama_vlm_num_ctx = new.num_ctx
            for sp in spaces.values():
                await sp.fit_after_resize()
        if after_model != before_model or (new.num_ctx != before_ctx and not chat.busy):
            unload = before_model if after_model != before_model else ""
            asyncio.create_task(chat.warm(unload=unload))
        await chat_hub.send(
            {
                "type": "settings",
                "settings": new.to_dict(),
                "status": await chat.status(),
                "models": await models_info(),
                "contexts": {k: sp.context_info() for k, sp in spaces.items()},
            }
        )
        return new.to_dict()

    @app.post("/api/context/compact")
    async def compact_context(body: SpaceIn | None = None) -> dict[str, Any]:
        return await space(body.space if body else "chat").compact_context(force=True)

    # --- Jiřík: modely (úrovně myšlení) -------------------------------------------------------------
    @app.get("/api/models")
    async def get_models() -> dict[str, Any]:
        return await models_info()

    @app.post("/api/models/pull")
    async def pull(body: LevelIn) -> dict[str, Any]:
        lv = LEVELS.get(body.level)
        if not lv:
            raise HTTPException(400, "neznámá úroveň")
        uncensored = settings.value.uncensored if body.uncensored is None else body.uncensored
        model = lv.uncensored if uncensored else lv.model
        need = lv.size_gb + 0.5
        if hardware.probe()["disk_free_gb"] < need:
            return {"ok": False, "error": f"Na disku chybí místo – potřeba je asi {need:.0f} GB."}
        await models.pull(model)
        return {"ok": True, "model": model}

    @app.post("/api/models/cancel")
    async def cancel(body: ModelIn) -> dict[str, Any]:
        await models.cancel(body.model)
        return {"ok": True}

    @app.post("/api/models/delete")
    async def delete(body: ModelIn) -> dict[str, Any]:
        active, *_ = await chat.pick()
        if body.model == active:
            return {"ok": False, "error": "Tento model se právě používá – přepněte nejdřív na jinou úroveň."}
        return {"ok": await models.delete(body.model)}

    # --- Jiřík Link: další počítače v síti ---------------------------------------------------------
    @app.post("/api/link/pair/open")
    async def pair_open() -> dict[str, Any]:
        try:
            p = await link.open_pairing()
        except RuntimeError as e:
            return {"ok": False, "error": str(e)}
        firewall, net_pub = await asyncio.gather(
            asyncio.to_thread(firewall_rule_present, ccfg),
            asyncio.to_thread(network_is_public),
        )
        if not firewall:
            open_firewall(ccfg)
        return {
            "ok": True,
            "pairing": p,
            "firewall_prompt": not firewall,
            "firewall_ok": firewall,
            "network_public": net_pub,
        }

    @app.post("/api/link/firewall")
    async def link_firewall(body: FirewallIn | None = None) -> dict[str, Any]:
        open_firewall(ccfg, switch_private=bool(body and body.switch_private))
        return {"ok": True}

    @app.post("/api/link/pair/confirm")
    async def pair_confirm(body: ConfirmIn) -> dict[str, Any]:
        try:
            return await link.confirm_pairing(body.ok, body.name)
        except LookupError as e:
            return {"ok": False, "error": str(e)}

    @app.post("/api/link/pair/close")
    async def pair_close() -> dict[str, Any]:
        await link.close_pairing()
        link.pairing = None
        await chat_hub.send({"type": "pairing", "pairing": None})
        return {"ok": True}

    @app.post("/api/link/remove/{nid}")
    async def unpair(nid: str) -> dict[str, Any]:
        return {"ok": await link.remove(nid)}

    @app.post("/api/link/rename/{nid}")
    async def rename(nid: str, body: PairIn) -> dict[str, Any]:
        return {"ok": await link.rename(nid, body.name)}

    @app.post("/api/link/wake/{nid}")
    async def wake(nid: str) -> dict[str, Any]:
        try:
            await link.wake(nid, timeout=30)
            return {"ok": True}
        except (LookupError, TimeoutError) as e:
            return {"ok": False, "error": str(e)}

    @app.get("/api/devices/{did}/screen")
    async def device_screen(did: str) -> dict[str, Any]:
        """Náhled obrazovky zařízení pro záložku Zařízení."""
        from .chat import _thumb

        if did == "local":
            dev = chat._local()
        else:
            try:
                dev = await link.wake(did, timeout=30)
            except (LookupError, TimeoutError) as e:
                return {"ok": False, "error": str(e)}
        if not dev or not dev.available or "desktop_view" not in dev.tools:
            return {"ok": False, "error": "Zařízení teď není připojené."}
        try:
            view = await registry.call(dev, "desktop_view", {"marks": False}, timeout=45)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}
        if not view.get("image_webp_base64"):
            return {
                "ok": False,
                "error": view.get("error") or "Snímek se nepodařilo pořídit.",
                "locked": bool(view.get("locked")),
            }
        return {
            "ok": True,
            "image": _thumb(view["image_webp_base64"], 960),
            "window": view.get("active_window", ""),
            "ts": time.time(),
        }

    @app.websocket("/ws/chat")
    async def ws_chat(ws: WebSocket) -> None:
        if not _request_ok(ws.headers) or (
            _needs_key(ws.client, "/ws/chat") and not _key_ok(_presented(ws.headers, ws.cookies))
        ):
            await ws.close(code=1008)
            return
        await ws.accept()
        try:
            status, minfo = await chat.status(), await models_info()
            await chat_hub.join(ws, lambda: chat_snapshot(status, minfo))
            while True:
                await ws.receive_text()
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            chat_hub.clients.pop(ws, None)

    @app.websocket("/ws/ui")
    async def ws_ui(ws: WebSocket) -> None:
        if not _request_ok(ws.headers):
            await ws.close(code=1008)
            return
        if _needs_key(ws.client, "/ws/ui") and not _key_ok(_presented(ws.headers, ws.cookies)):
            await ws.close(code=1008)
            return
        await ws.accept()
        hub.clients.add(ws)
        try:
            await ws.send_text(dumps({"type": "snapshot", **(await state())}))
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            hub.clients.discard(ws)

    @app.websocket("/ws/device")
    async def ws_device(ws: WebSocket) -> None:
        if ws.headers.get("origin") is not None or not host_allowed(
            ws.headers.get("host", ""), cfg.allowed_hosts
        ):
            await ws.close(code=1008)
            return
        await ws.accept()
        try:
            hello = json.loads(await asyncio.wait_for(ws.receive_text(), 10))
        except (asyncio.TimeoutError, ValueError, WebSocketDisconnect):
            await ws.close()
            return
        if (
            not isinstance(hello, dict)
            or hello.get("type") != "hello"
            or not hello.get("device_id")
            or str(hello["device_id"]).startswith("link-")  # spárované počítače chodí jen přes Jiřík Link (TLS)
        ):
            await ws.close()
            return
        hello["local"] = is_local(ws.client)  # „Tento počítač“ pro Jiříka – zařízení si to samo neurčí
        if cfg.token and not await _device_authenticated(ws, hello):
            log.warning("Odmítnuto zařízení %s: neprokázalo se párovacím tokenem", hello.get("device_id"))
            try:
                await ws.send_text(dumps({"type": "error", "error": "špatný párovací token"}))
                await ws.close()
            except (RuntimeError, WebSocketDisconnect):
                pass
            return
        send_lock = asyncio.Lock()

        async def send(msg: dict[str, Any]) -> None:
            async with send_lock:
                await ws.send_text(dumps(msg))

        dev = await registry.attach(hello, send)
        try:
            while True:
                msg = json.loads(await ws.receive_text())
                t = msg.get("type")
                if t == "result":
                    registry.on_result(msg)
                elif t == "status":
                    await registry.update_status(dev.device_id, msg.get("status") or {})
        except (WebSocketDisconnect, ValueError, RuntimeError):
            pass
        finally:
            await registry.detach(dev.device_id, send)

    async def _device_authenticated(ws: WebSocket, hello: dict[str, Any]) -> bool:
        nd = hello.get("nonce")
        if hello.get("auth") == AUTH_SCHEME and isinstance(nd, str) and 8 <= len(nd) <= 128:
            ns = new_nonce()
            await ws.send_text(
                dumps({"type": "challenge", "nonce": ns, "proof": auth_proof(cfg.token, "server", nd, ns)})
            )
            try:
                msg = json.loads(await asyncio.wait_for(ws.receive_text(), 10))
            except (asyncio.TimeoutError, ValueError, WebSocketDisconnect):
                return False
            return (
                isinstance(msg, dict)
                and msg.get("type") == "auth"
                and proof_ok(cfg.token, "device", ns, nd, msg.get("proof"))
            )
        if hmac.compare_digest(str(hello.get("token") or "").encode(), cfg.token.encode()):
            log.warning(
                "Zařízení %s posílá token v čitelné podobě – aktualizujte démona", hello.get("device_id")
            )
            return True
        return False

    @app.exception_handler(Exception)
    async def on_error(request: Request, exc: Exception) -> JSONResponse:
        log.exception("Chyba při %s", request.url.path)
        return JSONResponse(
            {"say": "Něco se pokazilo. Zkuste to prosím znovu.", "error": str(exc)}, status_code=500
        )

    return app
