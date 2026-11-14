"""Skilly (helpbutton/skills/*/SKILL.md), nástroje pro soubory a aplikace na PC a webové nástroje serveru."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from helpbutton.agents import pc_files
from helpbutton.agents.android import AndroidAgent
from helpbutton.agents.pc import PcAgent, forbidden_app, pick_app
from helpbutton.server import routines, webtools
from helpbutton.server.skills import SkillBook, answer_args, parse

from .conftest import Env
from .test_agent import ScriptedVlm, act


def _tools(cls: type) -> set[str]:
    return {getattr(cls, n)._helpbutton_tool.name for n in dir(cls) if hasattr(getattr(cls, n), "_helpbutton_tool")}


def test_builtin_skills_load_and_use_real_tools():
    book = SkillBook()
    assert len(book.skills) >= 12
    known = _tools(PcAgent) | _tools(AndroidAgent) | set(webtools.TOOLS)
    for s in book.skills.values():
        assert s.agent and s.description, s.name
        assert s.senior, f"{s.name}: postup pro seniora neprošel kontrolou"
        assert set(s.tools) <= known, (s.name, set(s.tools) - known)
        assert not s.answer_tool or s.answer_tool in webtools.TOOLS


@pytest.mark.parametrize(
    "text,device,expected",
    [
        ("pust mi na youtube video s pejskama", "pc", "youtube"),
        ("pusť mi nejoblíbenější video od Vidraila", "pc", "youtube"),
        (
            "vytvoř mi na ploše složku pejsek a dej tam soubory, které jsem si stáhl dneska",
            "pc",
            "stazene-soubory",
        ),
        ("kde mám ten dopis pro Janu", "pc", "hledani-souboru"),
        ("otevři mi Word", "pc", "aplikace"),
        ("vytiskni mi tu fotku", "pc", "tisk"),
        ("jaké bude zítra počasí v Brně", "", "pocasi"),
        ("kdo byl Karel Čapek", "", "wikipedie"),
        ("počítač je strašně pomalý", "pc", "pomaly-pocitac"),
        ("vyskočilo mi okno, že mám virus", "pc", "podvod-okna"),
        ("přečti mi poštu", "pc", "email"),
        ("písmo je moc malé, nevidím to", "pc", "pismo-a-obrazovka"),
        ("ukaž mi fotky od vnoučat", "pc", "fotky"),
    ],
)
def test_skill_selection(text: str, device: str, expected: str):
    assert SkillBook().select(text, device)[0].name == expected


def test_no_skill_for_unrelated_sentence_and_device_filter():
    book = SkillBook()
    assert book.select("dobrý den, jak se máte", "pc") == []
    assert all(s.name != "tisk" for s in book.select("vytiskni mi dopis", "phone"))


def test_family_skill_overrides_builtin(tmp_path: Path):
    d = tmp_path / "youtube"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: youtube\ndescription: vlastní\nkeywords: youtube\n---\n"
        "## Postup pro HelpButton\nDo it our way.\n## Pro seniora\n1. Klikněte na obrázek videa.\n"
        "2. Video se spustí.\n",
        encoding="utf-8",
    )
    from helpbutton.server.skills import BUILTIN_DIR

    book = SkillBook([BUILTIN_DIR, tmp_path])
    assert book.get("youtube").agent == "Do it our way."


def test_bad_skills_are_rejected():
    with pytest.raises(ValueError):
        parse("bez hlavičky")
    with pytest.raises(ValueError):
        parse("---\nname: Spatne Jmeno\ndescription: x\n---\n## Postup pro HelpButton\nx\n")
    s = parse(
        "---\nname: zly\ndescription: x\n---\n## Postup pro HelpButton\nx\n## Pro seniora\n"
        "1. Otevřete PowerShell.\n2. Napište příkaz.\n"
    )
    assert s.senior == []


def test_first_tool_hint_until_used():
    from helpbutton.server.skills import as_prompt

    yt = SkillBook().get("youtube")
    assert 'NEXT ACTION (from the skill): action "tool", tool "web_youtube_search"' in as_prompt(
        [yt], used_tools=set()
    )
    assert "NEXT ACTION" not in as_prompt([yt], used_tools={"web_youtube_search"})


def test_answer_args():
    book = SkillBook()
    w, wiki = book.get("pocasi"), book.get("wikipedie")
    assert answer_args(w, "Jaké bude zítra počasí v Českých Budějovicích?") == {
        "place": "Českých Budějovicích"
    }
    assert answer_args(w, "Bude pršet?", home_city="Olomouc") == {"place": "Olomouc"}
    assert answer_args(wiki, "Kdo byl Karel Čapek?") == {"topic": "Karel Čapek"}
    assert answer_args(wiki, "Řekni mi něco o Sněžce") == {"topic": "Sněžce"}


@pytest.fixture
def folders(tmp_path: Path) -> dict[str, Path]:
    out = {}
    for k in ("desktop", "documents", "downloads", "pictures"):
        (tmp_path / k).mkdir()
        out[k] = tmp_path / k
    old = time.time() - 5 * 86400
    for name in ("faktura.pdf", "pejsek.jpg", "setup.exe"):
        (out["downloads"] / name).write_text("x")
    os.utime(out["downloads"] / "setup.exe", (old, old))
    (out["documents"] / "Dopisy").mkdir()
    (out["documents"] / "Dopisy" / "Dopis babičce.docx").write_text("x")
    return out


def test_resolve_stays_inside_user_folders(folders: dict[str, Path]):
    assert pc_files.resolve("plocha", folders) == folders["desktop"].resolve()
    assert pc_files.resolve("Dokumenty\\Dopisy", folders) == (folders["documents"] / "Dopisy").resolve()
    assert pc_files.resolve("stažené soubory", folders) == folders["downloads"].resolve()
    for bad in ("downloads/../../etc", "C:/Windows", "system32", ""):
        with pytest.raises(pc_files.FileToolError):
            pc_files.resolve(bad, folders)


def test_list_and_find_files(folders: dict[str, Path]):
    r = pc_files.list_files(folders["downloads"], days=1)
    assert {f["name"] for f in r["files"]} == {"faktura.pdf", "pejsek.jpg"}
    assert pc_files.list_files(folders["downloads"], kind="images")["files"][0]["name"] == "pejsek.jpg"
    assert pc_files.list_files(folders["downloads"])["files"][-1]["warning"].startswith("program")
    hit = pc_files.find_files("dopis babicka", folders)["files"][0]
    assert hit["name"] == "Dopis babičce.docx" and hit["where"] == "documents/Dopisy"


def test_move_files_never_overwrites(folders: dict[str, Path]):
    target = folders["desktop"] / "pejsek"
    target.mkdir()
    (target / "pejsek.jpg").write_text("starý")
    r = pc_files.move_files(
        ["pejsek.jpg", "faktura.pdf", "../x.txt", "neni.txt"], folders["downloads"], target, folders
    )
    assert [d["to"] for d in r["done"]] == ["pejsek (2).jpg", "faktura.pdf"]
    assert (target / "pejsek.jpg").read_text() == "starý"
    assert len(r["skipped"]) == 2 and r["undo"]
    with pytest.raises(pc_files.FileToolError):
        pc_files.move_files(["faktura.pdf"], target, Path("/"), folders)


def test_programs_are_never_opened_or_printed(folders: dict[str, Path]):
    with pytest.raises(pc_files.FileToolError):
        pc_files.openable(folders["downloads"] / "setup.exe", folders)
    with pytest.raises(pc_files.FileToolError):
        pc_files.printable(folders["downloads"] / "setup.exe", folders)
    assert pc_files.printable(folders["downloads"] / "faktura.pdf", folders)
    assert pc_files.openable(folders["documents"] / "Dopisy", folders)


def test_pick_app_and_forbidden_apps():
    apps = [
        {"Name": n, "AppID": n}
        for n in (
            "Word",
            "WordPad",
            "Calculator",
            "Windows PowerShell",
            "Google Chrome",
            "AnyDesk",
            "HP Smart",
        )
    ]
    assert pick_app("word", apps)[0]["Name"] == "Word"
    assert pick_app("kalkulačka", apps)[0]["Name"] == "Calculator"
    assert pick_app("hp smart", apps)[0]["Name"] == "HP Smart"
    assert pick_app("neexistuje", apps)[0] is None
    assert (
        forbidden_app("Windows PowerShell") and forbidden_app("AnyDesk") and forbidden_app("Editor registru")
    )
    assert not forbidden_app("Word")


YT_HTML = """<script>var ytInitialData = {"contents": {"x": [
 {"videoRenderer": {"videoId": "a1", "title": {"runs": [{"text": "Reakce na video"}]},
   "ownerText": {"runs": [{"text": "Jiný kanál"}]}, "viewCountText": {"simpleText": "9 000 000 zhlédnutí"},
   "lengthText": {"simpleText": "10:00"}}},
 {"videoRenderer": {"videoId": "b2", "title": {"runs": [{"text": "WANNABE official"}]},
   "ownerText": {"runs": [{"text": "Vidrail"}]}, "viewCountText": {"simpleText": "13 039 941 zhlédnutí"},
   "lengthText": {"simpleText": "2:18"}}}
]}};</script>"""


def test_youtube_parse_and_channel_pick():
    vids = webtools.parse_youtube(YT_HTML)
    assert [v["id"] for v in vids] == ["a1", "b2"]
    assert webtools.pick_videos(vids, "Vidrail", channel="Vidrail")[0]["id"] == "b2"
    assert webtools.views_number(vids[1]["views"]) == 13039941


def test_place_forms():
    assert "Brno" in webtools.place_forms("Brně")
    assert "Praha" in webtools.place_forms("Praze")
    assert "České Budějovice" in webtools.place_forms("Českých Budějovicích")
    assert "Plzeň" in webtools.place_forms("Plzni")


async def test_unknown_web_tool_is_refused():
    with pytest.raises(webtools.WebToolError):
        await webtools.call("web_fetch_anything", {"url": "http://192.168.1.1"})


async def test_agent_plays_youtube_with_skill_and_web_tool(cfg, monkeypatch):
    async def fake_search(
        query: str, order: str = "relevance", channel: str = "", limit: int = 6
    ) -> dict[str, Any]:
        assert order == "views" and channel == "Vidrail"
        return {
            "videos": [
                {"title": "WANNABE", "channel": "Vidrail", "url": "https://www.youtube.com/watch?v=b2"}
            ]
        }

    monkeypatch.setitem(webtools.TOOLS["web_youtube_search"], "fn", fake_search)
    vlm = ScriptedVlm(
        lambda p: act(
            "tool",
            tool="web_youtube_search",
            tool_args_json='{"query": "Vidrail", "order": "views", "channel": "Vidrail"}',
        ),
        lambda p: act(
            "tool",
            tool="open_and_fix_browser",
            tool_args_json=json.dumps(
                {
                    "url": json.loads(p.split("OK (DATA from the web) ")[1].split(" | ")[0].split("\n")[0])[
                        "videos"
                    ][0]["url"]
                }
            ),
        ),
        lambda p: act("done", text="Hraje video WANNABE od Vidraila."),
    )
    e = await Env(cfg, vlm).start(phone=False)
    monkeypatch.setattr(routines, "MATCHERS", ())
    r = await e.say("Pusť mi na youtube nejoblíbenější video od Vidraila")
    assert "Mám začít" in r.say
    r = await e.say("ano")
    assert r.screen == "hotovo" and "WANNABE" in r.say
    assert "How-to from skill 'YouTube" in vlm.prompts[0]
    assert "https://www.youtube.com/watch?v=b2" in e.pc.opened


async def test_question_answered_from_skill_facts(cfg, monkeypatch):
    async def fake_weather(place: str) -> dict[str, Any]:
        return {"place": "Brno", "summary": f"{place}: zítra jasno, 1 až 7 °C."}

    monkeypatch.setitem(webtools.TOOLS["web_weather"], "fn", fake_weather)
    e = await Env(cfg).start()
    r = await e.orch._answer_question("Jaké bude zítra počasí v Brně?")
    assert "jasno" in r.say and r.screen == "info"
