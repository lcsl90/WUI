"""Scrollen in Vollbildprogrammen (nano, less, vim): CSI S / CSI T verschieben den Bildschirminhalt."""
from __future__ import annotations

import wslui


def make_screen(rows: int = 6) -> tuple[wslui.WrapTrackingScreen, wslui.TerminalStream]:
    screen = wslui.WrapTrackingScreen(20, rows, history=100, ratio=0.5)
    return screen, wslui.TerminalStream(screen)


def shown(screen: wslui.WrapTrackingScreen) -> list[str]:
    return ["".join(line.cells).rstrip() for line in wslui.screen_lines(screen)]


def test_csi_s_scrolls_the_whole_screen_up() -> None:
    screen, stream = make_screen()
    stream.feed(b"a\r\nb\r\nc\r\nd\r\ne\r\nf")

    stream.feed(b"\x1b[2S")

    assert shown(screen) == ["c", "d", "e", "f", "", ""]
    assert (screen.cursor.x, screen.cursor.y) == (1, 5)  # der Cursor bleibt, wo er war


def test_csi_t_scrolls_down_and_defaults_to_one_line() -> None:
    screen, stream = make_screen()
    stream.feed(b"a\r\nb\r\nc\r\nd\r\ne\r\nf")

    stream.feed(b"\x1b[T")

    assert shown(screen) == ["", "a", "b", "c", "d", "e"]


def test_scrolling_stays_inside_the_scroll_region() -> None:
    """So blättert nano: Titelzeile und Hilfezeilen stehen fest, nur der Textbereich wandert."""
    screen, stream = make_screen()
    stream.feed(b"title\r\n1\r\n2\r\n3\r\n4\r\nhelp")

    stream.feed(b"\x1b[2;5r\x1b[3S")  # Scrollbereich Zeile 2–5, dann 3 Zeilen hoch

    assert shown(screen) == ["title", "4", "", "", "", "help"]


def test_private_and_multi_parameter_variants_are_not_scrolling() -> None:
    screen, stream = make_screen()
    stream.feed(b"a\r\nb\r\nc")

    stream.feed(b"\x1b[?1;1;0S\x1b[1;2;3;4;5T")  # Grafik-Abfrage bzw. Maus-Tracking

    assert shown(screen)[:3] == ["a", "b", "c"]


def test_fullscreen_programs_do_not_fill_the_shell_scrollback() -> None:
    screen, stream = make_screen()
    stream.feed(b"\x1b[?1049h" + b"x\r\n" * 20 + b"\x1b[5S")

    assert len(screen.history.top) == 0

    stream.feed(b"\x1b[?1049l" + b"y\r\n" * 20)
    assert len(screen.history.top) > 0
