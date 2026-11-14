"""GUI agent (TDD v2) se skriptovaným „Vision modelem“ proti simulovaným obrazovkám."""

from __future__ import annotations

import re
from typing import Any, Callable

from helpbutton.server.agent import Vlm

from .conftest import Env

Decide = Callable[[str], dict[str, Any]]


def act(action: str, **kw: Any) -> dict[str, Any]:
    return {"thought": "", "say": kw.pop("say", ""), "action": action, **kw}


def el(prompt: str, label: str) -> int:
    """Najde číslo prvku podle popisku v seznamu, který agent posílá modelu."""
    m = re.search(r"\[(\d+)\] [^\n]*" + re.escape(label), prompt)
    assert m, f"prvek {label!r} na obrazovce není:\n{prompt}"
    return int(m.group(1))


class ScriptedVlm(Vlm):
    name = "scripted"

    def __init__(self, *steps: Decide):
        self.steps = list(steps)
        self.prompts: list[str] = []
        self.images = 0

    async def decide(self, system: str, text: str, image_b64: str | None) -> dict[str, Any]:
        self.prompts.append(text)
        self.images += bool(image_b64)
        return self.steps.pop(0)(text)


async def test_agent_closes_scam_popup_and_refuses_pay_button(cfg):
    vlm = ScriptedVlm(
        lambda p: act("click", element_id=el(p, "Zaplatit"), say="Klikám."),
        lambda p: act("click", element_id=el(p, "Zavřít"), say="Zavírám okno."),
        lambda p: act("done", text="Hotovo, okno je zavřené."),
    )
    e = await Env(cfg, vlm).start()
    r = await e.say("Vyskočilo mi divné okno a nejde zavřít")
    assert r.screen == "potvrzeni" and "Mám začít" in r.say
    assert e.phone.screen == "popup"
    r = await e.say("ano")
    assert r.screen == "hotovo" and "zavřené" in r.say
    assert e.phone.screen == "home"
    assert "REFUSED" in vlm.prompts[1]
    assert vlm.images == 3
    assert e.family.events[-1].kind == "success"
    assert any(m.get("type") == "agent" for m in e.events)


async def test_agent_asks_senior_and_continues(cfg):
    vlm = ScriptedVlm(
        lambda p: act("ask_user", text="Mám otevřít Fotky, nebo Obchod Play?"),
        lambda p: act("click", element_id=el(p, "Zavřít")),
        lambda p: act("click", element_id=el(p, "Fotky")),
        lambda p: act("done", text="Fotky jsou otevřené."),
    )
    e = await Env(cfg, vlm).start()
    await e.say("Něco mi tam svítí a nevím co s tím")
    r = await e.say("ano")
    assert r.expects_answer and "Fotky" in r.say
    r = await e.say("Fotky")
    assert r.screen == "hotovo"
    assert e.phone.screen == "photos"
    assert "senior answered: Fotky" in vlm.prompts[1]


async def test_agent_uses_system_tool_and_cannot_call_people(cfg):
    vlm = ScriptedVlm(
        lambda p: act("tool", tool="call_number", tool_args_json='{"phone": "+420111222333"}'),
        lambda p: act("tool", tool="get_battery", tool_args_json="{}"),
        lambda p: act("done", text="Baterie je v pořádku."),
    )
    e = await Env(cfg, vlm).start()
    await e.say("Zkontroluj mi telefon jestli je v pořádku")
    await e.say("ano")
    assert "ERROR: nástroj call_number" in vlm.prompts[1]
    assert '"level": 34' in vlm.prompts[2]
    assert not any(a.startswith("call") for a in e.phone.actions)


async def test_agent_never_types_codes(cfg):
    vlm = ScriptedVlm(
        lambda p: act("click", element_id=el(p, "Zavřít")),
        lambda p: act("click", element_id=el(p, "Obchod Play")),
        lambda p: act("type", element_id=el(p, "Hledat"), text="1234 5678"),
        lambda p: act("fail", text="Kód napsat nemůžu."),
    )
    e = await Env(cfg, vlm).start()
    await e.say("Napiš do obchodu ten kód co mi přišel")
    r = await e.say("ano")
    assert "REFUSED" in vlm.prompts[3]
    assert e.phone.typed == []
    assert "Petrovi" in r.say


async def test_agent_change_can_be_undone(cfg):
    vlm = ScriptedVlm(
        lambda p: act("click", element_id=el(p, "Zavřít")),
        lambda p: act("done", text="Hotovo."),
        lambda p: act("done", text="Vráceno."),
    )
    e = await Env(cfg, vlm).start()
    await e.say("Zavři tu reklamu")
    await e.say("ano")
    r = await e.say("Vrať to zpátky")
    assert "vrátit, co jsem právě udělala" in r.say
    r = await e.say("ano")
    assert r.say == "Vráceno."
    assert "Undo the changes" in vlm.prompts[2]


async def test_agent_drafts_reply_but_never_sends(cfg):
    from helpbutton.agents.mock import SCREENS

    SCREENS["phone"]["chat"] = [
        ("Napište zprávu", (40, 2200, 900, 2300), "input", "chat"),
        ("Odeslat SMS", (920, 2200, 1040, 2300), "button", "chat"),
    ]
    vlm = ScriptedVlm(
        lambda p: act("type", element_id=el(p, "Napište zprávu"), text="Adam je v poradku"),
        lambda p: act("click", element_id=el(p, "Odeslat")),
        lambda p: act("done", text="Odpověď je napsaná. Odešlete ji šipkou."),
    )
    e = await Env(cfg, vlm).start()
    r = await e.say("odepis prosim na zpravu, v zpravy, ze adam je v poradku")
    assert "Mám začít" in r.say
    e.phone.screen = "chat"
    r = await e.say("ano")
    assert "do NOT press send" in vlm.prompts[0] and "Messages (SMS)" in vlm.prompts[0]
    assert e.phone.typed == ["Adam je v poradku"]
    assert "REFUSED" in vlm.prompts[2]
    assert r.screen == "hotovo"


async def test_agent_reads_messages_but_never_codes(cfg):
    vlm = ScriptedVlm(
        lambda p: act(
            "done", say="Čtu vám zprávy.", text="Lenka píše: přijdu v 17:30. Banka píše: váš kód je 482 913."
        ),
    )
    e = await Env(cfg, vlm).start()
    await e.say("Přečti mi poslední zprávy")
    r = await e.say("ano")
    assert "přijdu v 17:30" in r.say
    assert "482" not in r.say and "(kód skrytý)" in r.say


async def test_agent_shows_desktop_icons_with_tool(cfg):
    """Přepis: „z počítačové plochy mi zmizely ikony“ – spolehlivě nástrojem místo klikání v menu."""
    vlm = ScriptedVlm(
        lambda p: act("tool", tool="set_desktop_icons", tool_args_json='{"visible": true}'),
        lambda p: act("done", text="Ikony na ploše jsou zpátky."),
    )
    e = await Env(cfg, vlm).start()
    e.pc.icons_visible = False
    r = await e.say("Z počítačové plochy mi zmizely ikony")
    assert "počítači" in r.say
    r = await e.say("ano")
    assert r.screen == "hotovo" and e.pc.icons_visible
    assert '"changed": true' in vlm.prompts[1]


async def test_agent_detects_loop_and_stops(cfg):
    """Přepis: kroky 4–10 pořád „musím na YouTube“ a klik, obrazovka beze změny."""
    vlm = ScriptedVlm(*[lambda p: act("key", key="volume_up")] * 10)
    e = await Env(cfg, vlm).start()
    await e.say("Něco mi na obrazovce pořád bliká a nevím co")
    r = await e.say("ano")
    assert len(vlm.prompts) == 4
    assert "SCREEN DID NOT CHANGE" in vlm.prompts[1]
    assert "WARNING: the screen has not changed" in vlm.prompts[2]
    assert "dokola" in r.say


def test_redact_codes():
    from helpbutton.common.safety import redact_codes

    assert redact_codes("Kód: 1234") == "Kód: (kód skrytý)"
    assert redact_codes("G-482913 je váš kód") == "G-(kód skrytý) je váš kód"
    assert redact_codes("sejdeme se v 17:30, 12. 11.") == "sejdeme se v 17:30, 12. 11."


async def test_without_vision_model_offers_family(env: Env):
    r = await env.say("Objednej mi taxi domů")
    assert "Petrovi" in r.say and "Mám začít" not in r.say


def center(prompt: str, label: str) -> tuple[int, int]:
    m = re.search(r"\[\d+\] [^\n]*" + re.escape(label) + r"[^\n]* @(\d+),(\d+)$", prompt, re.M)
    assert m, f"prvek {label!r} na obrazovce není:\n{prompt}"
    return int(m.group(1)), int(m.group(2))


async def test_agent_invented_element_id_falls_back_to_xy(cfg):
    """Log 11/2026: model poslal „click [64]“ (prvek neexistuje) i se souřadnicemi → má se kliknout na x,y."""
    vlm = ScriptedVlm(
        lambda p: act(
            "click",
            element_id=999,
            x=center(p, "Zavřít")[0],
            y=center(p, "Zavřít")[1],
            thought="close the popup",
        ),
        lambda p: act("done", text="Hotovo, okno je zavřené."),
    )
    e = await Env(cfg, vlm).start()
    await e.say("Vyskočilo mi divné okno a nejde zavřít")
    r = await e.say("ano")
    assert r.screen == "hotovo" and e.phone.screen == "home"
    assert "why: close the popup" in vlm.prompts[1]
    assert "window after: home" in vlm.prompts[1]


async def test_agent_invented_element_id_without_xy_reports_valid_ids(cfg):
    vlm = ScriptedVlm(
        lambda p: act("click", element_id=356),
        lambda p: act("fail", text="Nejde to."),
    )
    e = await Env(cfg, vlm).start()
    await e.say("Vyskočilo mi divné okno a nejde zavřít")
    await e.say("ano")
    assert "does not exist" in vlm.prompts[1] and "valid ids are 1.." in vlm.prompts[1]


async def test_chain_skips_models_on_exhausted_account():
    """Došel denní limit OpenRouteru → ostatní modely na stejném klíči se už nezkouší."""
    from helpbutton.server.agent import ChainVlm
    from helpbutton.server.openai_compat import Provider, QuotaExhausted

    calls: list[str] = []

    class Fake(Vlm):
        def __init__(self, name: str, key: str, quota: bool):
            self.name, self.quota = name, quota
            self.p = Provider(name, "https://openrouter.ai/api/v1", "m", key)

        async def decide(self, system: str, text: str, image_b64: str | None) -> dict[str, Any]:
            calls.append(self.name)
            if self.quota:
                raise QuotaExhausted("HTTP 429 free-models-per-day")
            return {"action": "done"}

    chain = ChainVlm([Fake("or-a", "k1", True), Fake("or-b", "k1", False), Fake("other", "k2", False)])
    assert await chain.decide("s", "t", None) == {"action": "done"}
    assert calls == ["or-a", "other"]
    calls.clear()
    await chain.decide("s", "t", None)
    assert calls == ["other"]


def test_quota_error_detection():
    from helpbutton.server.openai_compat import is_quota_error

    assert is_quota_error(429, "Rate limit exceeded: free-models-per-day. Add 10 credits")
    assert not is_quota_error(429, "Rate limit exceeded, retry in 2s")
    assert not is_quota_error(500, "daily maintenance")


async def test_agent_writes_run_history_and_learns(cfg):
    """Každý běh se zapíše (kroky, snímky, report.html) a příště model dostane zkušenost."""
    import json

    vlm = ScriptedVlm(
        lambda p: act("click", element_id=el(p, "Zavřít"), notes="1 close popup", thought="closing"),
        lambda p: act("done", text="Hotovo, okno je zavřené."),
    )
    e = await Env(cfg, vlm).start()
    await e.say("Vyskočilo mi divné okno a nejde zavřít")
    await e.say("ano")
    runs = list((cfg.data_dir / "agent_runs").iterdir())
    assert len(runs) == 1
    rows = [json.loads(x) for x in (runs[0] / "steps.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["type"] for r in rows] == ["start", "step", "step", "end"]
    assert rows[1]["target"].startswith("Zavřít") and rows[1]["screenshot"]
    assert (runs[0] / rows[1]["screenshot"]).stat().st_size > 0
    assert rows[-1]["status"] == "done"
    report = (runs[0] / "report.html").read_text(encoding="utf-8")
    assert "Krok 1" in report and "closing" in report
    assert "Your notes from the previous step: 1 close popup" in vlm.prompts[1]

    e.phone.screen = "popup"
    vlm.steps += [lambda p: act("done", text="Hotovo.")]
    await e.say("Zase mi vyskočilo divné okno a nejde zavřít")
    await e.say("ano")
    assert "SUCCEEDED" in vlm.prompts[2] and "click 'Zavřít" in vlm.prompts[2]


def test_memory_recall_prefers_similar_tasks(tmp_path):
    from helpbutton.server.agent_memory import AgentMemory

    m = AgentMemory(tmp_path)
    m.remember(
        "Pustit na YouTube video z kanálu Vidrail", "pc", "done", "ok", ["tool open_and_fix_browser -> OK"]
    )
    m.remember("Zapnout zvuk na počítači", "pc", "done", "ok", ["tool fix_sound -> OK"])
    m.remember("Pustit na YouTube video kanálu Vidrail", "pc", "fail", "ne", ["click (1,1) -> ERROR"])
    got = m.recall("pusť mi na youtube video od kanálu Vidrail", "pc")
    assert [r["status"] for r in got] == ["done", "fail"]
    assert m.recall("pusť mi na youtube video od kanálu Vidrail", "phone") == []
    assert "FAILED" in AgentMemory.as_prompt(got)


async def test_exhausted_account_sends_no_more_requests():
    """Po 429 free-models-per-day se na stejný klíč už nic neposílá (ani z porozumění, ani z agenta)."""
    import time

    import httpx
    import pytest

    from helpbutton.server import openai_compat as oc

    hits: list[str] = []
    reset_ms = int((time.time() + 600) * 1000)

    def handler(req: httpx.Request) -> httpx.Response:
        hits.append(req.url.host)
        return httpx.Response(
            429,
            json={
                "error": {
                    "message": "Rate limit exceeded: free-models-per-day. Add 10 credits",
                    "metadata": {"headers": {"X-RateLimit-Reset": str(reset_ms)}},
                }
            },
        )

    oc._EXHAUSTED.clear()
    try:
        a = oc.Provider("or-text", "https://openrouter.ai/api/v1", "m1", "k1")
        b = oc.Provider("or-vl", "https://openrouter.ai/api/v1", "m2", "k1")
        other = oc.Provider("groq", "https://api.groq.com/openai/v1", "m3", "k2")
        t = httpx.MockTransport(handler)
        with pytest.raises(oc.QuotaExhausted):
            await oc.chat(a, "s", "t", transport=t)
        with pytest.raises(oc.QuotaExhausted):
            await oc.chat(b, "s", "t", transport=t)
        assert hits == ["openrouter.ai"]
        assert abs(oc.exhausted_until(a) - reset_ms / 1000) < 1
        with pytest.raises(oc.QuotaExhausted):
            await oc.chat(other, "s", "t", transport=t)
        assert hits == ["openrouter.ai", "api.groq.com"]
    finally:
        oc._EXHAUSTED.clear()


class AskingVlm(ScriptedVlm):
    """Jako ScriptedVlm, navíc odpovídá na kontrolu „není už hotovo?“ před opakováním kroku."""

    def __init__(self, *steps: Decide, done: bool):
        super().__init__(*steps)
        self.done, self.asked = done, []

    async def ask(
        self, system: str, text: str, image_b64: str | None, schema: dict[str, Any]
    ) -> dict[str, Any]:
        self.asked.append(text)
        return {"done": self.done, "summary": "Fotku jsem udělala."}


async def test_repeat_of_successful_step_asks_if_already_done(cfg):
    """Živě: „vyfoť to“ – fotka se udělala, pak fotil dokola. Před zopakováním úspěšného kroku se HelpButton
    zeptá, jestli už není hotovo – a když ano, skončí (jedna fotka, ne deset)."""
    vlm = AskingVlm(*[lambda p: act("tool", tool="get_battery", tool_args_json="{}")] * 5, done=True)
    e = await Env(cfg, vlm).start()
    await e.say("Něco mi na obrazovce pořád bliká a nevím co")
    r = await e.say("ano")
    assert r.screen == "hotovo" and "Fotku jsem udělala" in r.say
    assert len(vlm.prompts) == 2 and "same action again" in vlm.asked[0]


async def test_same_step_never_runs_four_times_even_if_screen_changes(cfg):
    vlm = AskingVlm(*[lambda p: act("tool", tool="get_battery", tool_args_json="{}")] * 8, done=False)
    e = await Env(cfg, vlm).start()
    await e.say("Něco mi na obrazovce pořád bliká a nevím co")
    r = await e.say("ano")
    assert len(vlm.prompts) == 4 and "dokola" in r.say


def test_action_key_ignores_element_numbers():
    from helpbutton.server.agent import _action_key, _same_key

    d = {
        "action": "click",
        "element_id": 12,
        "x": 0,
        "y": 0,
        "tool": "",
        "tool_args_json": "",
        "key": "",
        "direction": "",
        "package": "",
        "text": "",
    }
    a = _action_key(d, {"label": "Spoušť (Button)"})
    b = _action_key({**d, "element_id": 31}, {"label": "Spoušť (Button)"})
    assert a == b == "click spoušť"
    assert _same_key(
        _action_key({**d, "element_id": 0, "x": 805, "y": 412}, None),
        _action_key({**d, "element_id": 0, "x": 812, "y": 398}, None),
    )
    assert not _same_key("click @100,100", "click @300,100")
