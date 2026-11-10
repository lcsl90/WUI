"""Smoke-Tests: Die App startet und zeigt die Dateien des Startordners."""
from __future__ import annotations

from pathlib import Path

import wslui
from conftest import wait_for_listing, wait_until


async def test_app_starts_with_default_two_pane_layout(tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("hi", encoding="utf-8")
    app = wslui.WSLUI(tmp_path)

    async with app.run_test(size=(120, 40)) as pilot:
        screen = app.screen
        await wait_until(pilot, lambda: len(screen.panes) == 2)

        first = screen.panes[0]
        await wait_for_listing(pilot, first, ["..", "hello.txt"])
        assert first.path == tmp_path
