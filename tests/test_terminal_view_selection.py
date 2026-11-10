"""Terminal-Widget: Markieren mit der Maus kopiert beim Loslassen sofort und meldet es per Toast."""
from __future__ import annotations

from textual.app import App, ComposeResult

import wslui
from conftest import FakeClipboard


class TerminalHost(App):
    """Minimale App um ein TerminalView – ohne echten Shell-Prozess."""

    def __init__(self, clipboard: FakeClipboard) -> None:
        super().__init__()
        self.sys_clipboard = clipboard

    def compose(self) -> ComposeResult:
        yield wslui.TerminalView()

    @property
    def view(self) -> wslui.TerminalView:
        return self.query_one(wslui.TerminalView)


async def started(pilot, data: bytes) -> wslui.TerminalView:
    view = pilot.app.view
    view.reset_screen()
    view.feed(data)
    await pilot.pause()
    return view


def notifications(app: App) -> list[tuple[str, str]]:
    return [(n.title, n.message) for n in app._notifications]


async def drag(pilot, view, start: tuple[int, int], end: tuple[int, int]) -> None:
    await pilot.mouse_down(view, offset=start)
    await pilot.hover(view, offset=end)
    await pilot.mouse_up(view, offset=end)
    await pilot.pause()


async def test_drag_copies_selected_text_on_mouse_up_and_notifies(clipboard: FakeClipboard) -> None:
    app = TerminalHost(clipboard)
    async with app.run_test(size=(40, 6)) as pilot:
        view = await started(pilot, b"hello world\r\n$ ")

        await drag(pilot, view, (0, 0), (4, 0))

        assert clipboard.copied == ["hello"]
        assert ("Text kopiert", "📋 hello") in notifications(app)


async def test_drag_backwards_over_several_lines(clipboard: FakeClipboard) -> None:
    app = TerminalHost(clipboard)
    async with app.run_test(size=(40, 6)) as pilot:
        view = await started(pilot, b"git status\r\nOn branch main\r\n")

        await drag(pilot, view, (6, 1), (4, 0))

        assert clipboard.copied == ["status\nOn bran"]
        title, message = notifications(app)[-1]
        assert title == "Text kopiert"
        assert message == "📋 status  (+1 Zeile)"


async def test_plain_click_copies_nothing(clipboard: FakeClipboard) -> None:
    app = TerminalHost(clipboard)
    async with app.run_test(size=(40, 6)) as pilot:
        view = await started(pilot, b"hello\r\n")

        await pilot.click(view, offset=(1, 0))

        assert clipboard.copied == []
        assert view.selection is None


async def test_selection_is_clamped_to_terminal_area(clipboard: FakeClipboard) -> None:
    app = TerminalHost(clipboard)
    async with app.run_test(size=(20, 4)) as pilot:
        view = await started(pilot, b"abc\r\ndef")

        await pilot.mouse_down(view, offset=(0, 0))
        await pilot.hover(offset=(19, 3))  # rechte untere Ecke
        await pilot.mouse_up(offset=(19, 3))
        await pilot.pause()

        assert clipboard.copied == ["abc\ndef"]


async def test_selection_stays_highlighted_until_next_key(clipboard: FakeClipboard) -> None:
    app = TerminalHost(clipboard)
    async with app.run_test(size=(40, 6)) as pilot:
        view = await started(pilot, b"hello\r\n")
        await drag(pilot, view, (0, 0), (2, 0))
        assert view.selection == wslui.TextSelection((0, 0), (2, 0))

        await pilot.press("x")

        assert view.selection is None


async def test_text_with_markup_characters_is_shown_literally(clipboard: FakeClipboard) -> None:
    app = TerminalHost(clipboard)
    async with app.run_test(size=(40, 6)) as pilot:
        view = await started(pilot, b"[bold]x[/bold]\r\n")

        await drag(pilot, view, (0, 0), (13, 0))

        assert clipboard.copied == ["[bold]x[/bold]"]
        note = list(app._notifications)[-1]
        assert note.message == "📋 [bold]x[/bold]"
        assert note.markup is False
