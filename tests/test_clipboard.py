"""Zwischenablage: Windows-Format und Verwendung der injizierten Zwischenablage."""
from __future__ import annotations

from pathlib import Path

import wslui
from conftest import FakeClipboard, wait_for_listing, wait_until


def test_windows_payload_is_utf16_with_bom() -> None:
    assert wslui.windows_clipboard_payload("ä") == b"\xff\xfe" + "ä".encode("utf-16-le")


def test_windows_payload_converts_lf_to_crlf_without_doubling_existing_crlf() -> None:
    payload = wslui.windows_clipboard_payload("a\nb\r\nc")
    assert payload == b"\xff\xfe" + "a\r\nb\r\nc".encode("utf-16-le")


def test_fake_clipboard_satisfies_protocol() -> None:
    assert isinstance(FakeClipboard(), wslui.Clipboard)


async def test_click_on_file_copies_its_name(tmp_path: Path, clipboard: FakeClipboard) -> None:
    (tmp_path / "hello.txt").write_text("hi", encoding="utf-8")
    app = wslui.WSLUI(tmp_path, clipboard=clipboard)

    async with app.run_test(size=(120, 40)) as pilot:
        pane = app.screen.panes[0]
        await wait_for_listing(pilot, pane, ["..", "hello.txt"])

        await pilot.click(pane.table, offset=(4, 2))  # Kopfzeile, "..", hello.txt

        await wait_until(pilot, lambda: clipboard.copied == ["hello.txt"])
