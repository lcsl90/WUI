"""Höhe des unteren Bereichs (Terminal) mit der Maus am oberen Rand ziehen."""
from __future__ import annotations

from pathlib import Path

import wslui
from conftest import FakeClipboard, wait_until


async def open_terminal(pilot, app: wslui.WSLUI) -> wslui.Dock:
    await wait_until(pilot, lambda: len(app.screen.panes) == 1)
    await pilot.press("ctrl+t")
    await wait_until(pilot, lambda: app.screen.term.running and app.screen.dock.region.height > 0)
    return app.screen.dock


def make_app(folder: Path, clipboard: FakeClipboard) -> wslui.WSLUI:
    app = wslui.WSLUI(folder, clipboard=clipboard)
    app.cfg["layout"] = "1"
    return app


async def drag_top_edge(pilot, dock: wslui.Dock, rows: int) -> None:
    """Zieht den oberen Rand um `rows` Zeilen (negativ = nach oben = größer)."""
    y = dock.region.y
    target = min(max(y + rows, 0), pilot.app.size.height - 1)
    await pilot.mouse_down(offset=(10, y))
    await pilot.hover(offset=(10, target))
    await pilot.mouse_up(offset=(10, target))
    await pilot.pause()


async def test_dragging_the_top_edge_resizes_the_terminal_and_is_remembered(
    tmp_path: Path, clipboard: FakeClipboard
) -> None:
    app = make_app(tmp_path, clipboard)

    async with app.run_test(size=(120, 40)) as pilot:
        dock = await open_terminal(pilot, app)
        term = app.screen.term
        before, bottom = dock.region.height, dock.region.bottom

        await drag_top_edge(pilot, dock, -7)

        assert dock.region.height == before + 7
        assert dock.region.bottom == bottom
        assert term.pscreen.lines == term.size.height == before + 7 - 2  # die Shell kennt die neue Größe
        assert app.cfg["dock_height"] == before + 7

        await drag_top_edge(pilot, dock, 10)
        assert dock.region.height == before - 3
        assert clipboard.copied == []  # Ziehen am Rand markiert keinen Text
        term.stop()


async def test_height_is_limited_so_files_and_terminal_stay_usable(tmp_path: Path, clipboard: FakeClipboard) -> None:
    app = make_app(tmp_path, clipboard)

    async with app.run_test(size=(120, 40)) as pilot:
        dock = await open_terminal(pilot, app)

        await drag_top_edge(pilot, dock, -100)
        assert 30 <= dock.region.height <= 34  # höchstens 85 % …
        assert app.screen.panes[0].region.height >= 4  # … die Dateifenster bleiben sichtbar

        await drag_top_edge(pilot, dock, 100)
        assert dock.region.height == wslui.Dock.MIN_ROWS
        app.screen.term.stop()


async def test_saved_height_is_used_on_the_next_start(tmp_path: Path, clipboard: FakeClipboard) -> None:
    app = make_app(tmp_path, clipboard)
    app.cfg["dock_height"] = 12

    async with app.run_test(size=(120, 40)) as pilot:
        dock = await open_terminal(pilot, app)

        assert dock.region.height == 12
        app.screen.term.stop()


async def test_ctrl_up_enlarges_and_ctrl_down_returns_to_the_dragged_height(
    tmp_path: Path, clipboard: FakeClipboard
) -> None:
    app = make_app(tmp_path, clipboard)
    app.cfg["dock_height"] = 12

    async with app.run_test(size=(120, 40)) as pilot:
        dock = await open_terminal(pilot, app)

        await pilot.press("ctrl+up")
        await pilot.pause()
        assert 28 <= dock.region.height <= 30  # 75 %

        await pilot.press("ctrl+down")
        await pilot.pause()
        assert dock.region.height == 12
        app.screen.term.stop()


async def test_hovering_the_top_edge_highlights_it(tmp_path: Path, clipboard: FakeClipboard) -> None:
    app = make_app(tmp_path, clipboard)

    async with app.run_test(size=(120, 40)) as pilot:
        dock = await open_terminal(pilot, app)

        await pilot.hover(offset=(10, dock.region.y))
        assert dock.has_class("-grip")

        await pilot.hover(offset=(10, dock.region.y + 3))
        assert not dock.has_class("-grip")
        app.screen.term.stop()
