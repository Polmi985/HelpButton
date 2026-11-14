"""Obrazovka nepochopená (log 13/11/2026): useknutý JSON, přetečený kontext, klik na špatný prvek,
a co senior uslyší a uvidí, když to HelpButton nedokončí."""

from __future__ import annotations

import base64
import io
import json
from typing import Any

import httpx
from PIL import Image

from helpbutton.server import guide
from helpbutton.server.agent import (
    POS_HEADER,
    AgentSession,
    AgentStep,
    GuiAgent,
    OllamaVlm,
    Vlm,
    _ground,
    _salvage,
    est_tokens,
)

from .conftest import Env


def _img() -> str:
    buf = io.BytesIO()
    Image.new("RGB", (1600, 900)).save(buf, "WEBP")
    return base64.b64encode(buf.getvalue()).decode()


def test_salvage_truncated_json():
    """Odpověď useknutá v „notes“ – akce už v ní je (pole akce jsou ve schématu na začátku)."""
    cut = (
        '{\n  "thought": "The video is playing.",\n  "action": "done",\n  "target": "",\n  "element_id": 0,\n'
        '  "x": 0,\n  "y": 0,\n  "text": "Video hraje.",\n  "direction": "",\n  "key": "",\n  "package": "",\n'
        '  "tool": "",\n  "tool_args_json": "",\n  "say": "",\n  "notes": "1. Opened YouTube sear'
    )
    d = _salvage(cut)
    assert d and d["action"] == "done" and d["text"] == "Video hraje."
    assert _salvage('{\n  "thought": "The first video in the search results is the') is None
    assert (
        _salvage(
            '{"thought": "t", "action": "type", "target": "Hledat", "element_id": 3, "x": 0, "y": 0, '
            '"text": "pejsci a ko'
        )
        is None
    )


async def test_ollama_grows_context_when_answer_is_cut(monkeypatch):
    """Oba pokusy useknuté na stejných 60 znacích = došel kontext (done_reason=length), ne num_predict."""
    sent: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        sent.append(body)
        if body["options"]["num_ctx"] <= 8192:
            return httpx.Response(
                200,
                json={
                    "message": {"content": '{\n  "thought": "The first video in the'},
                    "done_reason": "length",
                    "eval_count": 15,
                    "prompt_eval_count": 8177,
                },
            )
        return httpx.Response(
            200,
            json={
                "message": {"content": '{"action": "click", "element_id": 4}'},
                "done_reason": "stop",
                "eval_count": 30,
            },
        )

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    v = OllamaVlm("http://x", "qwen3-vl:8b-instruct", num_ctx=8192)
    d = await v.decide("s", "t", _img())
    assert d["element_id"] == 4
    assert [b["options"]["num_ctx"] for b in sent] == [8192, 16384]
    assert v.num_ctx == 16384


def test_prompt_shrinks_to_fit_budget():
    """100 prvků + 40 textů + 12 kroků se do lokálního kontextu nevešlo – prompt se má zkrátit sám."""
    s = AgentSession(task="Na YouTube pustit video s pejskem", device=None)  # type: ignore[arg-type]
    s.steps = [AgentStep("why " * 20, f"click [{i}]", "OK " + "x" * 280) for i in range(12)]
    obs = {
        "active_app": "YouTube - Google Chrome",
        "image_size": [1600, 900],
        "interactive_elements": [
            {
                "id": i,
                "label": f"Video číslo {i} o pejscích a kočičkách (Hyperlink)",
                "kind": "button",
                "bounds": [10 * i, 300, 10 * i + 200, 340],
            }
            for i in range(1, 101)
        ],
        "screen_text": [f"Popis videa {i} " * 6 for i in range(40)],
    }
    full = GuiAgent._prompt(s, obs)
    short = GuiAgent._prompt(s, obs, budget=3000)
    assert est_tokens(full) > 3000 >= est_tokens(short)
    assert "Senior's task" in short and "Step 13." in short
    assert "@110,320" in full and "[10, 300, 210, 340]" not in full


def test_relative_positions_for_qwen3():
    text = (
        POS_HEADER.format(w=1600, h=900) + "\n  [3] Hledat (Edit) @800,450\n  [4] Video (Hyperlink) @400,90"
    )
    out = OllamaVlm("http://x", "qwen3-vl:8b-instruct")._to_relative(text, (1600, 900))
    assert "@500,500" in out and "@250,100" in out and "relative 0-1000" in out


def test_ground_fixes_or_refuses_wrong_element():
    """„first video 'Štístko a Poupěnka' is clickable“ → click [1] „Vyhledávání v kartách“."""
    els = {
        1: {"label": "Vyhledávání v kartách (Button, collapsed)"},
        2: {"label": "Hledat (Edit)"},
        3: {"label": "Štístko a Poupěnka - Můj pejsek (Hyperlink)"},
    }
    d = {"element_id": 1, "target": "Štístko a Poupěnka - Můj pejsek", "x": 0, "y": 0}
    assert _ground(d, els, []) == "fixed" and d["element_id"] == 3
    d = {"element_id": 1, "target": "Štístko a Poupěnka - Můj pejsek", "x": 0, "y": 0}
    assert _ground(d, {1: els[1]}, ["Štístko a Poupěnka - Můj pejsek", "1,2 mil. zhlédnutí"]) == "wrong"
    d = {"element_id": 1, "target": "Štístko a Poupěnka - Můj pejsek", "x": 400, "y": 300}
    assert _ground(d, {1: els[1]}, ["Štístko a Poupěnka - Můj pejsek"]) == "fixed" and d["element_id"] == 0
    d = {"element_id": 2, "target": "search box", "x": 0, "y": 0}
    assert _ground(d, els, []) == "doubtful" and d["element_id"] == 2
    d = {"element_id": 2, "target": "Hledat", "x": 0, "y": 0}
    assert _ground(d, els, []) == "ok"


class Broken(Vlm):
    """Obrazovku nepochopí (useknutý JSON), ale návod ze snímku napsat umí."""

    name = "broken"

    def __init__(self, guides: list[list[str]] | None = None):
        self.guides = list(guides or [])
        self.asked: list[str] = []

    async def decide(self, system: str, text: str, image_b64: str | None) -> dict[str, Any]:
        raise ValueError("model nevrátil platný JSON ani na druhý pokus")

    async def ask(
        self, system: str, text: str, image_b64: str | None, schema: dict[str, Any]
    ) -> dict[str, Any]:
        assert image_b64
        self.asked.append(text)
        if not self.guides:
            raise NotImplementedError
        return {"steps": self.guides.pop(0)}


async def test_failed_agent_writes_guide_from_screen(cfg):
    """Když to HelpButton nedokončí: co je hotové (z faktů) + postup ze snímku. Vymyšlený nápis → oprava."""
    invented = ["Klikněte levým tlačítkem myši na „Přehrát vše“.", "Video se spustí."]
    real = [
        "Dvakrát rychle klikněte levým tlačítkem myši na ikonu „Google Chrome“ vlevo nahoře.",
        "Nahoře do bílého řádku napište, co hledáte, a zmáčkněte Enter.",
        "Uvidíte výsledky hledání.",
    ]
    vlm = Broken([invented, real])
    e = await Env(cfg, vlm).start(phone=False)
    r = await e.say("Najdi mi na internetu recept na bábovku")
    assert "Mám začít" in r.say
    r = await e.say("ano")
    assert "nevyznala" in r.say and "Petrovi" in r.say and "jak to dokončit" in r.say
    assert "Otevřela jsem stránku" in r.say
    assert r.guide == real
    assert "„Přehrát vše“" in vlm.asked[1] and "NEJSOU" in vlm.asked[1]
    r = await e.say("ne")
    assert r.guide == real and "nepíšu" in r.say


async def test_failed_agent_without_any_guide_model(cfg):
    """Bez modelu na návod: ověřený postup ze skillu (prohlížeč); bez skillu obecná rada – nic si nevymýšlí."""
    e = await Env(cfg, Broken()).start(phone=False)
    await e.say("Najdi mi na internetu recept na bábovku")
    r = await e.say("ano")
    assert r.guide == e.orch.skills.get("prohlizec").senior
    e.orch.skills.skills.clear()
    await e.say("Najdi mi na internetu recept na bábovku")
    r = await e.say("ano")
    assert r.guide and "nic jsem nesmazala" in r.guide[0]


def test_guide_progress_and_checks():
    opened = [
        AgentStep(
            "",
            'tool open_and_fix_browser({"url": "https://www.youtube.com/results?search_query=' 'pejsek"})',
            'OK {"opened": "...", "window": "pejsek - YouTube - Google Chrome"}',
        )
    ]
    assert guide.progress(opened) == "Otevřela jsem stránku pejsek - YouTube."
    folder = [
        AgentStep(
            "",
            'tool create_folder({"name": "pejsek", "where": "desktop"})',
            'OK {"path": "C:\\Users\\x\\Desktop\\pejsek", "created": true, "existed": false}',
        )
    ]
    assert guide.progress(folder) == "Vytvořila jsem na ploše složku „pejsek“."
    assert guide.clean({"steps": ["Otevřete PowerShell.", "Napište příkaz mkdir."]}) is None
    assert guide.clean({"steps": ["1. Klikněte na „Start“.", "2) Vyberte „Nastavení“."]}) == [
        "Klikněte na „Start“.",
        "Vyberte „Nastavení“.",
    ]
    loop = [
        "Vlevo nahoře je tlačítko s lupou, které je na obrazovce. Klikněte na to, abyste otevřeli nabídku Start."
    ]
    loop += ["Vlevo nahoře je tlačítko s lupou. Klikněte na to, abyste otevřeli nabídku Start."] * 6
    assert guide.clean({"steps": loop}) is None
    labels = ["Soubor", "Domů", "Tisk", "Stažené soubory"]
    assert guide.missing_names(["Klikněte na „Soubor“, pak na „Tisk“."], labels) == []
    assert guide.missing_names(["Klikněte na „Vytisknout fotku“."], labels) == ["Vytisknout fotku"]


def test_ocr_segments_split_toolbar_and_drop_noise():
    from helpbutton.agents import pc_gui

    line = [("Soubor", (10, 5, 60, 20)), ("Domů", (90, 5, 130, 20)), ("Vložení", (160, 5, 220, 20))]
    title = [
        ("Štístko", (300, 200, 360, 216)),
        ("a", (364, 200, 372, 216)),
        ("Poupěnka", (376, 200, 450, 216)),
    ]
    noise = [("•", (5, 300, 9, 304)), ("»>", (20, 300, 30, 310))]
    segs = pc_gui.ocr_segments([line, title, noise])
    assert [t for t, _b in segs] == ["Soubor", "Domů", "Vložení", "Štístko a Poupěnka"]
    assert segs[3][1] == (300, 200, 450, 216)


def test_merge_ocr_adds_only_new_text():
    from helpbutton.agents import pc_gui
    from helpbutton.common import som

    button = som.Element(0, "Tisknout (Button)", (100, 100, 200, 130))
    window = som.Element(0, "Hlavní plocha (Pane)", (0, 0, 1000, 800))
    segs = [
        ("Tisknout", (120, 108, 180, 122)),
        ("Tisk fotografií", (300, 400, 420, 416)),
        ("Kód 482913", (300, 500, 420, 516)),
    ]
    out = pc_gui.merge_ocr([button, window], segs, focus=(0, 0, 1000, 800))
    added = [e for e in out if e.label.endswith("(Text, OCR)")]
    assert [e.label for e in added] == ["Tisk fotografií (Text, OCR)", "Kód (kód skrytý) (Text, OCR)"]
    assert all(e.priority == pc_gui.PRIO_DOCUMENT for e in added)


async def test_zoom_refines_xy_click(cfg):
    """Klik na x,y mimo prvky: výřez 2× zvětšený, model cíl najde znovu, klik se posune."""
    from helpbutton.server.agent import GuiAgent

    class Zoomer(Vlm):
        name = "zoomer"
        seen: list[str] = []

        async def decide(self, system: str, text: str, image_b64: str | None) -> dict[str, Any]:
            self.seen.append(system)
            return {"action": "click", "x": 300, "y": 200}

    agent = GuiAgent(None, Zoomer())  # type: ignore[arg-type]
    d = {"x": 800, "y": 450, "target": "ikona tiskárny"}
    await agent._zoom(d, {"screenshot_webp_base64": _img()})
    assert (d["x"], d["y"]) == (750, 430)
    assert "magnified 2x" in Zoomer.seen[0]
