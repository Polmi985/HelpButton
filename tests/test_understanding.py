"""Porozumění: význam určuje model z kontextu (rozhovor, zařízení, co je otevřené); klíčová slova
jsou jen záloha bez modelu. Případy jsou ze skutečných rozhovorů."""

from __future__ import annotations

import time
from typing import Any

from helpbutton.server.agent import AgentSession, GuiAgent, Vlm
from helpbutton.server.brain import Brain, context_prompt, keyword_understand
from helpbutton.server.config import Config

from .conftest import Env

PC = [{"kind": "pc", "online": True}]
BOTH = PC + [{"kind": "phone", "online": True}]


def _hist(url: str = "https://www.seznam.cz") -> list[dict[str, Any]]:
    return [
        {
            "text": "otevri mi seznnam",
            "intent": "open_website",
            "url": url,
            "say": "Seznam",
            "ts": time.time(),
        }
    ]


def test_sites_with_typos_and_most_specific_match():
    cfg = Config()
    assert cfg.find_site("otevri mi seznnam") == "https://www.seznam.cz"
    assert cfg.find_site("nejnovejsi zpravy na seznamu") == "https://www.seznamzpravy.cz"
    assert cfg.find_site("chci si to precist na novinkach") == "https://www.novinky.cz"
    assert cfg.find_site("otevri mi zpravy na idnesu") == "https://www.idnes.cz/zpravy"


def test_news_is_web_not_sms_app():
    cfg = Config()
    for text, devs in (
        ("nejnovejsi zpravy na seznamu", BOTH),
        ("nejnovejsi zpravy", BOTH),
        ("otevri zpravy", PC),
        ("chci si precist novinky", BOTH),
    ):
        u = keyword_understand(text, cfg, None, devs)
        assert u.intent == "open_website" and u.app == "", text
    assert keyword_understand("otevri zpravy", cfg, None, BOTH).intent == "open_app"
    u = keyword_understand("jake je dnes pocasi", cfg, None, PC)
    assert (u.intent, u.url) == ("open_website", "https://pocasi.seznam.cz")


def test_internet_word_alone_is_not_a_network_problem():
    cfg = Config()
    assert keyword_understand("najdi mi na internetu recept", cfg, None, PC).intent == "complex_task"
    assert keyword_understand("nejde mi internet", cfg, None, PC).intent == "no_internet"


def test_follow_up_continues_on_open_site():
    cfg = Config()
    u = keyword_understand("nejnovejsi zpravy", cfg, _hist(), BOTH)
    assert (u.intent, u.url) == ("open_website", "https://www.seznamzpravy.cz")
    u = keyword_understand("a co sport", cfg, _hist(), PC)
    assert u.intent == "complex_task" and "seznam.cz" in u.context
    old = [{**_hist()[0], "ts": time.time() - 3600}]
    assert not keyword_understand("a co sport", cfg, old, PC).context


class FakeLlm:
    """Model, který vrátí předem dané JSON – a zapamatuje si, co dostal."""

    def __init__(self, cfg: Config, answer: dict[str, Any]):
        self.brain = Brain(cfg)
        self.answer, self.seen = answer, []

        async def llm(provider, text, devices, history=None):  # noqa: ANN001, ANN202
            self.seen.append(context_prompt(cfg, devices, history, text))
            return {
                "meaning": "",
                "target": "auto",
                "app": "",
                "contact": "",
                "text": "",
                "url": "",
                "channel": "",
                "say": "",
                "confidence": 0.9,
                **self.answer,
            }

        async def chain():  # noqa: ANN202
            return ["ollama"]

        self.brain._llm = llm
        self.brain.chain = chain


async def test_model_decides_meaning_keywords_do_not_override():
    """„udělej mi seznam věcí na nákup“: klíčová slova by viděla web seznam.cz, model ví, že jde o seznam."""
    cfg = Config(llm_provider="auto")
    f = FakeLlm(cfg, {"intent": "complex_task", "text": "Vytvořit seznam nákupu"})
    u = await f.brain.understand("udelej mi seznam veci na nakup", PC)
    assert u.intent == "complex_task" and u.source == "ollama"
    f = FakeLlm(cfg, {"intent": "question", "text": ""})
    u = await f.brain.understand("jak se dela fotka obrazovky", PC)
    assert u.text == "jak se dela fotka obrazovky"


async def test_safety_still_beats_model():
    cfg = Config(llm_provider="auto")
    f = FakeLlm(cfg, {"intent": "complex_task"})
    u = await f.brain.understand("volal mi pan z banky, at si nainstaluju anydesk", PC)
    assert u.intent == "scam_check"


async def test_keywords_only_when_no_model_answers():
    cfg = Config(llm_provider="auto")
    brain = Brain(cfg)

    async def broken(*a, **kw):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("Ollama neběží")

    async def chain():  # noqa: ANN202
        return ["ollama"]

    brain._llm, brain.chain = broken, chain
    u = await brain.understand("nic neslysim", PHONE)
    assert (u.intent, u.source) == ("sound_low", "keywords")


def test_model_gets_context_to_decide():
    """Model dostane, co je kde otevřené, které zařízení je zapnuté a co se před chvílí stalo."""
    cfg = Config()
    devs = [
        {
            "id": "pc-1",
            "name": "Počítač",
            "kind": "pc",
            "online": True,
            "status": {"foreground": "Seznam – najdu tam, co neznám - Opera"},
        },
        {"id": "phone-1", "name": "Telefon babičky", "kind": "phone", "online": False, "status": {}},
    ]
    hist = [{**_hist()[0], "device": "Počítač"}]
    p = context_prompt(cfg, devs, hist, "a ted nejnovejsi zpravy")
    assert "open now: Seznam – najdu tam, co neznám - Opera" in p and 'Telefon babičky" (phone) - OFF' in p
    assert "otevri mi seznnam" in p and "https://www.seznam.cz" in p and "Počítač]" in p
    assert "<veta>a ted nejnovejsi zpravy</veta>" in p
    assert "nothing - this is a new request" in context_prompt(cfg, devs, None, "x")


async def test_conversation_from_bug_report(env: Env):
    """„otevri mi seznnam“ → „nejnovejsi zpravy“: druhá věta otevře zprávy na Seznamu, ne agenta."""
    r = await env.say("otevri mi seznnam")
    assert env.pc.opened[-1] == "https://www.seznam.cz" and "Seznam" in r.say
    r = await env.say("nejnovejsi zpravy")
    assert env.pc.opened[-1] == "https://www.seznamzpravy.cz"
    assert "Seznam Zprávy" in r.say


class ScriptVlm(Vlm):
    name = "script"

    def __init__(self, *answers: dict[str, Any], checks: list[dict[str, Any]] | None = None):
        self.answers = list(answers)
        self.prompts: list[str] = []
        self.checks, self.asked = checks, []

    async def ask(
        self, system: str, text: str, image_b64: str | None, schema: dict[str, Any]
    ) -> dict[str, Any]:
        if self.checks is None or "done" not in schema.get("properties", {}):
            raise NotImplementedError
        self.asked.append(text)
        return self.checks.pop(0)

    async def decide(self, system: str, text: str, image_b64: str | None) -> dict[str, Any]:
        self.prompts.append(text)
        return {"thought": "", "say": "", **self.answers.pop(0)}


async def test_agent_leaves_console_and_ignores_nonsense_question(cfg, monkeypatch):
    e = await Env(cfg).start()
    pc = e.registry.pick(("pc",))
    real = e.pc.observe_screen

    async def observe() -> dict[str, Any]:
        obs = await real()
        if e.pc.screen == "desktop":
            obs.update(
                active_app="Windows PowerShell",
                system_window=True,
                open_windows=["Windows PowerShell", "Seznam - Google Chrome (minimized)"],
            )
        return obs

    monkeypatch.setattr(e.pc, "observe_screen", observe)
    e.pc._tools["observe_screen"] = (e.pc._tools["observe_screen"][0], observe)
    vlm = ScriptVlm(
        {"action": "ask_user", "text": "Zavřela jsem?"}, {"action": "done", "text": "Zprávy jsou otevřené."}
    )
    out = await GuiAgent(e.registry, vlm).run(AgentSession(task="nejnovejsi zpravy na seznamu", device=pc))
    assert "KEY_PRESS win+d" in e.pc.gui_log
    assert out.status == "done"
    assert "not a real question" in vlm.prompts[1]


VIDRAIL = "otevri mi prosim na pocitaci youtubove video, ktere je nejvic oblibene od Vidraila"


def test_youtube_task_is_not_just_open_youtube():
    cfg = Config()
    for text in (VIDRAIL, "chci na youtube najit nejvic oblibene video od vidraila"):
        for devs in (PC, BOTH):
            u = keyword_understand(text, cfg, None, devs)
            assert u.intent == "complex_task", (text, devs)
    assert keyword_understand("otevri youtube", cfg, None, PC).intent == "open_website"


def test_rule_plan_for_youtube_most_popular():
    from helpbutton.server.planner import rule_plan

    p = rule_plan(VIDRAIL, "pc")
    assert p.start_url == "https://www.youtube.com/results?search_query=Vidrail&sp=CAM%253D"
    assert "Vidrail" in p.say and "nejoblíbenější" in p.say
    assert any("first real video" in s for s in p.steps)
    p = rule_plan("pusť mi na YouTube nejnovější video od Michala Davida", "pc")
    assert "search_query=Michal+David&sp=CAI" in p.start_url
    assert rule_plan("vytvor mi na plose slozku fotky", "pc") is None


def test_model_plan_is_checked():
    from helpbutton.server.planner import checked_plan

    p = checked_plan(
        {
            "steps": ["Open Settings", "Click Personalization"],
            "start_url": "javascript:alert(1)",
            "say": "Otevřu nastavení.",
        },
        "x",
    )
    assert p.steps == ["Open Settings", "Click Personalization"] and p.start_url == ""
    assert checked_plan({"steps": [], "start_url": "", "say": ""}, "x") is None
    assert checked_plan({"steps": ["a"], "start_url": "https://anydesk.com/download"}, "x").start_url == ""


async def test_agent_plan_is_said_and_followed(cfg, monkeypatch):
    """YouTube dělá pevný postup (routines.py): vyhledá jako data, vybere video kanálu a otevře přímo jeho
    adresu – žádné klikání po stránce (živě: model klikl na hledání Windows místo hledání YouTube)."""
    from helpbutton.server import webtools

    asked: list[dict] = []

    async def fake_search(query: str, order: str = "relevance", channel: str = "", limit: int = 6) -> dict:
        asked.append({"query": query, "order": order, "channel": channel})
        return {
            "videos": [
                {"title": "WANNABE", "channel": "Vidrail", "url": "https://www.youtube.com/watch?v=b2"}
            ]
        }

    monkeypatch.setitem(webtools.TOOLS["web_youtube_search"], "fn", fake_search)
    vlm = ScriptVlm({"action": "done", "text": "Hraje video Vidrail."})
    e = await Env(cfg, vlm).start(phone=False)
    r = await e.say(VIDRAIL)
    assert r.screen == "hotovo" and "WANNABE" in r.say and "Vidrail" in r.say
    assert asked == [{"query": "Vidrail", "order": "views", "channel": "Vidrail"}]
    assert e.pc.opened == ["https://www.youtube.com/watch?v=b2"]
    assert vlm.prompts == []


async def test_settings_task_opens_settings_page_first(cfg):
    vlm = ScriptVlm({"action": "done", "text": "Tmavý režim je zapnutý."})
    e = await Env(cfg, vlm).start(phone=False)
    r = await e.say("zapni mi na pocitaci tmavy rezim")
    assert "Nastavení" in r.say and "Mám začít" in r.say
    await e.say("ano")
    assert "settings dark_mode" in e.pc.actions
    assert "Plan for this task" in vlm.prompts[0]


def test_model_plan_rejects_bad_sentence_for_senior():
    from helpbutton.server.planner import checked_plan

    p = checked_plan(
        {
            "steps": ["Otevřu nastavení tmavého režimu"],
            "start_url": "https://www.google.com/search?q=tmavý+režim",
            "say": "Zkuste otevřít aplikaci pro nastavení výběru základních nastavení",
        },
        "zapni tmavy rezim",
    )
    assert p.say == ""


PHONE = [{"kind": "phone", "online": True}]


async def test_open_website_on_phone(cfg):
    e = await Env(cfg).start(pc=False)
    r = await e.say("na mobilu otevri seznam")
    assert "open_url https://www.seznam.cz" in e.phone.actions
    assert "v telefonu" in r.say and "Počítač" not in r.say


def test_not_ringing_is_sound_problem():
    u = keyword_understand("telefon mi nevyzvani kamo proc", Config(), None, PHONE)
    assert (u.intent, u.target) == ("sound_low", "phone")


def test_irrelevant_model_plan_is_dropped():
    from helpbutton.server.planner import checked_plan

    raw = {
        "steps": ["Open Mapy.cz app", "Check the map is shown"],
        "start_url": "",
        "say": "Otevřu aplikaci Mapy.cz a zkontroluji, zda je na obrazovce zobrazena mapa.",
    }
    assert checked_plan(raw, "telefon mi nevyzvani kamo proc") is None
    ok = {
        "steps": ["Open Settings", "Turn on dark theme"],
        "start_url": "",
        "say": "Otevřu nastavení a zapnu tmavý motiv.",
    }
    assert checked_plan(ok, "zapni tmavy motiv") is not None


async def test_agent_narrates_steps_when_model_is_silent(cfg):
    said: list[str] = []
    vlm = ScriptVlm({"action": "click", "element_id": 2}, {"action": "done", "text": "Hotovo."})
    e = await Env(cfg, vlm).start(pc=False)

    async def say(t: str) -> None:
        said.append(t)

    dev = e.registry.pick(("phone",))
    await GuiAgent(e.registry, vlm, say=say).run(AgentSession(task="otevri nastaveni", device=dev))
    assert any(t.startswith("Klikám na „") for t in said)


def test_clock_answers_without_model():
    from helpbutton.server.orchestrator import clock_answer

    now = time.strptime("2026-11-12 21:03", "%Y-%m-%d %H:%M")
    assert clock_answer("kolik je hodin", now) == "Je 21 hodin a 3 minuty."
    assert clock_answer("kolikateho je dneska", now) == "Dnes je čtvrtek 12. listopadu 2026."
    assert clock_answer("jak se dela fotka obrazovky", now) == ""


def test_model_plan_can_pick_settings_shortcut():
    from helpbutton.server.planner import checked_plan

    raw = {
        "steps": ["Settings page Colors is open", "Choose Dark mode"],
        "start_settings": "dark_mode",
        "start_url": "https://www.google.com",
        "say": "Otevřu nastavení barev a zapnu tmavý režim.",
    }
    p = checked_plan(raw, "zapni tmavy rezim", "pc")
    assert p.start_settings == "dark_mode" and p.start_url == ""
    raw["start_settings"] = "windowsdefender"
    assert checked_plan(raw, "zapni tmavy rezim", "pc").start_settings == ""


def test_plan_with_command_line_is_dropped():
    from helpbutton.server.planner import checked_plan

    raw = {
        "steps": [
            "Otevřu Windows PowerShell, kde zadejím příkaz pro vytvoření složky s názvem 'pejsek'.",
            r"Zadejím příkaz `New-Item -Path 'C:\' -Name 'pejsek' -ItemType Directory` a potvrzuji.",
        ],
        "say": "Otevřu Windows PowerShell a vytvořím složku pejsek.",
    }
    assert checked_plan(raw, "vytvor na plose slozku s nazev pejsek", "pc") is None


async def test_agent_creates_folder_with_tool(cfg):
    vlm = ScriptVlm(
        {"action": "tool", "tool": "create_folder", "tool_args_json": '{"name": "pejsek"}'},
        {"action": "done", "text": "Složka pejsek je na ploše."},
    )
    e = await Env(cfg, vlm).start(phone=False)
    dev = e.registry.pick(("pc",))
    out = await GuiAgent(e.registry, vlm).run(AgentSession(task="vytvor na plose slozku pejsek", device=dev))
    assert out.status == "done" and "create_folder desktop/pejsek" in e.pc.actions


def test_create_folder_only_on_desktop_or_documents(tmp_path, monkeypatch):
    import pytest

    from helpbutton.agents import pc as pcmod

    monkeypatch.setattr(
        pcmod, "_known_folder", lambda where: tmp_path if where in ("desktop", "documents") else None
    )
    agent = pcmod.PcAgent("PC")
    assert agent.create_folder("pejsek")["created"] and (tmp_path / "pejsek").is_dir()
    assert agent.create_folder("pejsek")["existed"]
    assert agent.create_folder(r"..\..\Windows\x")["path"].startswith(str(tmp_path))
    for bad in ("CON", "", "..."):
        with pytest.raises(Exception):
            agent.create_folder(bad)
    with pytest.raises(Exception):
        agent.create_folder("x", where=r"C:\Windows")


async def test_agent_gives_up_on_same_action_even_if_screen_changes(cfg, monkeypatch):
    """Živě: 6× „Focus Terminal“ – na obrazovce se měnil text, takže stará kontrola nic nepoznala."""
    vlm = ScriptVlm(*[{"action": "click", "element_id": 1}] * 8)
    e = await Env(cfg, vlm).start(phone=False)
    real, n = e.pc.observe_screen, {"i": 0}

    async def observe() -> dict[str, Any]:
        e.pc.screen = "desktop"
        obs = await real()
        n["i"] += 1
        obs["screen_text"] = [f"výstup {n['i']}"]
        return obs

    e.pc._tools["observe_screen"] = (e.pc._tools["observe_screen"][0], observe)
    dev = e.registry.pick(("pc",))
    out = await GuiAgent(e.registry, vlm).run(AgentSession(task="neco", device=dev))
    assert out.status == "fail" and "to samé" in out.say
    assert "does not help" in vlm.prompts[3]


def _pc_tools() -> dict[str, dict[str, Any]]:
    from helpbutton.agents.pc import PcAgent

    return {s.name: s.to_json() for s in PcAgent("x", dry_run=True).tool_specs}


def test_first_step_from_small_model_is_checked():
    from helpbutton.server.planner import tool_plan

    tools = _pc_tools()
    p = tool_plan({"tool": "create_folder", "args": {"name": "pejsek", "where": "desktop"}}, tools)
    assert p.start_tool == "create_folder" and p.say == "Vytvořím na ploše složku „pejsek“."
    p = tool_plan({"tool": "open_settings", "args": {"page": "dark_mode"}}, tools)
    assert "tmavý režim" in p.say
    p = tool_plan(
        {
            "tool": "open_and_fix_browser",
            "args": {"url": "https://www.youtube.com/results?search_query=Vidrail&sp=CAM%253D"},
        },
        tools,
    )
    assert p.say == "Otevřu youtube.com a vyhledám „Vidrail“."
    assert (
        tool_plan({"tool": "execute_action", "args": {"action_type": "KEY_PRESS", "key": "win+r"}}, tools)
        is None
    )
    assert tool_plan({"tool": "shell_read", "args": {"command": "ipconfig"}}, tools) is None
    assert tool_plan({"tool": "neexistuje", "args": {}}, tools) is None
    assert tool_plan({"tool": "create_folder", "args": {}}, tools) is None
    assert tool_plan({"tool": "open_and_fix_browser", "args": {"url": "https://anydesk.com"}}, tools) is None
    assert tool_plan({"tool": "", "args": {}}, tools) is None


async def test_small_model_first_step_is_said_and_run(cfg):
    """Malý model: úzká otázka na první krok → senior ho slyší → agent ho provede kódem a jen ověří."""
    vlm = ScriptVlm({"action": "done", "text": "Složka pejsek je na ploše."})
    e = await Env(cfg, vlm).start(phone=False)

    async def first_step(task, device, tools, state=""):  # noqa: ANN001, ANN202
        assert "create_folder(name, where)" in tools and "execute_action" not in tools
        return {"meaning": "create folder", "tool": "create_folder", "args": {"name": "pejsek"}}

    async def strong() -> bool:
        return False

    e.orch.brain.first_step, e.orch.brain.strong_planner = first_step, strong
    r = await e.say("vytvor na plose slozku s nazev pejsek")
    assert "Vytvořím na ploše složku „pejsek“." in r.say and "Mám začít" in r.say
    r = await e.say("ano")
    assert r.screen == "hotovo" and "create_folder desktop/pejsek" in e.pc.actions
    assert "HelpButton already did step 1" in vlm.prompts[0]


async def test_nonsense_done_text_replaced_by_czech_summary(cfg):
    vlm = ScriptVlm({"action": "done", "text": "Show Desktop"})
    e = await Env(cfg, vlm).start(phone=False)
    dev = e.registry.pick(("pc",))
    s = AgentSession(task="vytvor na plose slozku pejsek", device=dev)
    s.start_tool, s.start_args = "create_folder", {"name": "pejsek"}
    out = await GuiAgent(e.registry, vlm).run(s)
    assert out.say == "Hotovo. Na ploše je nová složka „pejsek“."


async def test_strong_planner_is_not_local_model(cfg):
    from helpbutton.server.brain import Brain

    b = Brain(cfg)

    async def chain() -> list[str]:
        return ["ollama", "openrouter"]

    b.chain = chain
    cfg.ollama_model = "qwen3-vl:8b-instruct"
    assert await b.strong_planner() is False


async def test_first_step_tool_that_finishes_task_ends_without_clicking(cfg):
    vlm = ScriptVlm(checks=[{"done": True, "summary": "Složka je na ploše."}])
    e = await Env(cfg, vlm).start(phone=False)

    async def first_step(task, device, tools, state=""):  # noqa: ANN001, ANN202
        return {
            "meaning": "create folder",
            "tool": "create_folder",
            "args": {"name": "zolty", "where": "desktop"},
        }

    async def strong() -> bool:
        return False

    e.orch.brain.first_step, e.orch.brain.strong_planner = first_step, strong
    await e.say("udelej mi slozku na plose s nazvem zolty")
    r = await e.say("ano")
    assert r.screen == "hotovo" and "zolty" in r.say
    assert e.pc.actions.count("create_folder desktop/zolty") == 1 and not vlm.prompts
    assert "udelej mi slozku" in vlm.asked[0] and "created" in vlm.asked[0]


async def test_agent_stops_after_tool_that_did_the_task(cfg):
    vlm = ScriptVlm(
        {"action": "click", "element_id": 1},
        {"action": "tool", "tool": "create_folder", "tool_args_json": '{"name": "zolty"}'},
        {"action": "click", "element_id": 1},
        checks=[{"done": True, "summary": "Hotovo."}],
    )
    e = await Env(cfg, vlm).start(phone=False)
    dev = e.registry.pick(("pc",))
    out = await GuiAgent(e.registry, vlm).run(
        AgentSession(task="udelej mi slozku na plose s nazvem zolty", device=dev)
    )
    assert out.status == "done" and "zolty" in out.say and len(vlm.prompts) == 2


async def test_agent_continues_when_tool_did_only_part_of_task(cfg):
    vlm = ScriptVlm(
        {"action": "tool", "tool": "create_folder", "tool_args_json": '{"name": "fotky"}'},
        {"action": "tool", "tool": "open_settings", "tool_args_json": '{"page": "display"}'},
        {"action": "done", "text": "Složku jsem vytvořila a fotky přesunula."},
        checks=[{"done": False, "summary": ""}],
    )
    e = await Env(cfg, vlm).start(phone=False)
    dev = e.registry.pick(("pc",))
    out = await GuiAgent(e.registry, vlm).run(
        AgentSession(task="vytvor slozku fotky a presun do ni obrazky", device=dev)
    )
    assert out.status == "done" and len(vlm.prompts) == 3
    assert len(vlm.asked) == 1


async def test_same_failing_tool_call_is_not_repeated(cfg):
    calls: list[str] = []

    async def focus_window(title: str) -> dict[str, Any]:
        calls.append(title)
        raise ValueError(f"okno „{title}“ není otevřené")

    vlm = ScriptVlm(
        *[{"action": "tool", "tool": "focus_window", "tool_args_json": '{"title": "File Explorer"}'}] * 3,
        {"action": "fail", "text": "Nejde to."},
    )
    e = await Env(cfg, vlm).start(phone=False)
    e.pc._tools["focus_window"] = (e.pc._tools["focus_window"][0], focus_window)
    dev = e.registry.pick(("pc",))
    await GuiAgent(e.registry, vlm).run(AgentSession(task="neco", device=dev))
    assert len(calls) == 1 and "do something different" in vlm.prompts[2]
