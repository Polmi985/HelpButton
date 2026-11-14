"""Pevné postupy (routines.py) a hledání kontaktů – časté úkoly bez modelu a bez klikání."""

from __future__ import annotations

from typing import Any

import pytest

from helpbutton.common import contacts
from helpbutton.server import routines, webtools

from .conftest import Env
from .test_agent import ScriptedVlm, act

PC = {
    "open_and_fix_browser",
    "create_folder",
    "list_files",
    "move_files",
    "open_item",
    "find_files",
    "start_app",
    "pc_health",
    "list_printers",
}


BOOK = [
    {"name": n, "phone": str(i), "type": "2"}
    for i, n in enumerate(
        [
            "Lenka Nováková",
            "Jiří Novák",
            "Jan Svoboda",
            "Máma",
            "Petr Dvořák",
            "Petra Malá",
            "Josef Kraus",
            "Lucie Černá",
            "Babička Věra",
            "Doktorka Hrubá",
        ]
    )
]


@pytest.mark.parametrize(
    "said,expected",
    [
        ("Lence", "Lenka Nováková"),
        ("sestře Lence", "Lenka Nováková"),
        ("Jirkovi", "Jiří Novák"),
        ("Honzovi", "Jan Svoboda"),
        ("Pepovi", "Josef Kraus"),
        ("mamce", "Máma"),
        ("babičce", "Babička Věra"),
        ("doktorce", "Doktorka Hrubá"),
        ("Petrovi", "Petr Dvořák"),
        ("Nowakova", "Lenka Nováková"),
        ("Lusie", "Lucie Černá"),
    ],
)
def test_contact_found_like_senior_says_it(said: str, expected: str):
    assert contacts.match(said, BOOK)[0]["name"] == expected


def test_contact_suggestions_when_nothing_fits():
    assert contacts.match("Karlovi", BOOK) == []
    assert [s["name"] for s in contacts.suggest("Lenča", BOOK)][:1] == ["Lenka Nováková"]


async def test_unknown_contact_gets_suggestions_not_silence(env: Env):
    r = await env.say("Zavolej Lenoře")
    assert r.expects_answer and ("Lenka" in r.say)


@pytest.mark.parametrize(
    "text,kind,name",
    [
        ("pust mi na youtube video s pejskama", "pc", "youtube"),
        (
            "otevri mi prosim na pocitaci youtubove video, ktere je nejvic oblibene od Vidraila",
            "pc",
            "youtube",
        ),
        ("Pusť mi písničky od Karla Gotta", "phone", "youtube"),
        (
            "vytvor mi na plose slozku pejsek a dej tam soubory ktere jsem si stahl dneska",
            "pc",
            "downloads_to_folder",
        ),
        ("vytvoř mi v dokumentech složku Recepty", "pc", "new_folder"),
        ("kde mám ten dopis pro Janu", "pc", "find_file"),
        ("otevři mi kalkulačku", "pc", "open_app"),
        ("počítač je strašně pomalý", "pc", "slow_pc"),
        ("tiskárna netiskne", "pc", "printer"),
    ],
)
def test_routine_matches(text: str, kind: str, name: str):
    tools = PC if kind == "pc" else {"open_url"}
    assert routines.match(text, kind, tools).name == name


@pytest.mark.parametrize(
    "text",
    [
        "otevři mi seznam",
        "zavolej Lence",
        "zastav to video",
        "zapni mi na počítači tmavý režim",
        "najdi mi na internetu recept na bábovku",
        "dobrý den",
    ],
)
def test_no_routine(text: str):
    assert routines.match(text, "pc", PC) is None


def test_youtube_args():
    assert routines.youtube_args("pusť mi nejoblíbenější video od Vidraila") == {
        "query": "Vidrail",
        "order": "views",
        "channel": "Vidrail",
        "said_by": "od Vidraila",
    }
    assert routines.youtube_args("pusť mi na youtube nejnovější díl Ordinace")["order"] == "date"


@pytest.fixture
def fake_youtube(monkeypatch):
    asked: list[dict] = []

    async def search(
        query: str, order: str = "relevance", channel: str = "", limit: int = 6
    ) -> dict[str, Any]:
        asked.append({"query": query, "order": order, "channel": channel})
        return {
            "videos": [
                {"title": "Krtek a autíčko", "channel": "Krtek", "url": "https://www.youtube.com/watch?v=k1"}
            ]
        }

    monkeypatch.setitem(webtools.TOOLS["web_youtube_search"], "fn", search)
    return asked


async def test_youtube_on_pc_without_model(cfg, fake_youtube):
    vlm = ScriptedVlm()
    e = await Env(cfg, vlm).start(phone=False)
    r = await e.say("Pusť mi na počítači pohádku o krtkovi")
    assert r.screen == "hotovo" and "Krtek a autíčko" in r.say
    assert e.pc.opened == ["https://www.youtube.com/watch?v=k1"] and vlm.prompts == []


async def test_youtube_on_phone_opens_url(cfg, fake_youtube):
    e = await Env(cfg).start(pc=False)
    r = await e.say("Pusť mi na telefonu na youtube krtečka")
    assert r.screen == "hotovo"
    assert "open_url https://www.youtube.com/watch?v=k1" in e.phone.actions


async def test_downloads_moved_into_new_folder(cfg):
    e = await Env(cfg).start(phone=False)
    r = await e.say("Vytvoř mi na ploše složku pejsek a dej tam soubory, které jsem si stáhl dneska")
    assert "Mám začít" in r.say and "„pejsek“" in r.say
    r = await e.say("ano")
    assert r.screen == "hotovo" and "2 soubory" in r.say
    assert "create_folder desktop/pejsek" in e.pc.actions
    assert any(
        a.startswith("move_files downloads->desktop/pejsek") and "faktura.pdf" in a for a in e.pc.actions
    )
    assert "open_item desktop/pejsek/" in e.pc.actions


async def test_app_and_health_and_printer(cfg):
    e = await Env(cfg).start(phone=False)
    r = await e.say("Otevři mi kalkulačku")
    assert r.screen == "hotovo" and "start_app kalkulacku" in e.pc.actions
    r = await e.say("Počítač je strašně pomalý")
    assert r.screen == "hotovo" and "v pořádku" in r.say
    r = await e.say("Tiskárna netiskne")
    assert r.screen == "hotovo" and "HP DeskJet" in r.say


async def test_failed_routine_hands_over_to_agent(cfg):
    """Soubor nenalezen (mock find_files nic nevrátí) → agent pokračuje a ví, co už kód zkusil."""
    vlm = ScriptedVlm(lambda p: act("done", text="Dopis jsem otevřela."))
    e = await Env(cfg, vlm).start(phone=False)
    r = await e.say("Kde mám ten dopis pro Janu")
    assert r.screen == "hotovo" and "Dopis" in r.say
    assert "HelpButton already did by code" in vlm.prompts[0] and "find_files" in vlm.prompts[0]


async def test_failed_routine_without_model_gives_skill_guide(cfg):
    e = await Env(cfg).start(phone=False)
    r = await e.say("Kde mám ten dopis pro Janu")
    assert "nenašla" in r.say and r.guide == e.orch.skills.get("hledani-souboru").senior


async def test_photo_on_phone_is_taken_once(cfg):
    vlm = ScriptedVlm()
    e = await Env(cfg, vlm).start(pc=False)
    r = await e.say("Vyfoť to")
    assert r.screen == "hotovo" and "Galerii" in r.say
    assert e.phone.actions.count("take_photo") == 1 and vlm.prompts == []


async def test_unclear_answer_keeps_the_command(cfg):
    e = await Env(cfg).start(phone=False)
    r = await e.say("Vytvoř mi na ploše složku pejsek a dej tam soubory, které jsem si stáhl dneska")
    assert "Mám začít" in r.say
    r = await e.say("cože jak")
    assert r.expects_answer and "ano, nebo ne" in r.say and "pejsek" in r.say
    r = await e.say("no tak dobře")
    assert r.screen == "hotovo" and "create_folder desktop/pejsek" in e.pc.actions


async def test_three_unclear_answers_park_and_yes_resumes(cfg):
    e = await Env(cfg).start(phone=False)
    await e.say("Vytvoř mi na ploše složku pejsek a dej tam soubory, které jsem si stáhl dneska")
    for _ in range(2):
        r = await e.say("hm")
        assert r.expects_answer
    r = await e.say("eee")
    assert "Nechám to zatím být" in r.say and not e.pc.actions
    r = await e.say("zkus to znovu")
    assert r.screen == "hotovo" and "create_folder desktop/pejsek" in e.pc.actions


async def test_clear_no_still_cancels(cfg):
    e = await Env(cfg).start(phone=False)
    await e.say("Vytvoř mi na ploše složku pejsek a dej tam soubory, které jsem si stáhl dneska")
    r = await e.say("ne, nechci")
    assert "nic neměním" in r.say and not e.pc.actions


def test_yes_no_detection():
    from helpbutton.server.orchestrator import is_no, is_yes
    from helpbutton.common.safety import normalize as n

    for y in ("ano", "no tak dobře", "jo prosím", "jasně pusť to", "do toho", "určitě"):
        assert is_yes(n(y)) and not is_no(n(y)), y
    for no in ("ne", "ne prosím", "radši ne", "nechci", "zruš to"):
        assert is_no(n(no)) and not (is_yes(n(no)) and not is_no(n(no))), no
    for unclear in ("cože", "nevím", "hm", "eee"):
        assert not is_yes(n(unclear)) and not is_no(n(unclear)), unclear
