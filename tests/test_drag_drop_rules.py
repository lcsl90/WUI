"""Drag & Drop: reine Regeln für Zielordner und Aktion (Kopieren/Verschieben)."""
from __future__ import annotations

from pathlib import Path

import wslui
from wslui import Entry


def folder(path: str) -> Entry:
    return Entry(name=Path(path).name, path=Path(path), is_dir=True, kind="dir")


def file(path: str) -> Entry:
    return Entry(name=Path(path).name, path=Path(path), is_dir=False)


# ---------- Zielordner ----------


def test_drop_on_folder_row_targets_that_folder() -> None:
    assert wslui.drop_dir(Path("/b"), folder("/b/sub"), [Path("/a/x.txt")]) == Path("/b/sub")


def test_drop_on_file_row_targets_the_pane_folder() -> None:
    assert wslui.drop_dir(Path("/b"), file("/b/y.txt"), [Path("/a/x.txt")]) == Path("/b")


def test_drop_on_empty_area_targets_the_pane_folder() -> None:
    assert wslui.drop_dir(Path("/b"), None, [Path("/a/x.txt")]) == Path("/b")


def test_drop_on_parent_row_targets_the_parent_folder() -> None:
    parent = Entry(name="..", path=Path("/b"), is_dir=True, is_parent=True, kind="parent")
    assert wslui.drop_dir(Path("/b/c"), parent, [Path("/b/c/x.txt")]) == Path("/b")


def test_drop_on_a_dragged_folder_itself_targets_the_pane_folder() -> None:
    assert wslui.drop_dir(Path("/a"), folder("/a/sub"), [Path("/a/sub")]) == Path("/a")


def test_drop_into_same_folder_is_a_noop() -> None:
    assert wslui.is_noop_drop([Path("/a/x"), Path("/a/y")], Path("/a")) is True
    assert wslui.is_noop_drop([Path("/a/x")], Path("/b")) is False


# ---------- Aktion ----------


def test_modifiers_decide_like_windows_explorer() -> None:
    assert wslui.drop_action_from_modifiers(ctrl=True, shift=False) == "copy"
    assert wslui.drop_action_from_modifiers(ctrl=False, shift=True) == "move"
    assert wslui.drop_action_from_modifiers(ctrl=False, shift=False) is None


def test_preferred_action_is_move_on_same_drive_and_copy_across_drives() -> None:
    devices = {Path("/home/u/a.txt"): 1, Path("/home/u/b"): 1, Path("/mnt/c/x"): 2}
    device = devices.__getitem__
    assert wslui.preferred_drop_action([Path("/home/u/a.txt")], Path("/home/u/b"), device=device) == "move"
    assert wslui.preferred_drop_action([Path("/home/u/a.txt")], Path("/mnt/c/x"), device=device) == "copy"


def test_preferred_action_falls_back_to_copy_when_device_is_unknown() -> None:
    def broken(_: Path) -> int:
        raise OSError("weg")

    assert wslui.preferred_drop_action([Path("/a")], Path("/b"), device=broken) == "copy"


def test_drop_menu_lists_preferred_action_first_with_modifier_hints() -> None:
    assert wslui.drop_menu_items("copy") == [
        ("copy", "Hierher kopieren", "Strg"),
        ("move", "Hierher verschieben", "Shift"),
        ("sep", "", ""),
        ("cancel", "Abbrechen", "Esc"),
    ]
    assert [ident for ident, _, _ in wslui.drop_menu_items("move")][:2] == ["move", "copy"]
