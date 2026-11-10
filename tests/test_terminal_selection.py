"""Terminal-Textauswahl: reines Auswahlmodell und Umbruch-Erkennung im pyte-Screen."""
from __future__ import annotations

import pytest

import wslui
from wslui import ScreenLine, TextSelection


def line(text: str, width: int = 10, wrapped: bool = False) -> ScreenLine:
    return ScreenLine(tuple(text.ljust(width)), wrapped)


# ---------- TextSelection: Geometrie ----------


def test_start_and_end_are_normalized_when_dragging_backwards() -> None:
    sel = TextSelection(anchor=(5, 2), cursor=(1, 0))
    assert sel.start == (1, 0)
    assert sel.end == (5, 2)


@pytest.mark.parametrize(
    ("cell", "inside"),
    [
        ((2, 0), False),  # vor dem Start
        ((3, 0), True),  # Startzelle
        ((9, 0), True),  # Rest der ersten Zeile
        ((0, 1), True),  # mittlere Zeile komplett
        ((4, 2), True),  # Endzelle
        ((5, 2), False),  # hinter dem Ende
        ((0, 3), False),  # Zeile danach
    ],
)
def test_contains_follows_text_flow(cell: tuple[int, int], inside: bool) -> None:
    sel = TextSelection(anchor=(3, 0), cursor=(4, 2))
    assert sel.contains(*cell) is inside


# ---------- TextSelection: Text extrahieren ----------


def test_extract_single_line_includes_both_end_cells() -> None:
    lines = [line("hello world", width=12)]
    assert TextSelection((0, 0), (4, 0)).extract(lines) == "hello"


def test_extract_strips_trailing_padding_and_joins_lines_with_newline() -> None:
    lines = [line("ab"), line("cd"), line("ef")]
    assert TextSelection((0, 0), (9, 2)).extract(lines) == "ab\ncd\nef"


def test_extract_partial_first_and_last_line() -> None:
    lines = [line("git status"), line("On branch"), line("main")]
    assert TextSelection((4, 0), (1, 2)).extract(lines) == "status\nOn branch\nma"


def test_extract_joins_soft_wrapped_lines_without_newline_and_keeps_spaces_at_wrap() -> None:
    lines = [line("https://ex", wrapped=True), line("ample.com ", wrapped=True), line("/x")]
    assert TextSelection((0, 0), (9, 2)).extract(lines) == "https://example.com /x"


def test_extract_wide_characters_are_copied_once() -> None:
    cells = ("日", "", "本", "", "!", " ", " ", " ")
    assert TextSelection((0, 0), (7, 0)).extract([ScreenLine(cells)]) == "日本!"


def test_extract_starting_on_second_half_of_wide_character_includes_it() -> None:
    cells = ("日", "", "本", "", " ")
    assert TextSelection((3, 0), (4, 0)).extract([ScreenLine(cells)]) == "本"


def test_extract_drops_trailing_empty_lines_but_keeps_inner_ones() -> None:
    lines = [line("a"), line(""), line("b"), line(""), line("")]
    assert TextSelection((0, 0), (9, 4)).extract(lines) == "a\n\nb"


def test_extract_ignores_rows_beyond_screen() -> None:
    assert TextSelection((0, 0), (5, 9)).extract([line("only")]) == "only"


# ---------- WrapTrackingScreen ----------


def screen_after(data: bytes, columns: int = 10, lines: int = 4) -> "wslui.WrapTrackingScreen":
    screen = wslui.WrapTrackingScreen(columns, lines, history=100, ratio=1.0)
    wslui.pyte.ByteStream(screen).feed(data)
    return screen


def test_wrap_tracking_marks_autowrapped_line() -> None:
    lines = wslui.screen_lines(screen_after(b"0123456789abc"))
    assert lines[0].wrapped is True
    assert lines[1].wrapped is False
    assert TextSelection((0, 0), (9, 1)).extract(lines) == "0123456789abc"


def test_explicit_newline_is_not_a_wrap() -> None:
    lines = wslui.screen_lines(screen_after(b"0123456789\r\nabc"))
    assert lines[0].wrapped is False
    assert TextSelection((0, 0), (9, 1)).extract(lines) == "0123456789\nabc"


def test_erasing_a_line_clears_its_wrap_flag() -> None:
    # Zeile 0 umbrechen lassen, dann zurück an den Anfang und Zeile löschen (wie bei 'clear')
    screen = screen_after(b"0123456789abc\x1b[H\x1b[2J")
    assert all(not ln.wrapped for ln in wslui.screen_lines(screen))


def test_wrap_flag_travels_into_scrollback_history() -> None:
    screen = screen_after(b"0123456789abc\r\n1\r\n2\r\n3\r\n4", lines=3)
    screen.prev_page()  # zurückblättern: umbrochene Zeile ist wieder sichtbar
    lines = wslui.screen_lines(screen)
    assert lines[0].wrapped is True
    assert TextSelection((0, 0), (9, 1)).extract(lines) == "0123456789abc"
