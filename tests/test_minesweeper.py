"""Hledání min: řešitel, čtení plochy ze snímku, herní smyčka a celá cesta od věty seniora po hru."""

from __future__ import annotations

import pytest
from PIL import Image, ImageDraw

from helpbutton.common import minesweeper as M
from helpbutton.server.brain import keyword_understand
from helpbutton.server.config import Config

from .conftest import Env

U, F = M.UNKNOWN, M.FLAG


def test_first_move_is_the_centre():
    sol = M.solve([[U] * 10 for _ in range(8)], 10)
    assert sol.guess == (4, 5) and not sol.safe


def test_simple_deductions():
    cells = [[1, U, U], [1, U, U], [0, 0, 0]]
    sol = M.solve(cells)
    assert (1, 1) in sol.safe and (1, 2) in sol.safe
    assert (0, 1) in sol.mines


def test_subset_rule():
    cells = [[1, 2, 1], [U, U, U]]
    sol = M.solve(cells)
    assert sol.mines == {(1, 0), (1, 2)} and sol.safe == {(1, 1)}


def test_flags_count_as_mines_and_are_never_clicked():
    cells = [[1, F], [U, U]]
    sol = M.solve(cells)
    assert sol.safe == {(1, 0), (1, 1)}


def test_guess_takes_lowest_probability():
    cells = [[U, U], [1, 1]]
    sol = M.solve(cells, total_mines=1)
    assert not sol.safe and sol.guess in {(0, 0), (0, 1)} and abs(sol.guess_p - 0.5) < 1e-9


@pytest.mark.parametrize(
    "size,cell,origin", [((10, 8), 30, (360, 140)), ((18, 14), 34, (200, 90)), ((24, 20), 25, (40, 60))]
)
def test_reader_matches_the_game(size, cell, origin):
    g = M.SimGame(*size, seed=3, cell=cell, origin=origin, screen=(1280, 720))
    g.click(origin[0] + cell * (size[0] // 2) + cell // 2, origin[1] + cell * (size[1] // 2) + cell // 2)
    b = M.read_board(g.render())
    assert b is not None and (b.cols, b.rows) == size
    assert abs(b.left - origin[0]) <= 1 and abs(b.top - origin[1]) <= 1
    for r in range(g.rows):
        for c in range(g.cols):
            expected = g.number(r, c) if (r, c) in g.opened else U
            assert b.cells[r][c] == expected, (r, c)


def test_reader_sees_flags_and_ignores_other_screens():
    g = M.SimGame(10, 8, seed=1)
    img = g.render()
    d = ImageDraw.Draw(img)
    x0, y0 = g.origin
    d.polygon([(x0 + 9, y0 + 6), (x0 + 22, y0 + 12), (x0 + 9, y0 + 18)], fill=M.FLAG_COLOR)
    b = M.read_board(img)
    assert b is not None and b.cells[0][0] == F
    assert M.read_board(Image.new("RGB", (1280, 720), (255, 255, 255))) is None
    stripes = Image.new("RGB", (800, 600), M.GREEN_SHADES[0])
    assert M.read_board(stripes) is None


def _play(g: M.SimGame, **kw):
    return M.play(g.render, g.click, max_seconds=300, settle=0, sleep=lambda _s: None, **kw)


def test_plays_whole_games_safely():
    wins = 0
    for seed in range(8):
        g = M.SimGame(10, 8, seed=seed)
        res = _play(g)
        truth = "won" if g.won else "lost"
        assert res["status"] == truth, (seed, res)
        assert g.outside_clicks == 0
        if truth == "lost":
            assert res["guesses"] >= 1
        wins += g.won
    assert wins >= 5


def test_never_guesses_when_forbidden():
    for seed in range(6):
        g = M.SimGame(18, 14, seed=seed, cell=28, origin=(100, 80))
        res = _play(g, allow_guess=False)
        assert g.exploded is None and res["status"] in ("won", "needs_guess")


def test_user_taking_the_mouse_stops_the_game():
    g = M.SimGame(10, 8, seed=2)
    res = _play(g, should_stop=lambda: bool(g.clicks))
    assert res["status"] == "stopped_by_user" and len(g.clicks) == 1


def test_no_board_on_screen():
    blank = Image.new("RGB", (1280, 720), (32, 33, 36))
    clicks: list[tuple[int, int]] = []
    res = M.play(lambda: blank, lambda x, y: clicks.append((x, y)), settle=0, sleep=lambda _s: None)
    assert res["status"] == "no_board" and clicks == []


@pytest.mark.parametrize(
    "text",
    ["Zahraj si hledání min", "Chci hrát minesweeper", "pusť mi hru miny", "zahraj minolovku na počítači"],
)
def test_requests_are_understood(text, tmp_path):
    u = keyword_understand(text, Config(data_dir=tmp_path))
    assert u.intent == "play_minesweeper" and u.target == "pc"


def test_question_about_rules_is_not_a_game(tmp_path):
    assert (
        keyword_understand("Jak se hraje hledání min?", Config(data_dir=tmp_path)).intent
        != "play_minesweeper"
    )


async def test_senior_asks_helpbutton_plays_official_google_game(env: Env):
    r = await env.say("Zahraj si hledání min")
    assert r.screen == "potvrzeni" and "Hledání min" in r.say and "Googlu" in r.say
    r = await env.say("ano")
    assert r.screen == "hotovo"
    assert env.pc.opened == [M.GAME_URL]
    assert env.pc.actions and env.pc.actions[-1].startswith("minesweeper ")
    status = env.pc.actions[-1].split()[1]
    assert r.say == M.summary_cs({"status": status})


def _pc_with_game(monkeypatch, title: str, game: M.SimGame, browser_opens_game: bool = True):
    import io
    import sys
    import types

    from helpbutton.agents import pc_gui
    from helpbutton.agents.pc import PcAgent

    mon = (1920, 0, 1280, 720)
    shown = {"game": not browser_opens_game}
    mouse = {"pos": (0, 0)}

    def screenshot():
        img = game.render() if shown["game"] else Image.new("RGB", (1280, 720), (255, 255, 255))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue(), mon

    def click(x, y, double=False, right=False, hover_s=0.12):
        mouse["pos"] = (x, y)
        game.click(x - mon[0], y - mon[1])

    agent = PcAgent("PC")
    opened: list[str] = []

    def open_browser(url, task="open"):
        opened.append(url)
        shown["game"] = True
        return {"opened": url}

    monkeypatch.setattr(pc_gui, "screenshot", screenshot)
    monkeypatch.setattr(pc_gui, "click", click)
    monkeypatch.setattr(pc_gui, "foreground_title", lambda: title)
    monkeypatch.setattr(pc_gui, "ui_snapshot", lambda *a, **k: ([], []))
    monkeypatch.setattr(agent, "open_and_fix_browser", open_browser)
    monkeypatch.setitem(sys.modules, "pyautogui", types.SimpleNamespace(position=lambda: mouse["pos"]))
    monkeypatch.setattr("time.sleep", lambda _s: None)
    return agent, opened


def test_pc_tool_opens_official_game_and_plays_on_the_right_monitor(monkeypatch):
    game = M.SimGame(10, 8, seed=4)
    agent, opened = _pc_with_game(monkeypatch, "Minesweeper - Google Chrome", game)
    res = agent.play_minesweeper(max_seconds=60)
    assert opened == [M.GAME_URL]
    assert res["status"] == ("won" if game.won else "lost") and game.outside_clicks == 0
    assert res["say"] == M.summary_cs(res)


def test_pc_tool_never_clicks_outside_a_browser_game_window(monkeypatch):
    game = M.SimGame(10, 8, seed=4)
    agent, _ = _pc_with_game(monkeypatch, "Obrázek.png - Malování", game, browser_opens_game=False)
    res = agent.play_minesweeper(max_seconds=10)
    assert res["status"] == "no_board" and game.clicks == []
