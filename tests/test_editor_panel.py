"""Öffnen im eingebetteten Editor: Dateien links, Editor rechts; 'In Windows öffnen' als Alternative."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import wslui
from conftest import FakeClipboard, wait_for_listing, wait_until

# Ein "Editor", der auf eine Eingabezeile wartet und sich dann beendet – wie nano nach Ctrl+X.
FAKE_EDITOR = "sh -c 'read line' editor"


@pytest.fixture
def opened_externally(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    calls: list[Path] = []

    def fake_open_external(path: Path) -> bool:
        calls.append(path)
        return True

    monkeypatch.setattr(wslui, "open_external", fake_open_external)
    return calls


def make_app(folder: Path, clipboard: FakeClipboard, editor: str = FAKE_EDITOR) -> wslui.WSLUI:
    app = wslui.WSLUI(folder, clipboard=clipboard)
    app.cfg["layout"] = "1"
    app.cfg["editor"] = editor
    return app


async def ready_pane(pilot, names: list[str]) -> wslui.Pane:
    screen = pilot.app.screen
    await wait_until(pilot, lambda: len(screen.panes) == 1)
    pane = screen.panes[0]
    await wait_for_listing(pilot, pane, names)
    return pane


async def test_enter_on_text_file_opens_editor_to_the_right_of_the_files(
    tmp_path: Path, clipboard: FakeClipboard
) -> None:
    note = tmp_path / "note.txt"
    note.write_text("hi", encoding="utf-8")
    app = make_app(tmp_path, clipboard)

    async with app.run_test(size=(120, 40)) as pilot:
        pane = await ready_pane(pilot, ["..", "note.txt"])
        editor = app.screen.editor

        await pilot.press("enter")
        await wait_until(pilot, lambda: editor.view.running)

        assert editor.is_open
        assert editor.path == note
        assert editor.view.command == ["sh", "-c", "read line", "editor", str(note)]
        assert editor.view.has_focus
        assert editor.region.x >= pane.region.right  # Dateien links, Editor rechts

        await pilot.press("enter")  # "Editor" beenden
        await wait_until(pilot, lambda: not editor.is_open)
        assert pane.table.has_focus


async def test_binary_file_opens_with_windows_program(
    tmp_path: Path, clipboard: FakeClipboard, opened_externally: list[Path]
) -> None:
    (tmp_path / "photo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")
    app = make_app(tmp_path, clipboard)

    async with app.run_test(size=(120, 40)) as pilot:
        await ready_pane(pilot, ["..", "photo.png"])

        await pilot.press("enter")
        await pilot.pause()

        assert opened_externally == [tmp_path / "photo.png"]
        assert not app.screen.editor.is_open


async def test_capital_o_opens_even_text_files_in_windows(
    tmp_path: Path, clipboard: FakeClipboard, opened_externally: list[Path]
) -> None:
    (tmp_path / "note.txt").write_text("hi", encoding="utf-8")
    app = make_app(tmp_path, clipboard)

    async with app.run_test(size=(120, 40)) as pilot:
        await ready_pane(pilot, ["..", "note.txt"])

        await pilot.press("O")
        await pilot.pause()

        assert opened_externally == [tmp_path / "note.txt"]
        assert not app.screen.editor.is_open


async def test_context_menu_offers_open_and_open_in_windows(
    tmp_path: Path, clipboard: FakeClipboard, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wslui, "IS_WSL", True)
    (tmp_path / "note.txt").write_text("hi", encoding="utf-8")
    app = make_app(tmp_path, clipboard)

    async with app.run_test(size=(120, 40)) as pilot:
        pane = await ready_pane(pilot, ["..", "note.txt"])

        await pilot.click(pane.table, offset=(4, 2), button=3)
        await wait_until(pilot, lambda: isinstance(app.screen, wslui.ContextMenu))

        labels = {ident: label for ident, label, _ in app.screen.items if ident != "sep"}
        assert labels["open"] == "Öffnen"
        assert labels["open_windows"] == "In Windows öffnen"
        assert "edit" not in labels


async def test_context_menu_open_starts_the_editor(tmp_path: Path, clipboard: FakeClipboard) -> None:
    (tmp_path / "note.txt").write_text("hi", encoding="utf-8")
    app = make_app(tmp_path, clipboard)

    async with app.run_test(size=(120, 40)) as pilot:
        pane = await ready_pane(pilot, ["..", "note.txt"])
        await pilot.click(pane.table, offset=(4, 2), button=3)
        await wait_until(pilot, lambda: isinstance(app.screen, wslui.ContextMenu))

        app.screen.dismiss("open")
        await wait_until(pilot, lambda: isinstance(app.screen, wslui.MainScreen) and app.screen.editor.view.running)

        assert app.screen.editor.path == tmp_path / "note.txt"
        await pilot.press("enter")
        await wait_until(pilot, lambda: not app.screen.editor.is_open)


async def test_opening_another_file_while_editing_keeps_the_first_and_warns(
    tmp_path: Path, clipboard: FakeClipboard
) -> None:
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    app = make_app(tmp_path, clipboard)

    async with app.run_test(size=(120, 40), notifications=True) as pilot:
        pane = await ready_pane(pilot, ["..", "a.txt", "b.txt"])
        screen = app.screen
        await pilot.press("enter")
        await wait_until(pilot, lambda: screen.editor.view.running)

        pane.table.move_cursor(row=2)
        pane.open_entry()
        await pilot.pause()

        assert screen.editor.path == tmp_path / "a.txt"
        assert screen.editor.view.has_focus
        assert any(n.severity == "warning" for n in app._notifications)
        await pilot.press("enter")
        await wait_until(pilot, lambda: not screen.editor.is_open)


async def test_missing_editor_is_reported(tmp_path: Path, clipboard: FakeClipboard) -> None:
    (tmp_path / "note.txt").write_text("hi", encoding="utf-8")
    app = make_app(tmp_path, clipboard, editor="definitely-not-installed-editor")

    async with app.run_test(size=(120, 40), notifications=True) as pilot:
        await ready_pane(pilot, ["..", "note.txt"])

        await pilot.press("enter")
        await pilot.pause()

        assert not app.screen.editor.is_open
        assert any(n.severity == "error" and "definitely-not-installed-editor" in n.message for n in app._notifications)


async def test_quit_asks_for_confirmation_while_editor_is_open(tmp_path: Path, clipboard: FakeClipboard) -> None:
    (tmp_path / "note.txt").write_text("hi", encoding="utf-8")
    app = make_app(tmp_path, clipboard)

    async with app.run_test(size=(120, 40)) as pilot:
        pane = await ready_pane(pilot, ["..", "note.txt"])
        await pilot.press("enter")
        await wait_until(pilot, lambda: app.screen.editor.view.running)
        pane.table.focus()
        await pilot.pause()

        await pilot.press("q")
        await wait_until(pilot, lambda: isinstance(app.screen, wslui.ConfirmDialog))
        await pilot.press("n")
        await wait_until(pilot, lambda: isinstance(app.screen, wslui.MainScreen))
        assert app.is_running and app.screen.editor.view.running

        await pilot.press("q")
        await wait_until(pilot, lambda: isinstance(app.screen, wslui.ConfirmDialog))
        await pilot.press("y")
        await wait_until(pilot, lambda: not app.is_running)
        assert app.final_dir == str(tmp_path)


async def test_f12_cycles_files_editor_terminal(tmp_path: Path, clipboard: FakeClipboard) -> None:
    (tmp_path / "note.txt").write_text("hi", encoding="utf-8")
    app = make_app(tmp_path, clipboard)

    async with app.run_test(size=(120, 40)) as pilot:
        pane = await ready_pane(pilot, ["..", "note.txt"])
        screen = app.screen
        await pilot.press("enter")
        await wait_until(pilot, lambda: screen.editor.view.running)
        pane.table.focus()
        await pilot.pause()

        await pilot.press("f12")
        assert screen.editor.view.has_focus
        await pilot.press("f12")
        await wait_until(pilot, lambda: screen.term.has_focus)
        await pilot.press("f12")
        assert pane.table.has_focus

        screen.term.stop()
        screen.editor.view.stop()


@pytest.mark.skipif(shutil.which("nano") is None, reason="nano nicht installiert")
async def test_real_nano_shows_file_and_closes_with_ctrl_x(tmp_path: Path, clipboard: FakeClipboard) -> None:
    (tmp_path / "note.txt").write_text("Hallo aus nano\n", encoding="utf-8")
    app = make_app(tmp_path, clipboard, editor="nano")

    async with app.run_test(size=(120, 40)) as pilot:
        await ready_pane(pilot, ["..", "note.txt"])
        view = app.screen.editor.view

        await pilot.press("enter")

        def shows(text: str) -> bool:
            return view.pscreen is not None and any(text in "".join(ln.cells) for ln in wslui.screen_lines(view.pscreen))

        await wait_until(pilot, lambda: shows("Hallo aus nano") and shows("^X Exit"))
        assert view.in_alt_screen  # nano läuft im Vollbildmodus → Mausrad sendet Pfeiltasten

        await pilot.press("ctrl+x")
        await wait_until(pilot, lambda: not app.screen.editor.is_open)
