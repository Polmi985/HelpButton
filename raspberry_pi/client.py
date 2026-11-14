"""Hlavní orchestrátor hlasového tlačítka pro Raspberry Pi 5 (HelpButton LAN klient).

Propojuje fyzické tlačítko (GPIO / USB / klávesnice), mikrofon HATu Audio Injector Zero,
lokální Faster-Whisper STT (+ průběžný Rolling Micro-STT a překlad EN/SK -> CS),
lokální Piper / Edge-TTS (s větným streamováním a Senior Clarity DSP)
a výstup HATu se serverem HelpButton přes LAN (mDNS), přímou IP nebo lokální failover.

Spolehlivost:
  * Server se hledá znovu (mDNS) pokaždé, když vypadne nebo změní IP – i když při startu Pi ještě neběžel.
  * Krátký timeout na připojení (4 s), ale dlouhé čekání na odpověď: `/api/utterance` na serveru čeká,
    až GUI agent akci dokončí (i několik minut). Při pomalé odpovědi se příkaz NIKDY neposílá
    podruhé lokálnímu záložnímu mozku (hrozilo by dvojí provedení akce).
  * Lokální záložní mozek (Edge Failover) se použije jen, když se k serveru vůbec nelze připojit.
  * Každá chyba (mikrofon, server, STT) je seniorovi řečena nahlas – nikdy jen tichá červená LED.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hmac
import inspect
import json
import logging
import secrets
import threading
import time
from enum import Enum
from typing import Callable, Iterable
from urllib.parse import unquote

import httpx

from .audio_io import (
    PAUSE_PENDING,
    AudioPlayer,
    MicrophoneStream,
    PauseCheckResult,
    RecordedUtterance,
    apply_mic_speech_cleanup_pcm16,
    pcm_to_wav,
    process_vad_stream,
)
from .button_hw import ButtonController
from .config import RpiConfig, auth_headers, server_proof
from .stt import (
    SpeechToText,
    classify_utterance_completeness,
    is_repeat_request,
    translate_cs_to_en,
)
from .tts import TextToSpeech, split_into_clauses

log = logging.getLogger("helpbutton.rpi.client")

MDNS_SERVICES = ["_helpbutton._tcp.local.", "_helpbutton-server._tcp.local."]

HTTP_TIMEOUT = httpx.Timeout(connect=4.0, read=600.0, write=30.0, pool=10.0)
SHORT_TIMEOUT = httpx.Timeout(connect=3.0, read=15.0, write=10.0, pool=5.0)
SERVER_RECHECK_S = 15.0
MAX_SEEN_REPLIES = 256

MSG_SERVER_DOWN = "Se serverem HelpButton se teď nemohu spojit. Zkuste to prosím za chvíli."
MSG_SERVER_REJECTED = (
    "Server HelpButton moje spojení odmítl. Je potřeba upravit nastavení, dejte prosím vědět rodině."
)
MSG_MIC_BROKEN = "Nefunguje mi mikrofon. Dejte prosím vědět rodině, ať zkontroluje zvukovou kartu."
MSG_GENERIC_ERROR = "Omlouvám se, něco se pokazilo. Zkuste prosím zmáčknout tlačítko znovu."
MSG_STOPPED = "Zastavila jsem. Nic dalšího nedělám."


class ClientState(str, Enum):
    IDLE = "idle"
    PROMPTING = "prompting"
    LISTENING = "listening"
    AUTO_LISTENING = "auto_listening"
    PROCESSING = "processing"
    SPEAKING = "speaking"


class ServerRejectedError(RuntimeError):
    """Server běží, ale požadavek odmítl (HTTP 403 – ochrana Host/Origin na serveru)."""


def server_url_from_addresses(addrs: list[str], port: int | None) -> str | None:
    """Sestaví URL serveru z mDNS adres: přednost má IPv4, IPv6 se uzavře do [] (jinak neplatná URL).

    Server přijímá jen Host z domácí sítě (IP adresa, jméno bez tečky, *.local), IP adresa vždy projde.
    """
    if not addrs or not port:
        return None
    v4 = [a for a in addrs if ":" not in a]
    if v4:
        return f"http://{v4[0]}:{port}"
    v6 = [a for a in addrs if not a.lower().startswith("fe80")] or addrs
    return f"http://[{v6[0]}]:{port}"


def raise_if_rejected(r: httpx.Response, base_url: str) -> None:
    """Rozliší odmítnutí serverem (403/401/429) od výpadku – na odmítnutí nemá smysl přepínat na lokální failover."""
    if r.status_code in (401, 429):
        raise ServerRejectedError(
            f"Server {base_url} chce párovací token (HTTP {r.status_code}). Dejte stejný token jako v helpbutton.toml "
            'serveru do rpi_button.toml ([server] token = "...") nebo do proměnné HELPBUTTON_TOKEN.'
        )
    if r.status_code == 403:
        raise ServerRejectedError(
            f"Server {base_url} odmítl požadavek (403 – {r.text[:120]}). Použijte IP adresu nebo jméno *.local, "
            "případně přidejte jméno serveru do [helpbutton] allowed_hosts v helpbutton.toml."
        )


def _is_unreachable(exc: BaseException) -> bool:
    """Jen chyby PŘIPOJENÍ smí vést na lokální záložní mozek (požadavek se k serveru vůbec nedostal).

    Timeout čtení / chyba 500 znamená, že server příkaz možná už provádí – poslat ho znovu jinam
    by mohlo akci provést dvakrát.
    """
    return isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.UnsupportedProtocol))


def _ws_connect(websockets, url: str, headers: dict[str, str]):  # noqa: ANN001, ANN202
    """websockets ≥ 14 má additional_headers, starší extra_headers."""
    kw = {"open_timeout": 5, "max_size": 8 * 2**20}
    if not headers:
        return websockets.connect(url, **kw)
    try:
        return websockets.connect(url, additional_headers=headers, **kw)
    except TypeError:
        return websockets.connect(url, extra_headers=headers, **kw)


async def verify_server(base_url: str, token: str, http: httpx.AsyncClient | None = None) -> bool:
    """Prokáže se server párovacím tokenem? (podepíše náhodné číslo – token ani klíč po síti nejdou)

    True = pravý server. False = odpověděl, ale podpis nesedí (podvržený server, jiný token, stará verze).
    Chyba spojení se nechá probublat – volající ji bere jako „server nedostupný“."""
    nonce = secrets.token_hex(16)
    if http is not None:
        r = await http.get("/api/hello", params={"nonce": nonce}, timeout=SHORT_TIMEOUT)
    else:
        async with httpx.AsyncClient(base_url=base_url, timeout=SHORT_TIMEOUT, trust_env=False) as c:
            r = await c.get("/api/hello", params={"nonce": nonce})
    if r.status_code != 200:
        return False
    try:
        proof = r.json().get("proof")
    except (ValueError, AttributeError):
        return False
    return isinstance(proof, str) and hmac.compare_digest(proof, server_proof(token, nonce))


async def discover_helpbutton_server(timeout: float = 4.0) -> str | None:
    """Najde HelpButton server v LAN přes mDNS (`_helpbutton._tcp.local.` nebo `_helpbutton-server._tcp.local.`)."""
    urls = await discover_helpbutton_servers(timeout)
    return urls[0] if urls else None


async def discover_helpbutton_servers(timeout: float = 4.0, collect_s: float = 0.8) -> list[str]:
    """Všechny servery ohlášené přes mDNS (po prvním nalezeném ještě chvíli čeká na další).

    Ohlásit se jako „HelpButton“ může v síti kdokoli – volající si z kandidátů vybere ten, který se prokáže tokenem.
    """
    try:
        from zeroconf import ServiceStateChange
        from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf
    except ImportError:
        return []

    found: asyncio.Queue[str] = asyncio.Queue()
    pending: set[asyncio.Task] = set()
    try:
        azc = AsyncZeroconf()
    except Exception as e:
        log.debug("mDNS nelze spustit: %s", e)
        return []

    async def _resolve(zeroconf, service_type, name):  # noqa: ANN001
        try:
            info = AsyncServiceInfo(service_type, name)
            if await info.async_request(zeroconf, 2500):
                url = server_url_from_addresses(info.parsed_addresses(), info.port)
                if url:
                    await found.put(url)
        except Exception as e:
            log.debug("mDNS: nelze přečíst %s: %s", name, e)

    def on_change(zeroconf, service_type, name, state_change):  # noqa: ANN001
        if state_change is ServiceStateChange.Added:
            task = asyncio.ensure_future(_resolve(zeroconf, service_type, name))
            pending.add(task)
            task.add_done_callback(pending.discard)

    browser = None
    urls: list[str] = []
    try:
        browser = AsyncServiceBrowser(azc.zeroconf, MDNS_SERVICES, handlers=[on_change])
        deadline = asyncio.get_running_loop().time() + timeout
        first_at: float | None = None
        while True:
            now = asyncio.get_running_loop().time()
            end = deadline if first_at is None else min(deadline, first_at + collect_s)
            if now >= end:
                break
            try:
                url = await asyncio.wait_for(found.get(), timeout=end - now)
            except asyncio.TimeoutError:
                break
            if url not in urls:
                urls.append(url)
            if first_at is None:
                first_at = asyncio.get_running_loop().time()
        return urls
    except OSError as e:
        log.debug("mDNS hledání selhalo: %s", e)
        return urls
    finally:
        for task in list(pending):
            task.cancel()
        try:
            if browser is not None:
                await browser.async_cancel()
            await azc.async_close()
        except Exception:
            pass


class RpiVoiceButtonClient:
    """Kompletní stavový automat "Grandpa-Proof++" hlasového tlačítka na Raspberry Pi 5."""

    def __init__(
        self,
        cfg: RpiConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
        stt: SpeechToText | None = None,
        tts: TextToSpeech | None = None,
        player: AudioPlayer | None = None,
        frame_source: Callable[[Callable[[], bool]], Iterable[bytes]] | None = None,
    ):
        self.cfg = cfg
        self.base_url: str = cfg.server_url if cfg.server_url != "auto" else "http://localhost:8765"
        self._external_http = http_client
        self._shared_http: httpx.AsyncClient | None = None
        self._fallback_http: httpx.AsyncClient | None = None
        self._fallback_failed = False
        self._server_ok = True
        self._last_error_kind = ""
        self._server_stt = False
        self._verified_url: str | None = None
        self.player = player or AudioPlayer(cfg)
        self.stt = stt or SpeechToText(cfg)
        self.tts = tts or TextToSpeech(
            cfg, server_url_getter=lambda: self.base_url if self._trusted() else None
        )
        self._mic = MicrophoneStream(cfg)
        self._frame_source = frame_source or (lambda stop_chk: self._mic.iter_frames(stop_chk))

        self.button = ButtonController(cfg, on_press=self._on_button_pressed)
        self.state: ClientState = ClientState.IDLE
        self.senior_name: str = "Paní Jano"
        self.spoken_history: list[str] = []

        self._last_reply_say: str = (
            "HelpButton je připravená. Zmáčkněte tlačítko a řekněte, s čím potřebujete pomoci."
        )
        self._last_reply_expects_answer: bool = False
        self._last_full_reply: dict | None = None

        self._session_task: asyncio.Task | None = None
        self._ws_task: asyncio.Task | None = None
        self._watch_task: asyncio.Task | None = None
        self._background: set[asyncio.Task] = set()
        self._press_started_at: float = 0.0
        self._cancel_flag: bool = False
        self._stopping: bool = False
        self._skip_prompt: bool = False
        self._ws_interrupt: bool = False
        self._finish_listening_early: bool = False
        self._followup_button_pressed: bool = False
        self._seen_reply_ts: set[float] = set()
        self._say_lock = asyncio.Lock()
        self._closed = False
        self._pause_job: concurrent.futures.Future | None = None
        self._pause_job_cancel: threading.Event | None = None
        self._pause_result = None
        self._stt_cancel_param: bool | None = None

    def _get_http_client(self) -> httpx.AsyncClient:
        if self._external_http is not None:
            return self._external_http
        if self._shared_http is None or self._shared_http.is_closed:
            self._shared_http = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=HTTP_TIMEOUT,
                trust_env=False,
                headers=auth_headers(self.cfg.token),
            )
        return self._shared_http

    def _set_base_url(self, url: str) -> None:
        url = url.rstrip("/")
        if url == self.base_url:
            return
        log.info("Adresa serveru HelpButton: %s -> %s", self.base_url, url)
        self.base_url = url
        self._verified_url = None
        old, self._shared_http = self._shared_http, None
        if old is not None and not old.is_closed:
            self._spawn(old.aclose())

    def _trusted(self) -> bool:
        return not self.cfg.token or self._verified_url == self.base_url

    async def _ensure_verified(self) -> None:
        """Před prvním požadavkem s klíčem: prokáže se server tokenem? (ochrana proti podvrženému serveru)"""
        if self._trusted():
            return
        if await verify_server(self.base_url, self.cfg.token, self._external_http):
            self._verified_url = self.base_url
            log.info("Server %s se prokázal párovacím tokenem.", self.base_url)
            return
        raise ServerRejectedError(
            f"Server {self.base_url} se neprokázal párovacím tokenem – může to být podvržený server v síti, "
            "nebo má jiný token než [server] token v rpi_button.toml. Nic mu neposílám."
        )

    async def _pick_discovered(self, timeout: float) -> str | None:
        """Z mDNS kandidátů vybere ten, který se prokáže tokenem (bez tokenu první nalezený)."""
        try:
            urls = await discover_helpbutton_servers(timeout=timeout)
        except Exception as e:
            log.debug("mDNS hledání selhalo: %s", e)
            return None
        for url in urls:
            if not self.cfg.token:
                return url
            try:
                if await verify_server(url, self.cfg.token):
                    return url
                log.warning("mDNS: %s se ohlašuje jako HelpButton, ale neprokázal se tokenem – ignoruji ho.", url)
            except Exception as e:
                log.debug("mDNS kandidát %s neodpovídá: %s", url, e)
        return None

    def _use_discovered(self, url: str) -> None:
        self._set_base_url(url)
        if self.cfg.token:
            self._verified_url = url

    def _spawn(self, coro) -> asyncio.Task:  # noqa: ANN001
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    def _get_local_fallback_client(self) -> httpx.AsyncClient | None:
        """Pokud není hlavní PC server dostupný, vytvoří lokální in-process HelpButton server přímo na Pi 5."""
        if not self.cfg.standalone_fallback or self._external_http is not None or self._fallback_failed:
            return None
        if self._fallback_http is None:
            try:
                from helpbutton.server.app import create_app
                from helpbutton.server.config import load_config

                srv_cfg = load_config()
                srv_cfg.mdns = False
                srv_cfg.adb_watchdog = False
                app = create_app(srv_cfg)
                transport = httpx.ASGITransport(app=app)
                self._fallback_http = httpx.AsyncClient(
                    transport=transport, base_url="http://helpbutton-local-fallback", timeout=httpx.Timeout(120.0)
                )
                log.info("Aktivován záložní lokální HelpButton server přímo na Raspberry Pi 5 (Edge Failover).")
            except Exception as e:
                self._fallback_failed = True
                log.warning(
                    "Lokální záložní server nelze spustit (%s) – bez spojení se serverem nebude HelpButton odpovídat.",
                    e,
                )
                return None
        return self._fallback_http

    async def _probe_server(self, *, quiet_rejected: bool = False) -> dict | None:
        """Zkontroluje jen HLAVNÍ server (bez záložního mozku). Vrací /api/state nebo None."""
        try:
            await self._ensure_verified()
            r = await self._get_http_client().get("/api/state", timeout=SHORT_TIMEOUT)
            raise_if_rejected(r, self.base_url)
            r.raise_for_status()
            st = r.json()
            self._server_ok = True
            self._last_error_kind = ""
            return st if isinstance(st, dict) else {}
        except ServerRejectedError as e:
            if not quiet_rejected:
                log.error("%s", e)
            self._server_ok = False
            self._last_error_kind = "rejected"
        except Exception as e:
            log.debug("Server %s neodpovídá: %s", self.base_url, e)
            self._server_ok = False
        return None

    async def resolve_server(self) -> str:
        """Najde adresu serveru (přes mDNS nebo přímou URL) a načte oslovení seniora."""
        if self.cfg.server_url == "auto" and self._external_http is None:
            log.info("Hledám HelpButton server v síti přes mDNS…")
            url = await self._pick_discovered(3.5)
            if url:
                self._use_discovered(url)
                log.info("Nalezen HelpButton server přes mDNS: %s", self.base_url)
            else:
                log.info("mDNS server nenalezen, zkouším %s (a budu hledat dál na pozadí)", self.base_url)
        st = await self._probe_server()
        if st is not None:
            if st.get("senior"):
                self.senior_name = str(st["senior"])
            self._update_stt_context(st)
            log.info(
                "Připojeno k HelpButton serveru (%s) · senior: %s · porozumění: %s",
                self.base_url,
                self.senior_name,
                st.get("llm", "?"),
            )
        elif self._last_error_kind == "rejected":
            log.warning(
                "Server %s odmítl spojení bez platného tokenu (doplňte `[server] token` v rpi_button.toml nebo `--token`).%s",
                self.base_url,
                " Do spárování odpovídá lokální záložní mozek." if self.cfg.standalone_fallback else "",
            )
        else:
            log.warning(
                "Server %s zatím neodpovídá – hledám ho dál na pozadí%s.",
                self.base_url,
                ", mezitím odpovídá lokální záložní mozek" if self.cfg.standalone_fallback else "",
            )
            if self.cfg.server_url == "auto":
                log.info(
                    "Tip: Na Wi-Fi s oddělenými podsítěmi (mDNS neprojde) spusťte: bash raspberry_pi/connect_pi.sh <IP_PC> <TOKEN>"
                )
        return self.base_url

    async def _server_watch_loop(self) -> None:
        """Když server vypadne (nebo při startu ještě neběžel), hledá ho znovu (s pomalejším opakováním při chybě tokenu)."""
        while not self._closed:
            delay = SERVER_RECHECK_S * 2 if self._last_error_kind == "rejected" else SERVER_RECHECK_S
            await asyncio.sleep(delay)
            if self._server_ok or self._external_http is not None:
                continue
            if self.state not in (ClientState.IDLE,):
                continue
            if self.cfg.server_url == "auto":
                url = await self._pick_discovered(3.0)
                if url:
                    self._use_discovered(url)
            st = await self._probe_server(quiet_rejected=self._last_error_kind == "rejected")
            if st is not None:
                if st.get("senior"):
                    self.senior_name = str(st["senior"])
                self._update_stt_context(st)
                log.info("Spojení se serverem HelpButton obnoveno (%s).", self.base_url)

    async def warmup(self) -> None:
        """Přednačte STT model souběžně s hledáním serveru a předgeneruje české věty do TTS cache."""

        async def _srv() -> None:
            try:
                await self.resolve_server()
            except Exception as e:
                log.warning("Hledání serveru selhalo: %s", e)

        async def _stt() -> None:
            try:
                await self.stt.warmup()
            except Exception as e:
                log.error("Načtení rozpoznávání řeči selhalo: %s", e)

        async def _tts() -> None:
            try:
                await self.tts.warmup(self.senior_name)
            except Exception as e:
                log.warning("Předgenerování řeči selhalo: %s", e)

        await asyncio.gather(_srv(), _stt(), _tts())

    async def aclose(self) -> None:
        if self._shared_http and not self._shared_http.is_closed:
            await self._shared_http.aclose()
        if self._fallback_http and not self._fallback_http.is_closed:
            await self._fallback_http.aclose()

    async def close(self) -> None:
        await self.aclose()

    async def shutdown(self) -> None:
        """Čisté ukončení: zastaví zvuk, úlohy, GPIO (LED zhasne) a vrátí terminál do normálu."""
        if self._closed:
            return
        self._closed = True
        self._cancel_flag = True
        try:
            self.player.stop()
        except Exception:
            pass
        tasks = [
            t
            for t in (self._ws_task, self._watch_task, self._session_task, *self._background)
            if t is not None and not t.done()
        ]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=2.0)
        self.button.stop()
        try:
            await self.aclose()
        except Exception:
            pass

    async def run_forever(self) -> None:
        """Spustí obsluhu GPIO/klávesnice, WebSocket posluchač a drží hlavní smyčku."""
        loop = asyncio.get_running_loop()
        self.button.start(loop)
        self.button.set_led("blink")
        if not self.button.has_input:
            log.error(
                "Není aktivní žádné ovládání: GPIO tlačítko nefunguje a program neběží v terminálu. "
                "Zkontrolujte zapojení na pin 11 + 9 a skupinu gpio (viz výše)."
            )
        try:
            await self.warmup()
            self._ws_task = asyncio.create_task(self._ws_ui_loop())
            self._watch_task = asyncio.create_task(self._server_watch_loop())
            if self.state == ClientState.IDLE:
                self.button.set_led("off")
            log.info(
                "=== Raspberry Pi 5 Hlasové Tlačítko připraveno (TTS: %s, STT: %s, Clarity DSP: %s) ===",
                self.tts.active_engine_name(),
                f"Faster-Whisper ({self.cfg.whisper_model})" if self.stt.local_available else "Server STT",
                "ZAPNUTO" if self.cfg.senior_clarity_dsp else "VYPNUTO",
            )
            log.info(
                "Stiskněte fyzické tlačítko (GPIO %d) nebo MEZERNÍK / ENTER a mluvte.", self.cfg.gpio_pin
            )
            while True:
                await asyncio.sleep(0.5)
        finally:
            await self.shutdown()

    def _session_active(self) -> bool:
        return self._session_task is not None and not self._session_task.done()

    def _on_button_pressed(self) -> None:
        """Zavolá se při stisku fyzického GPIO tlačítka nebo Mezerníku."""
        if self._closed:
            return
        now = time.monotonic()

        if self.state != ClientState.IDLE and (now - self._press_started_at) < self.cfg.press_grace_s:
            log.debug("Ignoruji zákmit / dvojklik tlačítka po %.0f ms", (now - self._press_started_at) * 1000)
            return

        if self._stopping:
            self._press_started_at = now
            self.player.stop()
            return

        if self.state == ClientState.AUTO_LISTENING:
            log.info("Tlačítko stisknuto během automatického poslechu odpovědi – pokračuji v nahrávání.")
            self._press_started_at = now
            self._followup_button_pressed = True
            self.state = ClientState.LISTENING
            return

        if self.state == ClientState.LISTENING:
            log.info("Tlačítko stisknuto během nahrávání – dokončuji nahrávku.")
            self._finish_listening_early = True
            return

        if self.state == ClientState.PROMPTING:
            log.info("Tlačítko stisknuto během pozdravu – přeskakuji pozdrav a hned poslouchám.")
            self._press_started_at = now
            self._skip_prompt = True
            self.player.stop()
            return

        if self.state in (ClientState.PROCESSING, ClientState.SPEAKING) and self._session_active():
            log.info("Tlačítko stisknuto během práce/řeči – OKAMŽITÉ ZASTAVENÍ!")
            self._press_started_at = now
            self._start_emergency_stop()
            return

        if self.state in (ClientState.PROCESSING, ClientState.SPEAKING):
            self._ws_interrupt = True
            self.player.stop()

        self._press_started_at = now
        self._cancel_flag = False
        self._finish_listening_early = False
        self._followup_button_pressed = False
        self._skip_prompt = False
        self._session_task = asyncio.create_task(self.run_interaction())

    def _start_emergency_stop(self) -> None:
        self._cancel_flag = True
        self._stopping = True
        self.player.stop()
        prev = self._session_task
        if prev is not None and not prev.done():
            prev.cancel()
        self._session_task = asyncio.create_task(self._emergency_stop(prev))

    async def _emergency_stop(self, prev: asyncio.Task | None = None) -> dict:
        """Okamžitě zastaví akci na serveru, přehraje dvojité pípnutí a řekne 'Zastavila jsem'."""
        self._stopping = True
        try:
            self.state = ClientState.PROCESSING
            self.player.stop()
            if prev is not None and not prev.done():
                await asyncio.wait({prev}, timeout=2.0)
            self._cancel_flag = False
            self.state = ClientState.PROCESSING
            try:
                reply = await self._post_json("/api/stop", {}, timeout=SHORT_TIMEOUT)
            except Exception as e:
                log.warning("Serveru se nepodařilo poslat stop (%s).", e)
                reply = {"say": MSG_STOPPED, "led": "off", "tone": "double", "ts": time.time()}
            self._remember_ts(reply)
            self._last_reply_expects_answer = False
            self.button.set_led("off")
            await asyncio.to_thread(self.player.play_tone, "double")
            await self.speak_text(reply.get("say") or MSG_STOPPED)
            return reply
        finally:
            self._stopping = False
            self.state = ClientState.IDLE

    def _remember_ts(self, reply: dict | None) -> None:
        if not reply:
            return
        try:
            ts = float(reply.get("ts") or 0.0)
        except (TypeError, ValueError):
            return
        if ts:
            if len(self._seen_reply_ts) >= MAX_SEEN_REPLIES:
                for old in sorted(self._seen_reply_ts)[: MAX_SEEN_REPLIES // 2]:
                    self._seen_reply_ts.discard(old)
            self._seen_reply_ts.add(ts)

    async def _say_error(self, message: str, *, led: str = "red", tone: str = "low") -> None:
        """Chybu vždy oznámí nahlas (LED sama senior nepochopí). Nesmí sama spadnout."""
        try:
            self.button.set_led(led)
            if tone:
                await asyncio.to_thread(self.player.play_tone, tone)
            await self.speak_text(message)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("Nelze oznámit chybu nahlas: %s", e)

    async def run_interaction(self) -> dict | None:
        """Provede kompletní konverzační výměnu od stisku tlačítka (včetně navazujících otázek ANO/NE)."""
        press_task: asyncio.Task[dict | None] | None = None
        external_stop: dict | None = None

        async def _finish_external_stop(stop_reply: dict) -> dict:
            self.button.set_led("off")
            await asyncio.to_thread(self.player.play_tone, "double")
            await self.speak_text(stop_reply["say"])
            self.state = ClientState.IDLE
            return stop_reply

        async def _settle_press_task() -> None:
            nonlocal press_task
            if press_task is None:
                return
            if not press_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(press_task), timeout=0.05)
                except (asyncio.TimeoutError, Exception):
                    press_task.cancel()
                    try:
                        await press_task
                    except (asyncio.CancelledError, Exception):
                        pass
            press_task = None

        try:
            self.state = ClientState.PROMPTING
            self.button.set_led("blue")

            if self.cfg.speak_listening_greeting:
                press_reply = await self._safe_press()
                self._remember_ts(press_reply)

                if (
                    press_reply
                    and press_reply.get("screen") == "klid"
                    and "Zastavila" in press_reply.get("say", "")
                ):
                    return await _finish_external_stop(press_reply)

                if self.cfg.listen_beep and not self._skip_prompt:
                    await asyncio.to_thread(self.player.play_tone, "beep")
                if not self.button.is_held() and not self._skip_prompt:
                    greeting = (press_reply or {}).get("say") or f"Poslouchám, {self.senior_name}."
                    await self.speak_text(greeting, skippable=True)
                    if self.cfg.echo_guard_s > 0:
                        await asyncio.sleep(self.cfg.echo_guard_s)
            else:
                press_task = asyncio.create_task(self._safe_press())

                def _on_press_done(task: asyncio.Task[dict | None]) -> None:
                    nonlocal external_stop
                    if task.cancelled():
                        return
                    try:
                        res = task.result()
                    except Exception:
                        return
                    self._remember_ts(res)
                    if res and res.get("screen") == "klid" and "Zastavila" in res.get("say", ""):
                        external_stop = res

                press_task.add_done_callback(_on_press_done)
                if self.cfg.listen_beep and not self._skip_prompt and not self.button.is_held():
                    await asyncio.to_thread(self.player.play_tone, "beep")
                if external_stop is not None:
                    return await _finish_external_stop(external_stop)

            self._skip_prompt = False

            followup_turn = 0
            is_followup = False
            last_reply: dict | None = self._last_full_reply

            while True:
                if self._cancel_flag:
                    return last_reply

                self.state = ClientState.AUTO_LISTENING if is_followup else ClientState.LISTENING
                self.button.set_led("blue")
                self._finish_listening_early = False
                self._followup_button_pressed = False

                try:
                    utterance = await self._record_voice(
                        is_followup=is_followup,
                        extra_cancel=(lambda: external_stop is not None) if not is_followup else None,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.error("Mikrofon selhal: %s", e)
                    self.state = ClientState.PROCESSING
                    await self._say_error(MSG_MIC_BROKEN)
                    self.state = ClientState.IDLE
                    return None
                if external_stop is not None:
                    return await _finish_external_stop(external_stop)
                if self._cancel_flag or utterance.canceled:
                    self.button.set_led("off")
                    self.state = ClientState.IDLE
                    return last_reply

                if not utterance.has_speech:
                    self.button.set_led("off")
                    if not is_followup:
                        self.state = ClientState.PROCESSING
                        await asyncio.to_thread(self.player.play_tone, "low")
                        await self.speak_text(
                            "Neslyšela jsem nic. Zkuste prosím zmáčknout tlačítko a promluvit."
                        )
                    self.state = ClientState.IDLE
                    return last_reply

                await _settle_press_task()
                if external_stop is not None:
                    return await _finish_external_stop(external_stop)

                self.state = ClientState.PROCESSING
                self.button.set_led("blink")

                transcript_text = ""
                if self.stt.local_available or utterance.pre_transcribed_text:
                    transcript_text = await self._transcribe_utterance(utterance, is_followup=is_followup)

                if transcript_text and is_repeat_request(transcript_text) and self._last_reply_say:
                    log.info(
                        "Zachycena žádost o pomalejší zopakování (%r) – opakuji pomalu a hlasitěji.",
                        transcript_text,
                    )
                    self.button.set_led("blue" if self._last_reply_expects_answer else "green")
                    await self.speak_text(
                        self._last_reply_say,
                        rate_override=self.cfg.slow_repeat_rate,
                        extra_gain=1.30,
                    )
                    if (
                        self._last_reply_expects_answer
                        and self.cfg.auto_listen_followups
                        and followup_turn <= self.cfg.max_auto_followups
                    ):
                        is_followup = True
                        if self.cfg.echo_guard_s > 0:
                            await asyncio.sleep(self.cfg.echo_guard_s)
                        await asyncio.to_thread(self.player.play_tone, "listen_followup")
                        continue
                    self.button.set_led("off")
                    self.state = ClientState.IDLE
                    return self._last_full_reply

                reply = await self._send_with_heartbeat(utterance, transcript_text, is_followup=is_followup)
                last_reply = reply
                if not reply:
                    msg = MSG_SERVER_REJECTED if self._last_error_kind == "rejected" else MSG_SERVER_DOWN
                    await self._say_error(msg)
                    self.button.set_led("off")
                    self.state = ClientState.IDLE
                    return None

                self._remember_ts(reply)

                led_color = reply.get("led") or "off"
                tone_name = reply.get("tone") or "none"
                self.button.set_led(led_color)
                if tone_name in ("double", "low", "beep"):
                    await asyncio.to_thread(self.player.play_tone, tone_name)

                say_msg = (reply.get("say") or "").strip()
                expects = bool(reply.get("expects_answer"))
                if say_msg:
                    self._last_reply_say = say_msg
                    self._last_reply_expects_answer = expects
                    self._last_full_reply = reply
                    await self.speak_text(say_msg)
                    if self.cfg.bilingual_tts:
                        en_msg = await translate_cs_to_en(say_msg)
                        if en_msg:
                            await self.speak_text(en_msg, lang="en")

                if expects and self.cfg.auto_listen_followups and followup_turn < self.cfg.max_auto_followups:
                    followup_turn += 1
                    is_followup = True
                    if self.cfg.echo_guard_s > 0:
                        await asyncio.sleep(self.cfg.echo_guard_s)
                    await asyncio.to_thread(self.player.play_tone, "listen_followup")
                    continue

                if led_color not in ("blue", "blink"):
                    self.button.set_led("off")
                self.state = ClientState.IDLE
                return last_reply

        except asyncio.CancelledError:
            self.state = ClientState.IDLE
            return None
        except Exception as e:
            log.exception("Chyba při obsluze tlačítka: %s", e)
            self.state = ClientState.PROCESSING
            await self._say_error(MSG_GENERIC_ERROR)
            self.button.set_led("off")
            self.state = ClientState.IDLE
            return None
        finally:
            if press_task is not None and not press_task.done():
                press_task.cancel()
                try:
                    await press_task
                except (asyncio.CancelledError, Exception):
                    pass

    async def _record_voice(
        self,
        *,
        is_followup: bool = False,
        extra_cancel: Callable[[], bool] | None = None,
    ) -> RecordedUtterance:
        """Nahraje promluvu seniora pomocí Grandpa-Proof++ VAD s průběžným Rolling Micro-STT."""
        stop_stream = False
        loop = asyncio.get_running_loop()

        def _stop_chk() -> bool:
            return stop_stream or self._cancel_flag or (extra_cancel is not None and extra_cancel())

        def _is_canceled() -> bool:
            return self._cancel_flag or (extra_cancel is not None and extra_cancel())

        def _finish_early() -> bool:
            return self._finish_listening_early

        def _on_followup_btn() -> bool:
            if self._followup_button_pressed:
                self._followup_button_pressed = False
                return True
            return False

        def _on_pause_check(pcm_so_far: bytes):  # noqa: ANN202
            """Na začátku pomlky spustí na pozadí rychlý spekulativní přepis a hned se vrátí.

            Nahrávání nečeká: když senior už nepromluví, je přepis hotový zároveň s koncem ticha.
            Pokud už ale předchozí přepis běží (`self.stt.busy`), nový nezařazujeme do fronty,
            aby nevznikalo čekání na zámek `_run_lock`."""
            if not self.cfg.rolling_micro_stt or not self.stt.local_available:
                return None
            if getattr(self.stt, "busy", False):
                return None
            self._cancel_pause_job()
            try:
                cleaned_pcm = apply_mic_speech_cleanup_pcm16(
                    pcm_so_far,
                    rate=self.cfg.sample_rate,
                    bandpass=self.cfg.mic_bandpass_filter,
                    transient_filter=self.cfg.mic_transient_filter,
                    noise_subtraction=self.cfg.mic_noise_subtraction,
                    agc_enabled=self.cfg.mic_agc_enabled,
                    target_rms=self.cfg.mic_agc_target_rms,
                    max_gain=self.cfg.mic_agc_max_gain,
                )
                wav_chunk = pcm_to_wav(cleaned_pcm, self.cfg.sample_rate)
                cancel_ev = threading.Event()
                kwargs: dict = {"is_followup": is_followup}
                if self._stt_accepts_cancel():
                    kwargs["cancel_event"] = cancel_ev
                if self._stt_accepts_fast_speculative():
                    kwargs["fast_speculative"] = True
                self._pause_job_cancel = cancel_ev
                self._pause_job = asyncio.run_coroutine_threadsafe(
                    self.stt.transcribe_wav(wav_chunk, **kwargs), loop
                )
                return PAUSE_PENDING
            except Exception as e:
                log.debug("Průběžný přepis přeskočen: %s", e)
                return None

        def _poll_pause() -> PauseCheckResult | None:
            fut = self._pause_job
            if fut is None:
                return ("continue", "", "cs")
            if not fut.done():
                return None
            try:
                res = fut.result()
            except Exception as e:
                log.debug("Průběžný přepis selhal: %s", e)
                return ("continue", "", "cs")
            self._pause_result = res
            if not res.text:
                return ("continue", "", res.language)
            decision = classify_utterance_completeness(res.text, is_followup=is_followup)
            log.info("Přepis během pomlky: %r -> %s", res.text, decision)
            return decision, res.text, res.language

        def _run_vad() -> RecordedUtterance:
            nonlocal stop_stream
            try:
                frames = self._frame_source(_stop_chk)
                return process_vad_stream(
                    frames,
                    self.cfg,
                    is_button_held=self.button.is_held,
                    is_canceled=_is_canceled,
                    finish_early=_finish_early,
                    on_button_pressed_during_followup=_on_followup_btn,
                    on_pause_check=_on_pause_check if self.cfg.rolling_micro_stt else None,
                    poll_pause_check=_poll_pause,
                    on_pause_cancel=self._cancel_pause_job,
                    is_speaker_active=lambda: self.player.is_ducking_active(0.0),
                    is_followup=is_followup,
                )
            finally:
                stop_stream = True

        self._cancel_pause_job()
        try:
            utt = await asyncio.to_thread(_run_vad)
        except BaseException:
            self._cancel_pause_job()
            raise
        finally:
            stop_stream = True
        if utt.canceled or utt.timed_out:
            self._cancel_pause_job()
        return utt

    def _stt_accepts_cancel(self) -> bool:
        if self._stt_cancel_param is None:
            try:
                self._stt_cancel_param = (
                    "cancel_event" in inspect.signature(self.stt.transcribe_wav).parameters
                )
            except (TypeError, ValueError):
                self._stt_cancel_param = False
        return self._stt_cancel_param

    def _stt_accepts_fast_speculative(self) -> bool:
        try:
            return "fast_speculative" in inspect.signature(self.stt.transcribe_wav).parameters
        except (TypeError, ValueError):
            return False

    def _cancel_pause_job(self) -> None:
        """Zahodí rozpracovaný přepis z pomlky (senior mluví dál, nahrávání zrušeno…)."""
        ev, fut = self._pause_job_cancel, self._pause_job
        self._pause_job = self._pause_job_cancel = None
        self._pause_result = None
        if ev is not None:
            ev.set()
        if fut is not None and not fut.done():
            fut.cancel()

    async def _transcribe_utterance(self, utterance: RecordedUtterance, *, is_followup: bool) -> str:
        """Vybere nejrychlejší platný přepis: hotový z pomlky -> dobíhající z pomlky -> nový přepis."""
        if utterance.pre_transcribed_text:
            self._cancel_pause_job()
            return utterance.pre_transcribed_text
        covered = False
        text = ""
        if utterance.pre_transcription_pending and self._pause_job is not None:
            try:
                res = await asyncio.wait_for(asyncio.wrap_future(self._pause_job), timeout=12.0)
                text = res.text.strip()
                covered = bool(text)
            except (asyncio.TimeoutError, asyncio.CancelledError, concurrent.futures.CancelledError):
                text = ""
            except Exception as e:
                log.debug("Přepis z pomlky selhal: %s", e)
        elif self._pause_result is not None:
            text = self._pause_result.text.strip()
            covered = bool(text)
        self._cancel_pause_job()
        if text and not is_followup and utterance.speech_duration_s >= 1.5 and len(text.split()) <= 1:
            text = ""
            covered = False
        if not text and not covered and self.stt.local_available:
            res = await self.stt.transcribe_wav(utterance.stt_wav_bytes, is_followup=is_followup)
            text = res.text
        return text

    def _update_stt_context(self, st: dict) -> None:
        """Jména rodiny ze serveru dostane Whisper jako slovník – píše je pak správně („Lence“, „Denisovi“)."""
        if not isinstance(st, dict):
            return
        if isinstance(st.get("voice"), dict):
            self._server_stt = bool(st["voice"].get("stt"))
        setter = getattr(self.stt, "set_context_words", None)
        if setter is None:
            return
        names = [st.get("family"), st.get("family_dative")]
        try:
            setter([n for n in names if isinstance(n, str) and n.strip()])
        except Exception as e:
            log.debug("Slovník STT nelze nastavit: %s", e)

    async def _send_with_heartbeat(
        self,
        utterance: RecordedUtterance,
        transcript_text: str,
        *,
        is_followup: bool = False,
    ) -> dict | None:
        """Odešle požadavek na server a při delším zpracování (>2.5 s) jemně tiká na pozadí.

        Vrací odpověď serveru, nebo None, když se nepodařilo spojit (důvod v `_last_error_kind`).
        """
        hb_task: asyncio.Task | None = None
        if self.cfg.working_heartbeat_s > 0:
            hb_task = asyncio.create_task(self._working_heartbeat_loop())
        self._last_error_kind = ""
        try:
            if self.stt.local_available:
                if not transcript_text:
                    second = await self._server_second_opinion(utterance)
                    if second is not None:
                        return second
                    return {
                        "say": "Omlouvám se, nerozuměla jsem. Zkuste to prosím ještě jednou.",
                        "screen": "nerozumi",
                        "led": "off" if is_followup else "blue",
                        "tone": "low",
                        "expects_answer": not is_followup,
                        "ts": time.time(),
                    }
                log.info("Odesílám na server větu: %r", transcript_text)
                return await self._post_json("/api/utterance", {"text": transcript_text, "source": "button"})
            return await self._send_raw_audio_to_server(utterance.stt_pcm_bytes)
        except asyncio.CancelledError:
            raise
        except ServerRejectedError:
            self._last_error_kind = "rejected"
            return None
        except Exception as e:
            log.warning("Odeslání na server selhalo: %s", e)
            self._last_error_kind = self._last_error_kind or "down"
            return None
        finally:
            if hb_task:
                hb_task.cancel()

    async def _working_heartbeat_loop(self) -> None:
        """Každých `working_heartbeat_s` sekund přehraje tichý dřevěný tik, pokud HelpButton zrovna nemluví."""
        try:
            while not self._cancel_flag:
                await asyncio.sleep(self.cfg.working_heartbeat_s)
                if self._cancel_flag:
                    break
                if self.state != ClientState.PROCESSING or self.player.is_playing or self._say_lock.locked():
                    continue
                await asyncio.to_thread(self.player.play_tone, "working_tick")
        except asyncio.CancelledError:
            pass

    async def speak_text(
        self,
        text: str,
        lang: str = "cs",
        *,
        rate_override: float | None = None,
        extra_gain: float = 1.0,
        skippable: bool = False,
        cancel_check: Callable[[], bool] | None = None,
    ) -> None:
        """Přečte větu nahlas s větným streamováním (Sentence-Split Streaming TTS) a Senior Clarity DSP."""
        cleaned = (text or "").strip()
        if not cleaned or self._cancel_flag:
            return

        def _interrupted() -> bool:
            return (
                self._cancel_flag
                or (skippable and self._skip_prompt)
                or bool(cancel_check and cancel_check())
            )

        async with self._say_lock:
            if _interrupted():
                return
            prev_state = self.state
            self.state = ClientState.SPEAKING
            self.spoken_history.append(cleaned)
            if len(self.spoken_history) > 200:
                del self.spoken_history[:100]
            log.info("HelpButton říká (%s, rate=%s): %s", lang, rate_override or self.cfg.speech_rate, cleaned)
            tasks: list[asyncio.Task[bytes | None]] = []
            try:
                clauses = (
                    split_into_clauses(cleaned)
                    if (self.cfg.sentence_streaming_tts and rate_override is None)
                    else [cleaned]
                )
                if not clauses:
                    return

                tasks = [
                    asyncio.create_task(self.tts.synthesize(c, lang=lang, rate_override=rate_override))
                    for c in clauses
                ]
                spoke_any = False
                for t in tasks:
                    if _interrupted():
                        break
                    try:
                        wav = await t
                    except Exception as e:
                        log.warning("Syntéza řeči selhala: %s", e)
                        continue
                    if wav and not _interrupted():
                        await asyncio.to_thread(
                            self.player.play_wav_bytes,
                            wav,
                            apply_clarity=True,
                            extra_gain=extra_gain,
                        )
                        spoke_any = True
                if not spoke_any and not _interrupted():
                    log.error("Nepodařilo se vyslovit: %r (žádný TTS engine nefunguje?)", cleaned)
            finally:
                for t in tasks:
                    if not t.done():
                        t.cancel()
                if self.state == ClientState.SPEAKING:
                    self.state = prev_state

    async def _safe_press(self) -> dict | None:
        try:
            return await self._post_json("/api/press", {"source": "button"}, timeout=SHORT_TIMEOUT)
        except Exception as e:
            log.debug("POST /api/press selhal: %s", e)
            return None

    def _use_fallback_first(self) -> httpx.AsyncClient | None:
        """Je-li o serveru známo, že neodpovídá, jde se rovnou na záložní mozek (bez čekání na timeout)."""
        if self._server_ok or self._external_http is not None:
            return None
        return self._get_local_fallback_client()

    async def _get_json(self, path: str, *, timeout: httpx.Timeout | None = None) -> dict:
        fb = self._use_fallback_first()
        if fb is not None:
            r = await fb.get(path)
            r.raise_for_status()
            return r.json()
        try:
            await self._ensure_verified()
            r = await self._get_http_client().get(path, timeout=timeout or SHORT_TIMEOUT)
            raise_if_rejected(r, self.base_url)
            r.raise_for_status()
            self._server_ok = True
            return r.json()
        except ServerRejectedError as e:
            log.error("%s", e)
            raise
        except Exception as e:
            if not _is_unreachable(e):
                raise
            self._server_ok = False
            fb = self._get_local_fallback_client()
            if fb is not None:
                r = await fb.get(path)
                r.raise_for_status()
                return r.json()
            raise

    async def _post_json(self, path: str, payload: dict, *, timeout: httpx.Timeout | None = None) -> dict:
        fb = self._use_fallback_first()
        if fb is not None:
            log.info("Server HelpButton nedostupný – vyřizuji %s přes lokální záložní mozek na Pi 5.", path)
            r = await fb.post(path, json=payload)
            r.raise_for_status()
            return r.json()
        try:
            await self._ensure_verified()
            r = await self._get_http_client().post(path, json=payload, timeout=timeout or HTTP_TIMEOUT)
            raise_if_rejected(r, self.base_url)
            r.raise_for_status()
            self._server_ok = True
            return r.json()
        except ServerRejectedError as e:
            log.error("%s", e)
            raise
        except Exception as e:
            if not _is_unreachable(e):
                raise
            self._server_ok = False
            fb = self._get_local_fallback_client()
            if fb is not None:
                log.info(
                    "Server HelpButton nedostupný (%s) – vyřizuji %s přes lokální záložní mozek na Pi 5.", e, path
                )
                r = await fb.post(path, json=payload)
                r.raise_for_status()
                return r.json()
            raise

    async def _server_second_opinion(self, utterance: RecordedUtterance) -> dict | None:
        """Pi větě nerozumělo, ale senior opravdu mluvil: přepis zkusí ještě server (bývá na silnějším
        počítači, často s větším modelem) dřív, než seniora požádáme o zopakování. Vrací odpověď
        serveru jako /api/utterance, nebo None (server nemá STT / nedostupný / šlo jen o šum)."""
        if (
            not self._server_stt
            or not self._server_ok
            or self._use_fallback_first() is not None
            or utterance.speech_duration_s < 0.3
            or len(utterance.stt_wav_bytes) <= 44
        ):
            return None
        log.info("Lokální přepis je prázdný – nahrávku zkusí přepsat server.")
        try:
            await self._ensure_verified()
            r = await self._get_http_client().post(
                f"/api/audio?rate={self.cfg.sample_rate}&source=button",
                content=utterance.stt_wav_bytes,
                headers={"Content-Type": "audio/wav"},
                timeout=HTTP_TIMEOUT,
            )
            raise_if_rejected(r, self.base_url)
        except ServerRejectedError:
            raise
        except Exception as e:
            if _is_unreachable(e):
                self._server_ok = False
            log.debug("Přepis na serveru selhal: %s", e)
            return None
        if r.status_code == 501:
            self._server_stt = False
            return None
        if r.status_code >= 400:
            log.debug("Server odmítl přepis (HTTP %s)", r.status_code)
            return None
        self._server_ok = True
        try:
            reply = r.json()
        except ValueError:
            return None
        return reply if isinstance(reply, dict) else None

    async def _send_raw_audio_to_server(self, pcm_bytes: bytes) -> dict | None:
        """Odešle surové PCM na `/api/button/audio` (stejný protokol jako ESP32 v TDD)."""
        headers = {"Content-Type": "application/octet-stream"}
        path = f"/api/button/audio?rate={self.cfg.sample_rate}"
        try:
            await self._ensure_verified()
            r = await self._get_http_client().post(
                path, content=pcm_bytes, headers=headers, timeout=HTTP_TIMEOUT
            )
        except ServerRejectedError:
            raise
        except Exception as e:
            if _is_unreachable(e):
                self._server_ok = False
            self._last_error_kind = "down"
            log.warning("Chyba při odesílání audia na server: %s", e)
            return None
        raise_if_rejected(r, self.base_url)
        if r.status_code >= 400:
            log.warning("Server odmítl audio (HTTP %s): %s", r.status_code, r.text[:200])
            self._last_error_kind = "down"
            return None
        self._server_ok = True
        say_hdr = unquote(r.headers.get("x-helpbutton-say", ""))
        reply = {
            "say": say_hdr,
            "led": r.headers.get("x-helpbutton-led", "off"),
            "tone": r.headers.get("x-helpbutton-tone", "none"),
            "screen": r.headers.get("x-helpbutton-screen", "klid"),
            "expects_answer": r.headers.get("x-helpbutton-expects", "0") == "1",
            "ts": time.time(),
        }
        if r.status_code == 200 and r.content and len(r.content) > 44:
            self.button.set_led(reply["led"])
            if reply["tone"] in ("double", "low", "beep"):
                await asyncio.to_thread(self.player.play_tone, reply["tone"])
            if say_hdr:
                self._last_reply_say = say_hdr
                self._last_reply_expects_answer = bool(reply["expects_answer"])
                self._last_full_reply = dict(reply)
                self.spoken_history.append(say_hdr)
            await asyncio.to_thread(
                self.player.play_wav_bytes,
                r.content,
                apply_clarity=True,
            )
            reply["say"] = ""
            reply["tone"] = "none"
        return reply

    async def _handle_ws_message(self, msg: dict) -> None:
        mtype = msg.get("type")
        if mtype == "snapshot":
            if msg.get("senior"):
                self.senior_name = str(msg["senior"])
            self._update_stt_context(msg)
        elif mtype == "say":
            text = (msg.get("text") or "").strip()
            if text and self.state not in (
                ClientState.LISTENING,
                ClientState.AUTO_LISTENING,
                ClientState.PROMPTING,
            ):
                self._ws_interrupt = False
                await self.speak_text(text, cancel_check=lambda: self._ws_interrupt)
        elif mtype == "reply":
            rep = msg.get("reply") or {}
            try:
                ts = float(rep.get("ts") or 0.0)
            except (TypeError, ValueError):
                ts = 0.0
            if (
                ts
                and ts not in self._seen_reply_ts
                and self.state == ClientState.IDLE
                and not self._session_active()
            ):
                self._remember_ts(rep)
                led = rep.get("led") or "off"
                self.button.set_led(led)
                if rep.get("tone") in ("double", "low", "beep"):
                    await asyncio.to_thread(self.player.play_tone, rep["tone"])
                if rep.get("say"):
                    self._last_reply_say = rep["say"]
                    self._last_reply_expects_answer = bool(rep.get("expects_answer"))
                    self._ws_interrupt = False
                    await self.speak_text(rep["say"], cancel_check=lambda: self._ws_interrupt)
                if (
                    led not in ("blue", "blink")
                    and self.state == ClientState.IDLE
                    and not self._session_active()
                ):
                    self.button.set_led("off")

    async def _ws_ui_loop(self) -> None:
        """Poslouchá `/ws/ui` na serveru a čte nahlas průběžné hlášky (`type == 'say'`)."""
        try:
            import websockets
        except ImportError:
            return

        failures = 0
        while not self._closed:
            if self._last_error_kind == "rejected" and not self.cfg.token:
                await asyncio.sleep(max(30.0, SERVER_RECHECK_S * 2))
                continue
            ws_url = (
                self.base_url.replace("https://", "wss://").replace("http://", "ws://").rstrip("/") + "/ws/ui"
            )
            try:
                await self._ensure_verified()
                async with _ws_connect(websockets, ws_url, auth_headers(self.cfg.token)) as ws:
                    if failures:
                        log.info("WebSocket %s připojen.", ws_url)
                    failures = 0
                    async for raw in ws:
                        if self._cancel_flag:
                            continue
                        try:
                            msg = json.loads(raw)
                        except ValueError:
                            continue
                        if isinstance(msg, dict):
                            try:
                                await self._handle_ws_message(msg)
                            except asyncio.CancelledError:
                                raise
                            except Exception as e:
                                log.debug("Zpráva WebSocketu přeskočena: %s", e)
            except asyncio.CancelledError:
                break
            except Exception as e:
                failures += 1
                err_s = str(e)
                if "401" in err_s or "403" in err_s:
                    self._last_error_kind = "rejected"
                    if failures == 1:
                        log.warning(
                            "WebSocket %s odmítnut (%s) – zkontrolujte `[server] token` v rpi_button.toml nebo `--token`.",
                            ws_url,
                            err_s,
                        )
                    await asyncio.sleep(max(30.0, SERVER_RECHECK_S * 2))
                    continue
                if failures in (1, 10) or failures % 100 == 0:
                    log.warning("WebSocket %s nedostupný nebo odmítnut (%s) – zkouším znovu.", ws_url, e)
                await asyncio.sleep(min(10.0, 2.5 * min(failures, 4)))
