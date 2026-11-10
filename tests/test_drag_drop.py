"""Drag & Drop zwischen Dateifenstern: Verschieben und Kopieren mit der Maus."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import wslui
from conftest import FakeClipboard, wait_for_listing, wait_until


@pytest.fixture
def folders(tmp_path: Path, isolated_config: Path) -> tuple[Path, Path]:
    left, right = tmp_path / "left", tmp_path / "right"
    left.mkdir()
    right.mkdir()
    cfg = {"layout": "2", "panes": [{"path": str(left)}, {"path": str(right)}]}
    (isolated_config / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    return left, right


async def two_panes(pilot, left: list[str], right: list[str]) -> tuple[wslui.Pane, wslui.Pane]:
    screen = pilot.app.screen
    await wait_until(pilot, lambda: len(screen.panes) == 2)
    a, b = screen.panes
    await wait_for_listing(pilot, a, left)
    await wait_for_listing(pilot, b, right)
    return a, b


async def drag(pilot, source: wslui.Pane, row: int, target, offset: tuple[int, int], **modifiers: bool) -> None:
    """Zieht Zeile `row` (Index in pane.view) aus `source` auf `target` (Widget) an `offset`."""
    await pilot.mouse_down(source.table, offset=(4, row + 1))  # +1: Kopfzeile
    await pilot.hover(source.table, offset=(4, row + 3))
    await pilot.hover(target, offset=offset)
    await pilot.mouse_up(target, offset=offset, **modifiers)
    await pilot.pause()


def in_drop_menu(app) -> bool:
    return isinstance(app.screen, wslui.ContextMenu) and app.screen.items[-1][0] == "cancel"


async def test_drop_on_other_pane_asks_and_copies(folders: tuple[Path, Path], clipboard: FakeClipboard) -> None:
    left, right = folders
    (left / "a.txt").write_text("A", encoding="utf-8")
    app = wslui.WSLUI(None, clipboard=clipboard)

    async with app.run_test(size=(120, 40)) as pilot:
        src, dst = await two_panes(pilot, ["..", "a.txt"], [".."])

        await drag(pilot, src, 1, dst.table, (10, 5))
        await wait_until(pilot, lambda: in_drop_menu(app))
        assert [i for i, _, _ in app.screen.items] == ["move", "copy", "sep", "cancel"]  # gleiches Laufwerk

        app.screen.dismiss("copy")
        await wait_for_listing(pilot, dst, ["..", "a.txt"])
        assert (left / "a.txt").exists() and (right / "a.txt").read_text(encoding="utf-8") == "A"
        assert clipboard.copied == []  # Ziehen ist kein Klick → kein Name kopiert


async def test_enter_in_drop_menu_runs_the_suggested_move(folders: tuple[Path, Path], clipboard: FakeClipboard) -> None:
    left, right = folders
    (left / "a.txt").write_text("A", encoding="utf-8")
    app = wslui.WSLUI(None, clipboard=clipboard)

    async with app.run_test(size=(120, 40)) as pilot:
        src, dst = await two_panes(pilot, ["..", "a.txt"], [".."])

        await drag(pilot, src, 1, dst.table, (10, 5))
        await wait_until(pilot, lambda: in_drop_menu(app))
        await pilot.press("enter")

        await wait_for_listing(pilot, dst, ["..", "a.txt"])
        await wait_for_listing(pilot, src, [".."])


async def test_ctrl_drop_copies_without_asking(folders: tuple[Path, Path], clipboard: FakeClipboard) -> None:
    left, right = folders
    (left / "a.txt").write_text("A", encoding="utf-8")
    app = wslui.WSLUI(None, clipboard=clipboard)

    async with app.run_test(size=(120, 40)) as pilot:
        src, dst = await two_panes(pilot, ["..", "a.txt"], [".."])

        await drag(pilot, src, 1, dst.table, (10, 5), control=True)

        await wait_for_listing(pilot, dst, ["..", "a.txt"])
        assert (left / "a.txt").exists()
        assert not in_drop_menu(app)


async def test_shift_drop_moves_without_asking(folders: tuple[Path, Path], clipboard: FakeClipboard) -> None:
    left, right = folders
    (left / "a.txt").write_text("A", encoding="utf-8")
    app = wslui.WSLUI(None, clipboard=clipboard)

    async with app.run_test(size=(120, 40)) as pilot:
        src, dst = await two_panes(pilot, ["..", "a.txt"], [".."])

        await drag(pilot, src, 1, dst.table, (10, 5), shift=True)

        await wait_for_listing(pilot, dst, ["..", "a.txt"])
        await wait_for_listing(pilot, src, [".."])


async def test_drop_on_folder_row_moves_into_that_folder(folders: tuple[Path, Path], clipboard: FakeClipboard) -> None:
    left, right = folders
    (left / "a.txt").write_text("A", encoding="utf-8")
    (right / "sub").mkdir()
    app = wslui.WSLUI(None, clipboard=clipboard)

    async with app.run_test(size=(120, 40)) as pilot:
        src, dst = await two_panes(pilot, ["..", "a.txt"], ["..", "sub"])

        await drag(pilot, src, 1, dst.table, (6, 2), shift=True)  # Zeile 'sub'

        await wait_until(pilot, lambda: (right / "sub" / "a.txt").exists())


async def test_marked_entries_are_dragged_together(folders: tuple[Path, Path], clipboard: FakeClipboard) -> None:
    left, right = folders
    for name in ("a.txt", "b.txt", "c.txt"):
        (left / name).write_text(name, encoding="utf-8")
    app = wslui.WSLUI(None, clipboard=clipboard)

    async with app.run_test(size=(120, 40)) as pilot:
        src, dst = await two_panes(pilot, ["..", "a.txt", "b.txt", "c.txt"], [".."])
        src.marked = {"a.txt", "c.txt"}
        src.render_rows()

        await drag(pilot, src, 3, dst.table, (10, 8), control=True)  # zieht an 'c.txt'

        await wait_for_listing(pilot, dst, ["..", "a.txt", "c.txt"])


async def test_drop_back_into_own_folder_does_nothing(folders: tuple[Path, Path], clipboard: FakeClipboard) -> None:
    left, _ = folders
    (left / "a.txt").write_text("A", encoding="utf-8")
    app = wslui.WSLUI(None, clipboard=clipboard)

    async with app.run_test(size=(120, 40)) as pilot:
        src, _ = await two_panes(pilot, ["..", "a.txt"], [".."])

        await drag(pilot, src, 1, src.table, (10, 6))

        assert not in_drop_menu(app)
        assert not app.screen.drag.active
        assert clipboard.copied == []


async def test_parent_row_cannot_be_dragged(folders: tuple[Path, Path], clipboard: FakeClipboard) -> None:
    app = wslui.WSLUI(None, clipboard=clipboard)

    async with app.run_test(size=(120, 40)) as pilot:
        src, dst = await two_panes(pilot, [".."], [".."])

        await pilot.mouse_down(src.table, offset=(4, 1))
        await pilot.hover(dst.table, offset=(10, 5))

        assert not app.screen.drag.active
        await pilot.mouse_up(dst.table, offset=(10, 5))


async def test_ghost_follows_mouse_and_names_target(folders: tuple[Path, Path], clipboard: FakeClipboard) -> None:
    left, right = folders
    (left / "a.txt").write_text("A", encoding="utf-8")
    app = wslui.WSLUI(None, clipboard=clipboard)

    async with app.run_test(size=(120, 40)) as pilot:
        src, dst = await two_panes(pilot, ["..", "a.txt"], [".."])
        ghost = app.screen.query_one(wslui.DragGhost)

        await pilot.mouse_down(src.table, offset=(4, 2))
        await pilot.hover(dst.table, offset=(10, 5))
        await pilot.pause()

        assert ghost.display
        assert "a.txt" in str(ghost.content) and "right" in str(ghost.content)
        mx, my = dst.table.region.x + 10, dst.table.region.y + 5
        assert ghost.region.y == my and ghost.region.x > mx
        assert dst.has_class("-droptarget") and not src.has_class("-droptarget")

        await pilot.mouse_up(dst.table, offset=(10, 5))
        await wait_until(pilot, lambda: in_drop_menu(app))
        assert not ghost.display
        assert not dst.has_class("-droptarget")
        app.screen.dismiss("cancel")
        await pilot.pause()
        assert (left / "a.txt").exists() and not (right / "a.txt").exists()


async def test_plain_click_still_copies_name(folders: tuple[Path, Path], clipboard: FakeClipboard) -> None:
    left, _ = folders
    (left / "a.txt").write_text("A", encoding="utf-8")
    app = wslui.WSLUI(None, clipboard=clipboard)

    async with app.run_test(size=(120, 40)) as pilot:
        src, _ = await two_panes(pilot, ["..", "a.txt"], [".."])

        await pilot.click(src.table, offset=(4, 2))

        await wait_until(pilot, lambda: clipboard.copied == ["a.txt"])
        assert not app.screen.drag.active
