"""Gemeinsame Test-Helfer für WSLUI."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Callable

import pytest

import wslui


@pytest.fixture(autouse=True)
def isolated_config(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Jeder Test bekommt ein eigenes, leeres Konfigurationsverzeichnis."""
    cfg_dir = tmp_path_factory.mktemp("config")
    monkeypatch.setattr(wslui, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(wslui, "CONFIG_FILE", cfg_dir / "config.json")
    monkeypatch.setattr(wslui, "AUTOSTART_FLAG", cfg_dir / "autostart")
    return cfg_dir


async def wait_until(pilot, predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    """Lässt die App laufen, bis `predicate()` wahr ist (oder bricht nach `timeout` ab)."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("Bedingung wurde nicht rechtzeitig erfüllt")
        await pilot.pause(0.02)
        await asyncio.sleep(0)


class FakeClipboard:
    """Test-Double für `wslui.Clipboard`: merkt sich kopierte Texte, liefert festen Einfüge-Text."""

    def __init__(self, paste_text: str = "") -> None:
        self.copied: list[str] = []
        self.paste_text = paste_text

    def copy(self, text: str) -> None:
        self.copied.append(text)

    def paste(self) -> str:
        return self.paste_text


@pytest.fixture
def clipboard() -> FakeClipboard:
    return FakeClipboard()


async def wait_for_listing(pilot, pane, names: list[str]) -> None:
    """Wartet, bis `pane` genau `names` anzeigt und die Tabelle in finaler Breite gerendert ist."""

    def ready() -> bool:
        table = pane.table
        return (
            not pane.loading
            and [e.name for e in pane.view] == names
            and table.row_count == len(names)
            and table.size.width > 0
            and table._rendered_width == table.size.width
        )

    await wait_until(pilot, ready)
    await pilot.pause()
