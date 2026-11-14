"""Chytré nástroje GUI agenta (click_text, type_into, scroll_to_text, wait_for_text, remember),
pracovní paměť během úkolu a zpřesnění kliku mřížkou – i pro malý model, který se netrefí do pixelů."""

from __future__ import annotations

import base64
import io
import json
import re
from typing import Any

import pytest

from helpbutton.agents.mock import SCREENS, MockPhone
from helpbutton.common.protocol import tool as device_tool
from helpbutton.server import agent as A
from helpbutton.server.agent import AgentSession, GuiAgent, Vlm

from .conftest import Env
from .test_agent import ScriptedVlm, act


def smart(name: str, **args: Any) -> dict[str, Any]:
    return act("tool", tool=name, tool_args_json=json.dumps(args, ensure_ascii=False))


async def _start(cfg, vlm, phone: MockPhone | None = None) -> Env:
    e = Env(cfg, vlm)
    if phone is not None:
        e.phone = phone
    await e.start()
    r = await e.say("Vyskočilo mi divné okno a nejde zavřít")
    assert "Mám začít" in r.say
    return e


async def test_click_text_clicks_by_visible_text(cfg):
    vlm = ScriptedVlm(
        lambda p: smart("click_text", text="zavrit"),
        lambda p: smart("click_text", text="Fotky"),
        lambda p: act("done", text="Fotky jsou otevřené."),
    )
    e = await _start(cfg, vlm)
    r = await e.say("ano")
    assert r.screen == "hotovo"
    assert e.phone.screen == "photos"
    assert "clicked [" in vlm.prompts[1] and "Zavřít" in vlm.prompts[1]


async def test_click_text_still_refuses_risky_buttons(cfg):
    vlm = ScriptedVlm(
        lambda p: smart("click_text", text="Zaplatit poštovné 99 Kč"),
        lambda p: act("done", text="Hotovo."),
    )
    e = await _start(cfg, vlm)
    await e.say("ano")
    assert "REFUSED" in vlm.prompts[1]
    assert e.phone.screen == "popup"


async def test_click_text_matches_label_inside_longer_text(cfg):
    vlm = ScriptedVlm(
        lambda p: smart("click_text", text="tlačítko Zavřít v rohu"),
        lambda p: act("done", text="Hotovo."),
    )
    e = await _start(cfg, vlm)
    await e.say("ano")
    assert e.phone.screen == "home" and "clicked [" in vlm.prompts[1]


async def test_click_text_not_found_suggests_similar_labels(cfg):
    vlm = ScriptedVlm(
        lambda p: smart("click_text", text="Zavřít"),
        lambda p: smart("click_text", text="Obchod s aplikacemi Google"),
        lambda p: act("done", text="Hotovo."),
    )
    e = await _start(cfg, vlm)
    await e.say("ano")
    assert e.phone.screen == "home"
    assert "NOT FOUND" in vlm.prompts[2] and "Similar texts on screen: 'Obchod Play'" in vlm.prompts[2]


async def test_type_into_finds_field_types_and_presses_enter(cfg):
    vlm = ScriptedVlm(
        lambda p: smart("click_text", text="Zavřít"),
        lambda p: smart("click_text", text="Obchod Play"),
        lambda p: smart("type_into", field="hledat", text="1234 5678"),
        lambda p: smart("type_into", field="Hledat aplikace", text="WhatsApp", enter=True),
        lambda p: act("done", text="Hledám WhatsApp."),
    )
    e = await _start(cfg, vlm)
    await e.say("ano")
    assert "REFUSED" in vlm.prompts[3]
    assert e.phone.typed == ["WhatsApp"]
    assert "KEY_PRESS enter" in e.phone.gui_log


async def test_remembered_facts_stay_in_every_prompt(cfg):
    vlm = ScriptedVlm(
        lambda p: smart("remember", fact="Hledaná fotka je z Prahy"),
        lambda p: smart("click_text", text="Zavřít"),
        lambda p: act("done", text="Hotovo."),
    )
    e = await _start(cfg, vlm)
    await e.say("ano")
    assert "Facts you know" not in vlm.prompts[0]
    for p in vlm.prompts[1:]:
        assert "Facts you know" in p and "Hledaná fotka je z Prahy" in p


async def test_wait_for_text_sees_current_screen_text_without_waiting(cfg):
    vlm = ScriptedVlm(
        lambda p: smart("wait_for_text", text="napaden virem", seconds=15),
        lambda p: act("done", text="Hotovo."),
    )
    e = await _start(cfg, vlm)
    await e.say("ano")
    assert "OK FOUND" in vlm.prompts[1] and "plain text" in vlm.prompts[1]


class ScrollPhone(MockPhone):
    """Telefon s dlouhým seznamem: „Soukromí“ je vidět až po posunutí dolů."""

    @device_tool("Provede jednu akci na obrazovce.")
    async def execute_action(
        self,
        action_type: str,
        target_element_id: int = 0,
        x: int = 0,
        y: int = 0,
        text_to_type: str = "",
        direction: str = "",
        key: str = "",
        package: str = "",
    ) -> dict[str, Any]:
        if action_type.upper() == "SWIPE":
            self.gui_log.append(f"SWIPE {direction}")
            if self.screen == "list_top" and direction == "down":
                self.screen = "list_bottom"
            return {"done": "SWIPE"}
        return await super().execute_action(
            action_type, target_element_id, x, y, text_to_type, direction, key, package
        )


async def test_scroll_to_text_swipes_until_the_text_appears(cfg, monkeypatch):
    monkeypatch.setitem(
        SCREENS["phone"],
        "list_top",
        [
            ("Displej", (0, 300, 1080, 420), "button", "display"),
            ("Zvuk a vibrace", (0, 420, 1080, 540), "button", "list_top"),
        ],
    )
    monkeypatch.setitem(
        SCREENS["phone"],
        "list_bottom",
        [
            ("Baterie", (0, 300, 1080, 420), "button", "list_bottom"),
            ("Soukromí", (0, 420, 1080, 540), "button", "settings"),
        ],
    )
    vlm = ScriptedVlm(
        lambda p: smart("scroll_to_text", text="soukromi"),
        lambda p: smart("click_text", text="Soukromí"),
        lambda p: act("done", text="Soukromí je otevřené."),
    )
    phone = ScrollPhone("Telefon babičky", delay=0)
    e = Env(cfg, vlm)
    e.phone = phone
    await e.start()
    await e.say("Vyskočilo mi divné okno a nejde zavřít")
    phone.screen = "list_top"
    await e.say("ano")
    assert "OK FOUND" in vlm.prompts[1] and "after 1 scroll" in vlm.prompts[1]
    assert phone.gui_log.count("SWIPE down") == 1
    assert phone.screen == "settings"


def test_small_model_writes_smart_tool_as_action():
    d = A._normalize({"action": "click_text", "target": "Odmítnout vše"})
    assert d["action"] == "tool" and d["tool"] == "click_text"
    assert json.loads(d["tool_args_json"]) == {"text": "Odmítnout vše"}
    d = A._normalize({"action": "type_into", "target": "Hledat", "text": "počasí"})
    assert json.loads(d["tool_args_json"]) == {"field": "Hledat", "text": "počasí"}
    d = A._normalize({"action": "remember", "text": "Soubor je v Dokumentech"})
    assert json.loads(d["tool_args_json"]) == {"fact": "Soubor je v Dokumentech"}
    d = A._normalize({"action": "tool", "tool": "scroll_to_text", "target": "Soukromí", "direction": "up"})
    assert json.loads(d["tool_args_json"]) == {"text": "Soukromí", "direction": "up"}


def test_find_element_tolerates_case_accents_and_czech_inflection():
    els = {
        1: {"label": "Odmítnout vše (Button)", "kind": "button"},
        2: {"label": "Přijmout vše (Button)", "kind": "button"},
        3: {"label": "Kanál Štístko a Poupěnka (Hyperlink)", "kind": "link"},
        4: {"label": "Hledat (Edit, focused)", "kind": "input"},
    }
    assert A._find_element(els, "odmitnout vse") == 1
    assert A._find_element(els, "PŘIJMOUT VŠE") == 2
    assert A._find_element(els, "na kanálu Štístko a Poupěnka") == 3
    assert A._find_element(els, "Úplně jiný text") == 0
    assert A._find_input(els, "") == 4
    assert A._find_input(els, "hledat") == 4


def test_working_memory_dedupes_limits_and_hides_codes():
    s = AgentSession("úkol", device=None)
    for i in range(15):
        s.add_fact(f"fakt {i}")
    s.add_fact("fakt 14")
    assert len(s.facts) == A.MAX_FACTS and s.facts[0] == "fakt 5" and s.facts[-1] == "fakt 14"
    s.add_fact("Kód z SMS je 482913")
    assert "482913" not in s.facts[-1]


def test_tool_results_become_facts():
    s = AgentSession("úkol", device=None)
    A._auto_facts(
        s,
        "find_files",
        {"found": [{"name": "dopis.docx"}, {"name": "dopis2.docx"}], "folder": "C:/Users/senior/Documents"},
    )
    assert "find_files: folder = C:/Users/senior/Documents" in s.facts
    assert "find_files: found = dopis.docx, dopis2.docx" in s.facts


ICON = (1236, 371)


def _screenshot() -> str:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (1600, 900), (236, 236, 236))
    d = ImageDraw.Draw(img)
    d.rectangle((40, 40, 1560, 860), outline=(200, 200, 200), width=2)
    d.rectangle((ICON[0] - 11, ICON[1] - 11, ICON[0] + 11, ICON[1] + 11), fill=(220, 20, 20))
    buf = io.BytesIO()
    img.save(buf, "WEBP", quality=90)
    return base64.b64encode(buf.getvalue()).decode()


class GridVlm(Vlm):
    """Model, který umí jen ukázat políčko: najde červenou ikonu ve zvětšeném výřezu a řekne jeho číslo."""

    name = "grid"

    def __init__(self, answer: int | None = None):
        self.asks = 0
        self.answer = answer

    async def decide(self, system: str, text: str, image_b64: str | None) -> dict[str, Any]:
        raise AssertionError("zpřesnění mřížkou nemá volat decide")

    async def ask(
        self, system: str, text: str, image_b64: str | None, schema: dict[str, Any]
    ) -> dict[str, Any]:
        import numpy as np
        from PIL import Image

        self.asks += 1
        if self.answer is not None:
            return {"cell": self.answer}
        n = int(re.search(r"numbered 1-(\d+)", text).group(1))
        cols = rows = round(n**0.5)
        img = np.asarray(Image.open(io.BytesIO(base64.b64decode(image_b64))).convert("RGB")).astype(int)
        mask = (img[:, :, 0] > 170) & (img[:, :, 1] < 90) & (img[:, :, 2] < 90)
        ys, xs = np.nonzero(mask)
        if xs.size == 0:
            return {"cell": 0}
        col = int(xs.mean() // (img.shape[1] / cols))
        row = int(ys.mean() // (img.shape[0] / rows))
        return {"cell": row * cols + col + 1}


async def test_grid_zoom_lands_on_small_icon():
    vlm = GridVlm()
    d = {"x": 1180, "y": 410, "target": "ikona nastavení"}
    await GuiAgent(None, vlm)._zoom(d, {"screenshot_webp_base64": _screenshot()}, {})
    assert abs(d["x"] - ICON[0]) <= 3 and abs(d["y"] - ICON[1]) <= 3
    assert vlm.asks == 2


async def test_grid_zoom_snaps_to_known_element_in_cell():
    vlm = GridVlm()
    d = {"x": 1180, "y": 410, "target": "Nastavení"}
    elements = {
        7: {
            "id": 7,
            "label": "Nastavení",
            "kind": "button",
            "bounds": [ICON[0] - 11, ICON[1] - 11, ICON[0] + 11, ICON[1] + 11],
        },
        8: {"id": 8, "label": "Okno", "kind": "pane", "bounds": [40, 40, 1560, 860]},
    }
    await GuiAgent(None, vlm)._zoom(d, {"screenshot_webp_base64": _screenshot()}, elements)
    assert (d["x"], d["y"]) == ICON
    assert vlm.asks == 1


async def test_grid_zoom_keeps_estimate_when_target_not_visible():
    d = {"x": 300, "y": 700, "target": "ikona, která tu není"}
    await GuiAgent(None, GridVlm(answer=0))._zoom(d, {"screenshot_webp_base64": _screenshot()}, {})
    assert (d["x"], d["y"]) == (300, 700)


def test_grid_cells_cover_the_crop_exactly():
    from PIL import Image

    b64, cells = A._grid_crop(Image.new("RGB", (1600, 900), (255, 255, 255)), (100, 50, 633, 380), 4, 4)
    assert len(cells) == 16 and base64.b64decode(b64)[:4] == b"RIFF"
    assert cells[0][:2] == (100, 50) and cells[-1][2:] == (633, 380)
    for a, b in zip(cells, cells[1:]):
        if a[1] == b[1]:
            assert a[2] == b[0]


@pytest.mark.parametrize("box", [(-50, -50, 300, 200), (1500, 800, 300, 200), (10, 10, 5000, 5000)])
def test_clamp_box_stays_on_screen(box):
    x1, y1, x2, y2 = A._clamp_box(*box, 1600, 900)
    assert 0 <= x1 < x2 <= 1600 and 0 <= y1 < y2 <= 900
