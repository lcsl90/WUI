"""Öffnen: Welche Dateien landen im eingebetteten Editor, welche beim Windows-Programm?"""
from __future__ import annotations

from pathlib import Path

import pytest

import wslui


def entry_for(path: Path) -> wslui.Entry:
    e = wslui.Entry(name=path.name, path=path, is_dir=False)
    e.kind = wslui.classify(e, on_drvfs=False)
    return e


@pytest.mark.parametrize("name", ["notes.txt", "main.py", "README.md", "config.json", "Makefile", "icon.svg"])
def test_text_files_open_in_editor(tmp_path: Path, name: str) -> None:
    path = tmp_path / name
    path.write_text("hello\n", encoding="utf-8")
    assert wslui.opens_in_editor(entry_for(path)) is True


def test_empty_file_opens_in_editor(tmp_path: Path) -> None:
    path = tmp_path / "empty"
    path.touch()
    assert wslui.opens_in_editor(entry_for(path)) is True


@pytest.mark.parametrize("name", ["photo.png", "song.mp3", "clip.mp4", "doc.pdf", "src.zip", "setup.exe", "brief.docx", "zahlen.xlsx"])
def test_media_office_and_binary_types_open_in_windows_even_if_content_is_text(tmp_path: Path, name: str) -> None:
    path = tmp_path / name
    path.write_text("looks like text", encoding="utf-8")
    assert wslui.opens_in_editor(entry_for(path)) is False


def test_unknown_file_with_nul_bytes_opens_in_windows(tmp_path: Path) -> None:
    path = tmp_path / "blob.bin"
    path.write_bytes(b"\x7fELF\x02\x01\x01\x00\x00\x00")
    assert wslui.opens_in_editor(entry_for(path)) is False


def test_unreadable_file_still_goes_to_editor_which_reports_the_problem(tmp_path: Path) -> None:
    missing = tmp_path / "gone.txt"  # existiert nicht → OSError beim Lesen
    assert wslui.opens_in_editor(entry_for(missing)) is True


# ---------- Editor-Kommandozeile ----------


def test_editor_command_appends_absolute_path() -> None:
    which = {"nano": "/usr/bin/nano"}.get
    assert wslui.editor_command("nano", Path("/w/-odd name.txt"), which=which) == ["nano", "/w/-odd name.txt"]


def test_editor_command_supports_arguments() -> None:
    which = {"nano": "/usr/bin/nano"}.get
    assert wslui.editor_command("nano -l", Path("/w/a"), which=which) == ["nano", "-l", "/w/a"]


def test_editor_command_falls_back_to_nano_when_unset() -> None:
    which = {"nano": "/usr/bin/nano"}.get
    assert wslui.editor_command("  ", Path("/w/a"), which=which) == ["nano", "/w/a"]


def test_editor_command_is_none_when_editor_is_not_installed() -> None:
    assert wslui.editor_command("nano", Path("/w/a"), which=lambda _: None) is None


def test_nano_is_the_default_editor() -> None:
    assert wslui.DEFAULT_CONFIG["editor"] == "nano"


# ---------- Mausrad im Vollbildprogramm (nano, less …) ----------


def test_wheel_in_alternate_screen_sends_three_arrow_keys() -> None:
    assert wslui.wheel_input(up=True, alt_screen=True, app_cursor=False) == "\x1b[A" * 3
    assert wslui.wheel_input(up=False, alt_screen=True, app_cursor=True) == "\x1bOB" * 3


def test_wheel_in_normal_screen_scrolls_history_instead() -> None:
    assert wslui.wheel_input(up=True, alt_screen=False, app_cursor=False) is None


def test_editor_command_is_none_for_malformed_config() -> None:
    assert wslui.editor_command('nano "-l', Path("/w/a"), which=lambda _: "/usr/bin/nano") is None
