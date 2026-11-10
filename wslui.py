#!/usr/bin/env python3
"""WSLUI – ein moderner, mausfreundlicher Dateimanager für WSL (Textual-TUI)."""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shlex
import shutil
import stat
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Collection, Iterable, Literal, Protocol, Sequence, runtime_checkable

from rich.syntax import Syntax
from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.errors import NoWidget
from textual.geometry import Offset
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Input,
    Label,
    Markdown,
    OptionList,
    Static,
)
from textual.widgets.option_list import Option

# --------------------------------------------------------------------------- #
# Umgebung & Konfiguration
# --------------------------------------------------------------------------- #

IS_WSL = "microsoft" in platform.release().lower() or os.path.exists(
    "/proc/sys/fs/binfmt_misc/WSLInterop"
)
HOME = Path.home()
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "wslui"
CONFIG_FILE = CONFIG_DIR / "config.json"
AUTOSTART_FLAG = CONFIG_DIR / "autostart"

DEFAULT_CONFIG = {
    "theme": "tokyo-night",
    "icons": "emoji",  # emoji | nerd | plain
    "layout": "2",
    "show_hidden": False,
    "click_copy": True,
    "preview": False,
    "editor": "nano",  # Editor für „Öffnen“ – läuft eingebettet rechts neben den Dateien
    "panes": [],
    "bookmarks": [],
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg: dict) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(CONFIG_FILE)
    except OSError:
        pass


def set_autostart(enabled: bool) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if enabled:
        AUTOSTART_FLAG.touch()
    else:
        AUTOSTART_FLAG.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# WSL-Helfer: Zwischenablage, Pfade, Öffnen
# --------------------------------------------------------------------------- #


def to_windows_path(path: Path) -> str:
    try:
        out = subprocess.run(
            ["wslpath", "-w", str(path)], capture_output=True, text=True, timeout=3
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return str(path)


def from_windows_path(text: str) -> str:
    """Wandelt 'C:\\Users\\x' bzw. '\\\\wsl.localhost\\...' in einen Linux-Pfad um."""
    t = text.strip().strip('"')
    if re.match(r"^[A-Za-z]:[\\/]", t) or t.startswith("\\\\"):
        try:
            out = subprocess.run(
                ["wslpath", "-u", t], capture_output=True, text=True, timeout=3
            )
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return t


@runtime_checkable
class Clipboard(Protocol):
    """Zugriff auf die Zwischenablage des Systems (unter WSL: die von Windows)."""

    def copy(self, text: str) -> None:
        """Kopiert `text` (darf asynchron erfolgen, blockiert nicht)."""

    def paste(self) -> str:
        """Liefert den aktuellen Text (darf blockieren – nur aus Worker-Threads aufrufen)."""


def windows_clipboard_payload(text: str) -> bytes:
    """clip.exe erwartet UTF-16 mit BOM; Windows-Programme erwarten CRLF-Zeilenenden."""
    crlf = text.replace("\r\n", "\n").replace("\n", "\r\n")
    return b"\xff\xfe" + crlf.encode("utf-16-le")


class SystemClipboard:
    """Windows-Zwischenablage über clip.exe/PowerShell; außerhalb von WSL OSC 52 bzw. wl-paste/xclip."""

    def __init__(self, app: App) -> None:
        self._app = app

    def copy(self, text: str) -> None:
        threading.Thread(target=self._copy_blocking, args=(text,), daemon=True).start()

    def _copy_blocking(self, text: str) -> None:
        if IS_WSL and shutil.which("clip.exe"):
            try:
                subprocess.run(
                    ["clip.exe"],
                    input=windows_clipboard_payload(text),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=True,
                )
                return
            except (OSError, subprocess.SubprocessError):
                pass
        self._app.call_from_thread(self._app.copy_to_clipboard, text)

    def paste(self) -> str:
        return read_clipboard()


def read_clipboard() -> str:
    """Liest Text aus der Windows-Zwischenablage (blockierend, ~0,3 s)."""
    if IS_WSL and shutil.which("powershell.exe"):
        try:
            out = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-Clipboard -Raw",
                ],
                capture_output=True,
                timeout=6,
                cwd="/mnt/c" if os.path.isdir("/mnt/c") else None,
            )
            text = out.stdout.decode("utf-8", errors="replace")
            return text[:-2] if text.endswith("\r\n") else text
        except (OSError, subprocess.SubprocessError):
            return ""
    for cmd in (["wl-paste", "-n"], ["xclip", "-o", "-selection", "clipboard"]):
        if shutil.which(cmd[0]):
            try:
                return subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout
            except (OSError, subprocess.SubprocessError):
                pass
    return ""


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-_]")


def sanitize_paste(text: str) -> str:
    """Entfernt Escape-Sequenzen/Steuerzeichen und vereinheitlicht Zeilenenden."""
    text = _ANSI_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(ch for ch in text if ch in "\n\t" or (ord(ch) >= 32 and ch != "\x7f"))
    return text.rstrip("\n")


def spawn(cmd: list[str], cwd: str | None = None) -> bool:
    try:
        subprocess.Popen(
            cmd,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except OSError:
        return False


def open_external(path: Path) -> bool:
    """Öffnet Datei/Ordner mit dem Windows-Standardprogramm."""
    if IS_WSL:
        if shutil.which("wslview"):
            return spawn(["wslview", str(path)])
        if shutil.which("explorer.exe"):
            return spawn(["explorer.exe", to_windows_path(path)], cwd=str(path.parent))
    if shutil.which("xdg-open"):
        return spawn(["xdg-open", str(path)])
    return False


def reveal_in_explorer(path: Path) -> bool:
    if IS_WSL and shutil.which("explorer.exe"):
        if path.is_dir():
            return spawn(["explorer.exe", to_windows_path(path)], cwd=str(path))
        return spawn(
            ["explorer.exe", f"/select,{to_windows_path(path)}"], cwd=str(path.parent)
        )
    return open_external(path if path.is_dir() else path.parent)


_win_profile: Path | None = None


def detect_windows_profile() -> None:
    """Ermittelt %USERPROFILE% (einmalig, im Hintergrund)."""
    global _win_profile
    if not IS_WSL or not shutil.which("cmd.exe"):
        return
    try:
        out = subprocess.run(
            ["cmd.exe", "/c", "echo %USERPROFILE%"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd="/mnt/c" if os.path.isdir("/mnt/c") else None,
        )
        win = out.stdout.strip().splitlines()[-1].strip() if out.stdout.strip() else ""
        if win and "%" not in win:
            p = Path(from_windows_path(win))
            if p.is_dir():
                _win_profile = p
    except (OSError, subprocess.SubprocessError, IndexError):
        pass


# --------------------------------------------------------------------------- #
# Dateitypen, Icons, Farben
# --------------------------------------------------------------------------- #

KINDS: dict[str, tuple[str, str, str, str]] = {
    # kind:      (emoji, nerd,     plain, style)
    "parent": ("📁", "\uf062", "^", "bold #7f8aa3"),
    "dir": ("📁", "\uf07b", ">", "bold #61afef"),
    "dirlink": ("📂", "\uf07c", ">", "bold italic #56b6c2"),
    "link": ("🔗", "\uf0c1", "@", "italic #56b6c2"),
    "broken": ("💔", "\uf127", "!", "italic strike #e06c75"),
    "exec": ("⚡", "\uf489", "*", "bold #98c379"),
    "winexe": ("💾", "\uf17a", "*", "bold #98c379"),
    "python": ("🐍", "\ue73c", " ", "#ffd866"),
    "code": ("💻", "\uf121", " ", "#e5c07b"),
    "web": ("🌐", "\uf13b", " ", "#f78c6c"),
    "script": ("📜", "\uf489", " ", "#98c379"),
    "config": ("🔧", "\ue615", " ", "#c678dd"),
    "doc": ("📝", "\uf15c", " ", "#d7dae0"),
    "pdf": ("📕", "\uf1c1", " ", "#ff6b6b"),
    "data": ("📊", "\uf1c0", " ", "#4dd0e1"),
    "image": ("🎨", "\uf1c5", " ", "#d19a66"),
    "audio": ("🎵", "\uf1c7", " ", "#c792ea"),
    "video": ("🎬", "\uf1c8", " ", "#ff79c6"),
    "archive": ("📦", "\uf1c6", " ", "bold #e06c75"),
    "secret": ("🔑", "\uf084", " ", "#ff9e64"),
    "file": ("📄", "\uf15b", " ", "#abb2bf"),
}

_EXT: dict[str, str] = {}
for _kind, _exts in {
    "python": "py pyw pyi ipynb",
    "code": "js mjs cjs ts tsx jsx go rs c h cc cpp hpp cxx cs java kt kts rb php lua "
    "swift scala dart zig nim ex exs erl hs ml clj r jl pl vue svelte sol asm",
    "web": "html htm css scss sass less",
    "script": "sh bash zsh fish ps1 psm1 bat cmd",
    "config": "json jsonc yaml yml toml ini conf cfg env xml properties lock "
    "gitignore gitattributes editorconfig dockerignore",
    "doc": "md markdown txt rst log doc docx odt rtf tex org",
    "pdf": "pdf",
    "data": "csv tsv xls xlsx ods db sqlite sqlite3 sql parquet avro feather",
    "image": "png jpg jpeg gif bmp svg webp ico tif tiff heic psd raw",
    "audio": "mp3 wav flac ogg m4a aac opus wma",
    "video": "mp4 mkv avi mov webm wmv flv m4v",
    "archive": "zip tar gz tgz bz2 xz zst 7z rar deb rpm iso dmg apk jar whl",
    "secret": "pem key crt cer pub gpg asc p12 pfx kdbx",
    "winexe": "exe msi dll appx msix",
}.items():
    for _e in _exts.split():
        _EXT["." + _e] = _kind

_NAMES = {
    "makefile": "script",
    "dockerfile": "config",
    "containerfile": "config",
    "readme": "doc",
    "license": "doc",
    ".bashrc": "script",
    ".zshrc": "script",
    ".profile": "script",
    ".env": "config",
}

# Windows-Systemdateien, die auf /mnt/* wie versteckte Dateien behandelt werden
_WIN_HIDDEN = {
    "desktop.ini",
    "thumbs.db",
    "ntuser.dat",
    "pagefile.sys",
    "swapfile.sys",
    "hiberfil.sys",
    "dumpstack.log.tmp",
    "system volume information",
    "recovery",
}


@dataclass
class Entry:
    name: str
    path: Path
    is_dir: bool
    is_link: bool = False
    broken: bool = False
    size: int = 0
    mtime: float = 0.0
    mode: int = 0
    hidden: bool = False
    kind: str = "file"
    target: str = ""
    is_parent: bool = False


def classify(e: Entry, on_drvfs: bool) -> str:
    if e.is_parent:
        return "parent"
    if e.broken:
        return "broken"
    if e.is_dir:
        return "dirlink" if e.is_link else "dir"
    low = e.name.lower()
    if low in _NAMES:
        return _NAMES[low]
    ext = os.path.splitext(low)[1]
    if ext in _EXT:
        return _EXT[ext]
    if low.startswith(".") and ("rc" in low or low.endswith("config")):
        return "config"
    if e.is_link:
        return "link"
    if not on_drvfs and e.mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
        return "exec"
    return "file"


def list_dir(path: Path) -> list[Entry]:
    on_drvfs = str(path).startswith("/mnt/")
    entries: list[Entry] = []
    with os.scandir(path) as it:
        for de in it:
            p = Path(de.path)
            try:
                is_link = de.is_symlink()
            except OSError:
                is_link = False
            broken = False
            try:
                st = de.stat(follow_symlinks=True)
            except OSError:
                try:
                    st = de.stat(follow_symlinks=False)
                except OSError:
                    st = None
                broken = is_link
            is_dir = bool(st and stat.S_ISDIR(st.st_mode))
            low = de.name.lower()
            hidden = de.name.startswith(".") or (
                on_drvfs and (low in _WIN_HIDDEN or de.name.startswith("$") or low.startswith("ntuser."))
            )
            target = ""
            if is_link:
                try:
                    target = os.readlink(de.path)
                except OSError:
                    pass
            e = Entry(
                name=de.name,
                path=p,
                is_dir=is_dir,
                is_link=is_link,
                broken=broken,
                size=st.st_size if st and not is_dir else 0,
                mtime=st.st_mtime if st else 0.0,
                mode=st.st_mode if st else 0,
                hidden=hidden,
                target=target,
            )
            e.kind = classify(e, on_drvfs)
            entries.append(e)
    return entries


# --------------------------------------------------------------------------- #
# Öffnen: eingebetteter Editor oder Windows-Programm?
# --------------------------------------------------------------------------- #

# Diese Typen sind auch als Text kaum sinnvoll in nano zu bearbeiten
_WINDOWS_KINDS = {"image", "audio", "video", "pdf", "archive", "winexe"}
_WINDOWS_EXTS = {".doc", ".docx", ".odt", ".rtf", ".xls", ".xlsx", ".ods", ".ppt", ".pptx", ".odp"}
_TEXT_EXTS = {".svg"}  # als "image" eingestuft, aber Text


def looks_binary(head: bytes) -> bool:
    """Heuristik wie bei git/grep: NUL-Bytes am Dateianfang → Binärdatei."""
    return b"\x00" in head


def opens_in_editor(entry: Entry) -> bool:
    """Textdateien öffnen im eingebetteten Editor, alles andere mit dem Windows-Programm."""
    ext = os.path.splitext(entry.name.lower())[1]
    if ext not in _TEXT_EXTS and (entry.kind in _WINDOWS_KINDS or ext in _WINDOWS_EXTS):
        return False
    try:
        with open(entry.path, "rb") as fh:
            return not looks_binary(fh.read(8192))
    except OSError:
        return True  # der Editor zeigt das Problem (z. B. fehlende Rechte) selbst an


def editor_command(
    editor: str, path: Path, which: Callable[[str], str | None] = shutil.which
) -> list[str] | None:
    """Kommandozeile für den eingebetteten Editor – None, wenn er nicht installiert ist.

    Der Pfad ist absolut (beginnt mit '/'), kann also nie als Option missverstanden werden.
    """
    try:
        argv = shlex.split(editor) or ["nano"]
    except ValueError:  # z. B. unbalancierte Anführungszeichen in der Konfiguration
        return None
    if not which(argv[0]):
        return None
    return [*argv, str(path)]


_num = re.compile(r"(\d+)")


def natkey(s: str) -> list:
    return [int(t) if t.isdigit() else t for t in _num.split(s.lower())]


SORTS = ["name", "size", "date", "ext"]
SORT_LABEL = {"name": "Name", "size": "Größe", "date": "Datum", "ext": "Typ"}


def sort_entries(entries: list[Entry], key: str, rev: bool) -> list[Entry]:
    keyf: Callable[[Entry], object] = {
        "name": lambda e: natkey(e.name),
        "size": lambda e: (e.size, natkey(e.name)),
        "date": lambda e: e.mtime,
        "ext": lambda e: (os.path.splitext(e.name)[1].lower(), natkey(e.name)),
    }[key]
    dirs = sorted((e for e in entries if e.is_dir), key=keyf if key != "size" else (lambda e: natkey(e.name)), reverse=rev and key != "size")
    files = sorted((e for e in entries if not e.is_dir), key=keyf, reverse=rev)
    return dirs + files


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def fmt_time(ts: float) -> str:
    if not ts:
        return ""
    d = datetime.fromtimestamp(ts)
    if d.date() == datetime.now().date():
        return "heute " + d.strftime("%H:%M")
    return d.strftime("%d.%m.%y %H:%M")


def icon_for(kind: str, mode: str) -> str:
    emoji, nerd, plain, _ = KINDS[kind]
    return {"emoji": emoji, "nerd": nerd, "plain": plain}.get(mode, emoji)


def shell_join(items: list[str]) -> str:
    if len(items) == 1:
        return items[0]
    return " ".join(shlex.quote(i) for i in items)


# --------------------------------------------------------------------------- #
# Drag & Drop: Regeln (reine Logik)
# --------------------------------------------------------------------------- #

DropAction = Literal["copy", "move"]


def drop_dir(pane_dir: Path, hovered: Entry | None, dragged: Collection[Path]) -> Path:
    """Zielordner beim Ablegen: der Ordner unter der Maus ('..' = übergeordneter Ordner),
    sonst der Ordner des Fensters. Auf sich selbst lässt sich ein Ordner nicht ablegen."""
    if hovered is not None and hovered.is_dir and hovered.path not in dragged:
        return hovered.path
    return pane_dir


def is_noop_drop(sources: Sequence[Path], target: Path) -> bool:
    """Nichts zu tun, wenn alle Quellen bereits im Zielordner liegen."""
    return all(src.parent == target for src in sources)


def drops_into_itself(sources: Sequence[Path], target: Path) -> bool:
    """Ein Ordner kann nicht in sich selbst oder einen seiner Unterordner abgelegt werden."""
    return any(target == src or src in target.parents for src in sources)


def drop_action_from_modifiers(ctrl: bool, shift: bool) -> DropAction | None:
    """Wie im Windows-Explorer: Strg = kopieren, Shift = verschieben; ohne Taste → nachfragen."""
    if ctrl:
        return "copy"
    if shift:
        return "move"
    return None


def preferred_drop_action(
    sources: Sequence[Path], target: Path, device: Callable[[Path], int] = lambda p: os.lstat(p).st_dev
) -> DropAction:
    """Vorschlag wie im Windows-Explorer: gleiches Laufwerk → verschieben, sonst kopieren."""
    try:
        target_dev = device(target)
        same_drive = all(device(src) == target_dev for src in sources)
    except OSError:
        return "copy"
    return "move" if same_drive else "copy"


def drop_menu_items(preferred: DropAction) -> list[tuple[str, str, str]]:
    """Menü nach dem Ablegen – der Vorschlag steht oben (Enter wählt ihn)."""
    move = ("move", "Hierher verschieben", "Shift")
    copy = ("copy", "Hierher kopieren", "Strg")
    first, second = (move, copy) if preferred == "move" else (copy, move)
    return [first, second, ("sep", "", ""), ("cancel", "Abbrechen", "Esc")]


# --------------------------------------------------------------------------- #
# Kleine Widgets
# --------------------------------------------------------------------------- #


class ClickBar(Static):
    """Einzeilige Leiste mit klickbaren Bereichen."""

    def __init__(self, **kw) -> None:
        super().__init__("", **kw)
        self._spans: list[tuple[int, int, Callable[[], None]]] = []

    def set_items(self, items: Iterable[tuple[Text, Callable[[], None] | None]]) -> None:
        text = Text(no_wrap=True, overflow="ellipsis")
        spans = []
        for label, cb in items:
            start = text.cell_len
            text.append_text(label)
            if cb is not None:
                spans.append((start, text.cell_len, cb))
        self._spans = spans
        self.update(text)

    def on_click(self, event: events.Click) -> None:
        off = event.get_content_offset(self)
        if off is None:
            return
        for start, end, cb in self._spans:
            if start <= off.x < end:
                event.stop()
                cb()
                return


class FileTable(DataTable):
    """DataTable mit eigener Maus-Logik: Klick = auswählen+kopieren, Doppelklick = öffnen."""

    BINDINGS = [
        Binding("enter", "open", "Öffnen", show=False),
        Binding("right,l", "open_dir", "Rein", show=False),
        Binding("left,h,backspace", "parent", "Hoch", show=False),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
        Binding("space", "mark", "Markieren", show=False),
        Binding("a", "mark_all", "Alle markieren", show=False),
    ]

    def __init__(self, pane: "Pane", **kw) -> None:
        super().__init__(cursor_type="row", zebra_stripes=False, show_cursor=True, **kw)
        self.pane = pane
        self._rendered_width = -1
        self._press: tuple[int, int, int] | None = None  # (Zeile, x, y) beim Drücken der linken Taste

    # ---------- Drag & Drop ----------
    @property
    def _drag(self) -> "DragController":
        return self.screen.drag  # type: ignore[attr-defined]

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if self._drag.active:  # Loslassen geschah außerhalb des Fensters → alten Vorgang verwerfen
            self._drag.cancel()
        self._press = None
        row = event.style.meta.get("row")
        if event.button == 1 and isinstance(row, int) and 0 <= row < self.row_count:
            self._press = (row, event.screen_x, event.screen_y)
            self.capture_mouse()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self._press is None:
            return
        row, x0, y0 = self._press
        if not self._drag.active:
            # Erst ab spürbarer Bewegung ziehen – ein leicht verwackelter Klick bleibt ein Klick
            if abs(event.screen_y - y0) < 1 and abs(event.screen_x - x0) < 2:
                return
            entries = self.pane.drag_entries(row)
            if not entries:
                self._press = None
                self.release_mouse()
                return
            self.suppress_click()  # Ziehen ist kein Klick: nichts kopieren, nichts öffnen
            self.move_cursor(row=row, animate=False)
            self._drag.begin(self.pane, entries)
        event.prevent_default()  # keine Hover-Markierung der DataTable während des Ziehens
        self._drag.move(event.screen_x, event.screen_y)

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if self._press is None:
            return
        self._press = None
        self.release_mouse()
        if self._drag.active:
            self._drag.drop(event.screen_x, event.screen_y, ctrl=event.ctrl, shift=event.shift)

    async def _on_click(self, event: events.Click) -> None:
        event.prevent_default()  # Standardverhalten der DataTable unterdrücken
        event.stop()
        self.focus()
        meta = event.style.meta
        if "row" not in meta:
            return
        row = meta["row"]
        if row == -1:
            if event.button == 1:
                self.pane.sort_by_column(meta.get("column", -1))
            return
        if not (0 <= row < self.row_count):
            return
        self.move_cursor(row=row, animate=False)
        if event.button == 3:
            self.pane.open_context_menu(event.screen_x, event.screen_y)
        elif event.button == 1:
            if event.chain >= 2:
                self.pane.open_entry()
            else:
                self.pane.entry_clicked()

    def on_focus(self) -> None:
        self.screen.set_active(self.pane)  # type: ignore[attr-defined]

    def on_resize(self, event: events.Resize) -> None:
        if event.size.width != self._rendered_width:
            self.pane.render_rows()

    def action_open(self) -> None:
        self.pane.open_entry()

    def action_open_dir(self) -> None:
        e = self.pane.current
        if e and e.is_dir:
            self.pane.open_entry()

    def action_parent(self) -> None:
        self.pane.go_parent()

    def action_mark(self) -> None:
        self.pane.toggle_mark()

    def action_mark_all(self) -> None:
        self.pane.mark_all()


# --------------------------------------------------------------------------- #
# Pane (ein Dateifenster)
# --------------------------------------------------------------------------- #


@dataclass
class PaneState:
    path: str
    sort: str = "name"
    rev: bool = False


class Pane(Vertical):
    BINDINGS = [Binding("escape", "clear_filter", show=False)]

    def __init__(self, state: PaneState, **kw) -> None:
        super().__init__(classes="pane", **kw)
        self.state = state
        self.all_entries: list[Entry] = []
        self.view: list[Entry] = []
        self.marked: set[str] = set()
        self.filter_text = ""
        self.loading = False
        self._dir_mtime = 0.0
        self._pending_keep: str | None = None

    # ---------- Aufbau ----------
    def compose(self) -> ComposeResult:
        yield ClickBar(classes="pathbar")
        yield FileTable(self)
        yield Input(placeholder="Filter … (Enter = fertig, Esc = löschen)", classes="filter")

    def on_mount(self) -> None:
        self.load(Path(self.state.path))

    @property
    def table(self) -> FileTable:
        return self.query_one(FileTable)

    @property
    def path(self) -> Path:
        return Path(self.state.path)

    @property
    def current(self) -> Entry | None:
        if not self.view:
            return None
        r = self.table.cursor_row
        return self.view[r] if 0 <= r < len(self.view) else None

    @property
    def cfg(self) -> dict:
        return self.app.cfg  # type: ignore[attr-defined]

    def selection(self) -> list[Entry]:
        sel = [e for e in self.view if e.name in self.marked and not e.is_parent]
        if sel:
            return sel
        cur = self.current
        return [cur] if cur and not cur.is_parent else []

    def entry_at(self, row: int) -> Entry | None:
        return self.view[row] if 0 <= row < len(self.view) else None

    def drag_entries(self, row: int) -> list[Entry]:
        """Was beim Ziehen ab `row` mitgeht: alle markierten Einträge, wenn die Zeile
        markiert ist – sonst nur sie selbst ('..' nie)."""
        e = self.entry_at(row)
        if e is None or e.is_parent:
            return []
        if e.name in self.marked:
            return [x for x in self.view if x.name in self.marked and not x.is_parent]
        return [e]

    def on_click(self, event: events.Click) -> None:
        self.table.focus()

    # ---------- Laden ----------
    def load(self, path: Path, keep: str | None = None) -> None:
        self.loading = True
        self._pending_keep = keep
        self.update_status()
        self._load_worker(path, keep)

    @work(thread=True, exclusive=True, group="load")
    def _load_worker(self, path: Path, keep: str | None) -> None:
        try:
            entries = list_dir(path)
            mtime = path.stat().st_mtime
        except OSError as exc:
            self.app.call_from_thread(self._load_failed, path, exc)
            return
        self.app.call_from_thread(self._loaded, path, entries, mtime, keep)

    def _load_failed(self, path: Path, exc: OSError) -> None:
        self.loading = False
        msg = "Keine Berechtigung" if isinstance(exc, PermissionError) else exc.strerror or str(exc)
        self.app.notify(f"{path}: {msg}", title="Ordner nicht lesbar", severity="error")
        if not self.all_entries:  # erster Ladeversuch gescheitert → Home
            if path != HOME:
                self.state.path = str(HOME)
                self.load(HOME)
        self.update_status()

    def _loaded(self, path: Path, entries: list[Entry], mtime: float, keep: str | None) -> None:
        if not self.is_attached:
            return
        changed_dir = str(path) != self.state.path or not self.all_entries
        if str(path) != self.state.path:
            self.marked.clear()
            self.filter_text = ""
            self.query_one(".filter", Input).value = ""
        self.state.path = str(path)
        self.all_entries = entries
        self._dir_mtime = mtime
        self.loading = False
        if changed_dir and keep is None:
            keep = ".." if path.parent != path else None
        self.render_rows(keep=keep, reset=changed_dir and keep in (None, ".."))
        self.update_pathbar()
        self.screen.pane_changed(self)  # type: ignore[attr-defined]

    def reload(self, keep: str | None = None) -> None:
        cur = self.current
        self.load(self.path, keep=keep or (cur.name if cur else None))

    def check_changed(self) -> None:
        if self.loading:
            return
        try:
            m = self.path.stat().st_mtime
        except OSError:
            return
        if m != self._dir_mtime:
            self.reload()

    # ---------- Darstellung ----------
    def render_rows(self, keep: str | None = None, reset: bool = False) -> None:
        t = self.table
        if keep is None and not reset:
            cur = self.current
            keep = cur.name if cur else None
        show_hidden = self.cfg["show_hidden"]
        items = [e for e in self.all_entries if show_hidden or not e.hidden]
        if self.filter_text:
            f = self.filter_text.lower()
            items = [e for e in items if f in e.name.lower()]
        items = sort_entries(items, self.state.sort, self.state.rev)
        if self.path.parent != self.path and not self.filter_text:
            items.insert(
                0,
                Entry(name="..", path=self.path.parent, is_dir=True, is_parent=True, kind="parent"),
            )
        self.view = items

        width = t.size.width or 80
        t._rendered_width = t.size.width
        show_size = width >= 34
        show_date = width >= 60
        fixed = 1  # Scrollbar
        if show_size:
            fixed += 9 + 2
        if show_date:
            fixed += 14 + 2
        name_w = max(8, width - fixed - 2)
        arrow = " ▼" if self.state.rev else " ▲"
        lbl = lambda key, text: text + (arrow if self.state.sort == key else "")  # noqa: E731

        t.clear(columns=True)
        t.add_column(Text(lbl("name", "Name"), style="bold"), width=name_w, key="name")
        if show_size:
            t.add_column(Text(lbl("size", "Größe"), style="bold", justify="right"), width=9, key="size")
        if show_date:
            t.add_column(Text(lbl("date", "Geändert"), style="bold"), width=14, key="date")

        mode = self.cfg["icons"]
        rows = []
        for e in items:
            rows.append(self._row(e, name_w, show_size, show_date, mode))
        t.add_rows(rows)

        idx = 0
        if keep is not None:
            for i, e in enumerate(items):
                if e.name == keep:
                    idx = i
                    break
        if reset and items and items[0].is_parent and len(items) > 1:
            idx = 1 if keep in (None, "..") else idx
        if items:
            t.move_cursor(row=min(idx, len(items) - 1), animate=False)
        self.update_status()

    def _row(self, e: Entry, name_w: int, show_size: bool, show_date: bool, mode: str) -> list:
        style = KINDS[e.kind][3]
        if e.hidden:
            style += " dim"
        marked = e.name in self.marked
        icon = icon_for(e.kind, mode)
        if marked:
            icon = "✔ " if mode == "emoji" else "✔"
            style = "bold #ffcc00"
        name = Text(no_wrap=True, overflow="ellipsis")
        name.append(icon + " ", style=style if marked else KINDS[e.kind][3])
        name.append(e.name, style=style)
        if e.is_dir and not e.is_parent:
            name.append("/", style="dim")
        if e.is_link and e.target:
            name.append(f" → {e.target}", style="dim italic")
        name.truncate(name_w, overflow="ellipsis")
        cells: list = [name]
        if show_size:
            if e.is_parent:
                s = Text("")
            elif e.is_dir:
                s = Text("—", style="dim", justify="right")
            else:
                s = Text(human_size(e.size), style="#8be9fd" if e.size >= 100 * 1024 * 1024 else "", justify="right")
            cells.append(s)
        if show_date:
            cells.append(Text("" if e.is_parent else fmt_time(e.mtime), style="dim"))
        return cells

    def update_pathbar(self) -> None:
        bar = self.query_one(".pathbar", ClickBar)
        p = self.path
        segs: list[tuple[str, Path]] = []
        parts = p.parts
        if p == HOME or HOME in p.parents:
            segs.append(("🏠 ~" if self.cfg["icons"] == "emoji" else "~", HOME))
            base = HOME
            rest = p.relative_to(HOME).parts
        elif len(parts) >= 3 and parts[1] == "mnt" and len(parts[2]) == 1:
            drive = Path("/mnt") / parts[2]
            segs.append((("💽 " if self.cfg["icons"] == "emoji" else "") + parts[2].upper() + ":", drive))
            base = drive
            rest = parts[3:]
        else:
            segs.append(("/", Path("/")))
            base = Path("/")
            rest = parts[1:]
        cur = base
        for part in rest:
            cur = cur / part
            segs.append((part, cur))

        width = max(10, bar.size.width - 2) if bar.size.width else 200
        sep = " › "
        # von vorne kürzen, bis es passt
        def total(s: list) -> int:
            return sum(Text(x[0]).cell_len for x in s) + len(sep) * (len(s) - 1)

        shown = list(segs)
        cut = False
        while len(shown) > 2 and total(shown) + (2 + len(sep) if cut else 0) > width:
            shown.pop(1)
            cut = True
        items: list[tuple[Text, Callable[[], None] | None]] = []
        for i, (label, target) in enumerate(shown):
            if i == 1 and cut:
                items.append((Text("…" + sep, style="dim"), None))
            elif i > 0:
                items.append((Text(sep, style="dim"), None))
            last = i == len(shown) - 1
            items.append(
                (
                    Text(label, style="bold reverse" if last else "bold #61afef"),
                    (lambda t=target: self.goto(t)),
                )
            )
        bar.set_items(items)
        name = p.name or str(p)
        self.border_title = Text(f" {name} ", style="bold")

    def update_status(self) -> None:
        if self.loading:
            self.border_subtitle = Text(" lädt … ", style="italic")
            return
        dirs = sum(1 for e in self.view if e.is_dir and not e.is_parent)
        files = [e for e in self.view if not e.is_dir]
        total = sum(e.size for e in files)
        parts = [f"{dirs} Ordner", f"{len(files)} Dateien", human_size(total)]
        if self.marked:
            parts.append(f"✔ {len(self.marked)} markiert")
        if self.filter_text:
            parts.append("Filter aktiv")
        parts.append(f"Sort: {SORT_LABEL[self.state.sort]}{' ▼' if self.state.rev else ''}")
        self.border_subtitle = Text(" " + " · ".join(parts) + " ")

    # ---------- Navigation ----------
    def goto(self, path: Path, keep: str | None = None) -> None:
        path = Path(os.path.abspath(os.path.expanduser(str(path))))
        if not path.is_dir():
            self.app.notify(f"{path} ist kein Ordner", severity="warning")
            return
        if not os.access(path, os.R_OK | os.X_OK):
            self.app.notify(f"Keine Berechtigung für {path}", severity="error")
            return
        self.load(path, keep=keep)

    def go_parent(self) -> None:
        p = self.path
        if p.parent != p:
            self.goto(p.parent, keep=p.name)

    def open_entry(self) -> None:
        e = self.current
        if e is None:
            return
        if e.is_parent:
            self.go_parent()
        elif e.is_dir:
            self.goto(e.path)
        elif e.broken:
            self.app.notify("Defekter Symlink", severity="warning")
        elif opens_in_editor(e):
            self.screen.open_in_editor(e.path)  # type: ignore[attr-defined]
        else:
            self.open_in_windows()

    def open_in_windows(self) -> None:
        """Öffnet den Eintrag mit dem Windows-Standardprogramm (außerhalb von WSL: xdg-open)."""
        e = self.current
        if e is None or e.is_parent:
            return
        if e.broken:
            self.app.notify("Defekter Symlink", severity="warning")
        elif open_external(e.path):
            self.app.notify(f"Öffne {e.name} …", timeout=2, markup=False)
        else:
            self.app.notify("Kein Programm zum Öffnen gefunden", severity="error")

    def entry_clicked(self) -> None:
        e = self.current
        if e and not e.is_parent and self.cfg["click_copy"]:
            self.app.sys_clipboard.copy(e.name)  # type: ignore[attr-defined]
            self.app.notify(f"📋 {e.name}", title="Name kopiert", timeout=2)

    # ---------- Markieren / Sortieren / Filter ----------
    def toggle_mark(self) -> None:
        e = self.current
        if e and not e.is_parent:
            self.marked ^= {e.name}
            row = self.table.cursor_row
            self.render_rows(keep=e.name)
            if row + 1 < len(self.view):
                self.table.move_cursor(row=row + 1, animate=False)

    def mark_all(self) -> None:
        names = {e.name for e in self.view if not e.is_parent}
        self.marked = set() if names and names <= self.marked else names
        self.render_rows()

    def sort_by_column(self, col: int) -> None:
        keys = [str(c.key.value) for c in self.table.ordered_columns]
        if not (0 <= col < len(keys)):
            return
        key = keys[col]
        if self.state.sort == key:
            self.state.rev = not self.state.rev
        else:
            self.state.sort, self.state.rev = key, False
        self.render_rows()

    def cycle_sort(self) -> None:
        i = SORTS.index(self.state.sort)
        self.state.sort = SORTS[(i + 1) % len(SORTS)]
        self.state.rev = False
        self.render_rows()
        self.app.notify(f"Sortierung: {SORT_LABEL[self.state.sort]}", timeout=1.5)

    def action_filter(self) -> None:
        inp = self.query_one(".filter", Input)
        inp.add_class("-on")
        inp.focus()

    def action_clear_filter(self) -> None:
        inp = self.query_one(".filter", Input)
        if inp.has_class("-on") or self.filter_text:
            inp.value = ""
            inp.remove_class("-on")
            self.filter_text = ""
            self.render_rows()
            self.table.focus()
        elif self.marked:
            self.marked.clear()
            self.render_rows()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.has_class("filter"):
            self.filter_text = event.value
            self.render_rows(reset=True)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.has_class("filter"):
            if not event.value:
                event.input.remove_class("-on")
            self.table.focus()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self.screen.highlight_changed(self)  # type: ignore[attr-defined]

    # ---------- Kontextmenü ----------
    def open_context_menu(self, x: int, y: int) -> None:
        e = self.current
        if e is None:
            return
        items: list[tuple[str, str, str]] = []
        if not e.is_parent:
            items += [
                ("copy_name", "Name kopieren", "c"),
                ("copy_path", "Pfad kopieren", "p"),
            ]
            if IS_WSL:
                items.append(("copy_win", "Windows-Pfad kopieren", "w"))
            items.append(("sep", "", ""))
        items.append(("open", "Öffnen", "Enter"))
        if not e.is_dir:
            items.append(("open_windows", "In Windows öffnen" if IS_WSL else "Mit System öffnen", "O"))
        if IS_WSL:
            items.append(("reveal", "Im Explorer zeigen", "o"))
        if shutil.which("code"):
            items.append(("vscode", "In VS Code öffnen", "C"))
        items.append(("terminal", "Terminal hier", "t"))
        if not e.is_parent:
            items += [
                ("sep", "", ""),
                ("mark", "Markieren", "Leer"),
                ("rename", "Umbenennen", "F2"),
                ("copy_to", "Kopieren nach …", "F5"),
                ("move_to", "Verschieben nach …", "F6"),
                ("delete", "Löschen", "Entf"),
            ]
        items += [("sep", "", ""), ("mkdir", "Neuer Ordner", "F7"), ("newfile", "Neue Datei", "n")]

        def done(result: str | None) -> None:
            if result:
                self.screen.run_menu_action(result)  # type: ignore[attr-defined]

        self.app.push_screen(ContextMenu(items, x, y), done)


# --------------------------------------------------------------------------- #
# Vorschau
# --------------------------------------------------------------------------- #


class Preview(VerticalScroll):
    can_focus = False

    def compose(self) -> ComposeResult:
        yield Static(id="pv-body")

    def show(self, e: Entry | None, icons: str) -> None:
        body = self.query_one("#pv-body", Static)
        self.scroll_home(animate=False)
        if e is None:
            body.update("")
            self.border_title = "Vorschau"
            return
        self.border_title = Text(f" {e.name} ", style="bold")
        self.border_subtitle = ""
        try:
            if e.is_dir:
                body.update(self._dir_preview(e.path, icons))
            else:
                body.update(self._file_preview(e))
        except OSError as exc:
            body.update(Text(f"Nicht lesbar: {exc.strerror or exc}", style="red"))

    def _dir_preview(self, path: Path, icons: str) -> Text:
        t = Text()
        n = 0
        with os.scandir(path) as it:
            for de in it:
                if de.name.startswith("."):
                    continue
                try:
                    isdir = de.is_dir()
                except OSError:
                    isdir = False
                kind = "dir" if isdir else (_EXT.get(os.path.splitext(de.name.lower())[1], "file"))
                t.append(icon_for(kind, icons) + " ", style=KINDS[kind][3])
                t.append(de.name + ("/" if isdir else "") + "\n", style=KINDS[kind][3])
                n += 1
                if n >= 200:
                    t.append("…\n", style="dim")
                    break
        if n == 0:
            t.append("(leer)", style="dim italic")
        self.border_subtitle = f" {n}{'+' if n >= 200 else ''} Einträge "
        return t

    def _file_preview(self, e: Entry):
        st = e.path.stat()
        self.border_subtitle = f" {human_size(st.st_size)} "
        info = Text()
        info.append(f"{icon_for(e.kind, 'emoji')}  {e.name}\n\n", style="bold")
        info.append(f"Größe:     {human_size(st.st_size)}\n")
        info.append(f"Geändert:  {datetime.fromtimestamp(st.st_mtime):%d.%m.%Y %H:%M:%S}\n")
        info.append(f"Rechte:    {stat.filemode(st.st_mode)}\n")
        if e.is_link:
            info.append(f"Link auf:  {e.target}\n")
        if IS_WSL:
            info.append(f"Windows:   {to_windows_path(e.path)}\n", style="dim")
        if e.kind in {"image", "audio", "video", "archive", "winexe", "pdf"} or st.st_size == 0:
            return info
        with open(e.path, "rb") as fh:
            raw = fh.read(64 * 1024)
        if b"\x00" in raw[:4096]:
            info.append("\n(Binärdatei)", style="dim italic")
            return info
        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines()[:400]
        code = "\n".join(lines)
        try:
            lexer = Syntax.guess_lexer(str(e.path), code=code)
        except Exception:  # noqa: BLE001
            lexer = "text"
        bg = self.app.get_css_variables().get("background", "#000000")
        return Syntax(
            code,
            lexer,
            theme="monokai",
            line_numbers=True,
            word_wrap=False,
            background_color=bg,
            indent_guides=True,
        )


# --------------------------------------------------------------------------- #
# Eingebettetes Terminal (PTY + pyte)
# --------------------------------------------------------------------------- #

try:
    import fcntl
    import signal
    import struct
    import termios

    import pyte
except ImportError:  # pragma: no cover
    pyte = None  # type: ignore[assignment]

from rich.style import Style  # noqa: E402
from textual.widget import Widget  # noqa: E402

_MODE_DECCKM = 1 << 5  # pyte speichert private Modi um 5 Bit verschoben
_MODE_BRACKETED_PASTE = 2004 << 5

_KEY_SEQ = {
    "enter": "\r",
    "tab": "\t",
    "shift+tab": "\x1b[Z",
    "backspace": "\x7f",
    "ctrl+h": "\x08",
    "escape": "\x1b",
    "delete": "\x1b[3~",
    "insert": "\x1b[2~",
    "pageup": "\x1b[5~",
    "pagedown": "\x1b[6~",
    "home": "\x1b[H",
    "end": "\x1b[F",
    "ctrl+space": "\x00",
    "ctrl+@": "\x00",
    "f1": "\x1bOP",
    "f2": "\x1bOQ",
    "f3": "\x1bOR",
    "f4": "\x1bOS",
    "f5": "\x1b[15~",
    "f6": "\x1b[17~",
    "f7": "\x1b[18~",
    "f8": "\x1b[19~",
    "f9": "\x1b[20~",
    "f10": "\x1b[21~",
    "f11": "\x1b[23~",
}
_ARROWS = {"up": "A", "down": "B", "right": "C", "left": "D"}
_ALT_SCREEN_MODES = {47 << 5, 1047 << 5, 1049 << 5}  # Vollbildprogramme wie nano, less, vim
_WHEEL_LINES = 3


def wheel_input(up: bool, alt_screen: bool, app_cursor: bool) -> str | None:
    """Eingabe fürs Mausrad: im Vollbildprogramm Pfeiltasten (wie Windows Terminal),
    sonst None – dann blättert das Terminal im eigenen Verlauf."""
    if not alt_screen:
        return None
    arrow = ("\x1bO" if app_cursor else "\x1b[") + ("A" if up else "B")
    return arrow * _WHEEL_LINES


# --------------------------------------------------------------------------- #
# Terminal-Textauswahl (reine Logik – ohne UI testbar)
# --------------------------------------------------------------------------- #

Cell = tuple[int, int]  # (Spalte, Zeile) im sichtbaren Terminalbereich


def _reading_order(cell: Cell) -> tuple[int, int]:
    return cell[1], cell[0]


@dataclass(frozen=True)
class ScreenLine:
    """Eine Terminalzeile als Zellen ("" = rechte Hälfte eines doppelt breiten Zeichens)."""

    cells: tuple[str, ...]
    wrapped: bool = False  # endet durch automatischen Umbruch und geht in der nächsten Zeile weiter


@dataclass(frozen=True)
class TextSelection:
    """Fließtext-Auswahl wie in Terminals üblich: von `anchor` bis `cursor`, beide Zellen inklusive."""

    anchor: Cell
    cursor: Cell

    @property
    def start(self) -> Cell:
        return min(self.anchor, self.cursor, key=_reading_order)

    @property
    def end(self) -> Cell:
        return max(self.anchor, self.cursor, key=_reading_order)

    def contains(self, x: int, y: int) -> bool:
        return _reading_order(self.start) <= (y, x) <= _reading_order(self.end)

    def extract(self, lines: Sequence[ScreenLine]) -> str:
        """Der markierte Text 1:1 – umbrochene Zeilen ohne künstlichen Zeilenumbruch,
        Füll-Leerzeichen am Zeilenende und leere Zeilen am Ende der Auswahl entfernt."""
        (sx, sy), (ex, ey) = self.start, self.end
        ey = min(ey, len(lines) - 1)
        parts: list[str] = []
        for y in range(sy, ey + 1):
            cells = lines[y].cells
            x0 = sx if y == sy else 0
            x1 = ex if y == ey else len(cells) - 1
            if 0 < x0 < len(cells) and cells[x0] == "":
                x0 -= 1  # Auswahl beginnt auf der rechten Hälfte eines breiten Zeichens
            text = "".join(cells[x0 : x1 + 1])
            continues = y < ey and lines[y].wrapped
            parts.append(text if continues else text.rstrip(" "))
            if y < ey and not continues:
                parts.append("\n")
        return "".join(parts).rstrip("\n")


def screen_lines(screen) -> list[ScreenLine]:
    """Der sichtbare Inhalt eines pyte-Screens als ScreenLines."""
    columns = range(screen.columns)
    lines = []
    for y in range(screen.lines):
        row = screen.buffer[y]
        lines.append(ScreenLine(tuple(row[x].data for x in columns), getattr(row, "wrapped", False)))
    return lines


class WrapTrackingScreen(pyte.HistoryScreen if pyte is not None else object):  # type: ignore[misc]
    """pyte-HistoryScreen, der sich merkt, welche Zeilen durch automatischen Umbruch enden.

    Das Flag hängt an der Zeile selbst und wandert so beim Scrollen mit in den Verlauf.
    Damit lässt sich ein umbrochener Befehl oder eine lange URL 1:1 kopieren.
    """

    _in_draw = False

    @staticmethod
    def _mark(row, wrapped: bool) -> None:
        row.wrapped = wrapped

    def draw(self, data: str) -> None:
        self._in_draw = True
        try:
            super().draw(data)
        finally:
            self._in_draw = False

    def linefeed(self) -> None:
        # Innerhalb von draw() gibt es einen Zeilenvorschub nur beim automatischen Umbruch;
        # ein "\n" des Programms beendet die Zeile dagegen explizit.
        self._mark(self.buffer[self.cursor.y], self._in_draw)
        super().linefeed()

    def erase_in_line(self, how: int = 0, private: bool = False) -> None:
        super().erase_in_line(how, private)
        if how in (0, 2):
            self._mark(self.buffer[self.cursor.y], False)

    def erase_in_display(self, how: int = 0, *args, **kwargs) -> None:
        super().erase_in_display(how, *args, **kwargs)
        rows = {0: range(self.cursor.y + 1, self.lines), 1: range(self.cursor.y)}.get(how, range(self.lines))
        for y in rows:
            self._mark(self.buffer[y], False)


def _pyte_color(c: str) -> str | None:
    if c == "default":
        return None
    if c.startswith("bright"):
        base = c[6:]
        return "bright_" + ("yellow" if base == "brown" else base)
    if c == "brown":
        return "yellow"
    if len(c) == 6 and all(ch in "0123456789abcdefABCDEF" for ch in c):
        return "#" + c
    return c


def copied_notice(text: str) -> str:
    """Kurzer Toast-Text zu kopiertem Text: erste Zeile (gekürzt) und Anzahl weiterer Zeilen."""
    first, *rest = text.split("\n")
    if len(first) > 60:
        first = first[:59] + "…"
    more = f"  (+{len(rest)} Zeile{'' if len(rest) == 1 else 'n'})" if rest else ""
    return f"📋 {first}{more}"


class TerminalView(Widget, can_focus=True):
    """Ein echtes Shell-Terminal als Widget (Bash in einem Pseudo-Terminal).

    Markieren mit der Maus kopiert beim Loslassen sofort in die Zwischenablage.
    """

    DEFAULT_CSS = "TerminalView { height: 1fr; width: 1fr; }"
    ALLOW_SELECT = False  # eigene Auswahl statt der von Textual (die nur per Ctrl+C kopiert)

    class Exited(Message):
        """Der Prozess im Terminal hat sich beendet."""

        def __init__(self, view: "TerminalView") -> None:
            super().__init__()
            self.view = view

        @property
        def control(self) -> "TerminalView":
            return self.view

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.fd: int | None = None
        self.proc: subprocess.Popen | None = None
        self.pscreen = None
        self.stream = None
        self.lock = threading.Lock()
        self._dirty = False
        self._styles: dict[tuple, Style] = {}
        self.start_dir: str = str(HOME)
        self.command: list[str] | None = None  # None = interaktive Shell
        self.selection: TextSelection | None = None
        self._sel_anchor: Cell | None = None

    # ---------- Prozess ----------
    @property
    def running(self) -> bool:
        return self.fd is not None and self.proc is not None and self.proc.poll() is None

    def reset_screen(self) -> None:
        """Legt einen frischen, zur Widget-Größe passenden Terminal-Bildschirm an."""
        cols, rows = max(self.size.width, 20), max(self.size.height, 3)
        screen = WrapTrackingScreen(cols, rows, history=5000, ratio=0.5)
        screen.write_process_input = self._write_str  # Antworten auf Terminal-Abfragen
        with self.lock:
            self.pscreen = screen
            self.stream = pyte.ByteStream(screen)
        self._set_selection(None)

    def feed(self, data: bytes) -> None:
        """Verarbeitet Ausgabe des Programms (thread-sicher; neu gezeichnet wird im Tick)."""
        with self.lock:
            if self.stream is not None:
                self.stream.feed(data)
        self._dirty = True

    def start(self) -> None:
        if pyte is None or self.running:
            return
        self.reset_screen()
        cols, rows = self.pscreen.columns, self.pscreen.lines
        master, slave = os.openpty()
        self._set_winsize(slave, rows, cols)
        env = dict(os.environ, TERM="xterm-256color", COLORTERM="truecolor", WSLUI_ACTIVE="1")
        for k in ("COLUMNS", "LINES"):
            env.pop(k, None)
        argv = self.command or [os.environ.get("SHELL") or "/bin/bash", "-i"]
        # eigene Sitzung mit dem PTY als Kontrollterminal (Job-Control, Ctrl+C/Ctrl+Z)
        cmd = ["setsid", "-c", "-w", *argv] if shutil.which("setsid") else argv
        cwd = self.start_dir if os.path.isdir(self.start_dir) else str(HOME)
        try:
            self.proc = subprocess.Popen(
                cmd, stdin=slave, stdout=slave, stderr=slave, cwd=cwd, env=env, close_fds=True
            )
        finally:
            os.close(slave)
        self.fd = master
        threading.Thread(target=self._reader, args=(master,), daemon=True).start()

    def _reader(self, fd: int) -> None:
        while True:
            try:
                data = os.read(fd, 65536)
            except OSError:
                break
            if not data:
                break
            self.feed(data)
        try:
            self.app.call_from_thread(self._on_exited, fd)
        except Exception:  # noqa: BLE001  (App bereits beendet)
            pass

    def _on_exited(self, fd: int) -> None:
        if self.fd == fd:
            try:
                os.close(fd)
            except OSError:
                pass
            self.fd = None
            self.post_message(self.Exited(self))

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                os.kill(self.proc.pid, signal.SIGHUP)
            except OSError:
                pass
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    @staticmethod
    def _set_winsize(fd: int, rows: int, cols: int) -> None:
        try:
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass

    def _write_str(self, data: str) -> None:
        self.write(data.encode("utf-8", errors="replace"))

    def write(self, data: bytes) -> None:
        if self.fd is None:
            return
        try:
            os.write(self.fd, data)
        except OSError:
            pass

    def is_idle(self) -> bool:
        """True, wenn die Shell selbst im Vordergrund ist (kein Programm läuft)."""
        if not self.running:
            return False
        try:
            return os.tcgetpgrp(self.fd) == self.proc.pid  # type: ignore[arg-type,union-attr]
        except OSError:
            return False

    def shell_cwd(self) -> str | None:
        if not self.running:
            return None
        try:
            return os.readlink(f"/proc/{self.proc.pid}/cwd")  # type: ignore[union-attr]
        except OSError:
            return None

    def cd(self, path: str) -> None:
        # führendes Leerzeichen: landet (bei HISTCONTROL=ignorespace/ignoreboth) nicht in der History
        self.write(("\x15 cd -- " + shlex.quote(path) + "\r").encode())

    # ---------- Lebenszyklus ----------
    def on_mount(self) -> None:
        self.set_interval(1 / 30, self._tick)

    def on_unmount(self) -> None:
        self.stop()

    def _tick(self) -> None:
        if self._dirty:
            self._dirty = False
            self.refresh()

    def on_resize(self, event: events.Resize) -> None:
        cols, rows = event.size.width, event.size.height
        if cols < 2 or rows < 1:
            return
        if self.pscreen is None:
            return
        with self.lock:
            self.pscreen.resize(rows, cols)
        if self.fd is not None:
            self._set_winsize(self.fd, rows, cols)
        self._set_selection(None)
        self.refresh()

    # ---------- Eingabe ----------
    def _scroll_to_bottom(self) -> None:
        scr = self.pscreen
        if scr is None:
            return
        with self.lock:
            guard = 0
            while scr.history.position < scr.history.size and guard < 1000:
                scr.next_page()
                guard += 1

    def _key_bytes(self, event: events.Key) -> str | None:
        k = event.key
        if k in _ARROWS or (k.startswith(("ctrl+", "shift+", "alt+")) and k.split("+")[-1] in _ARROWS):
            mods = k.split("+")[:-1]
            letter = _ARROWS[k.split("+")[-1]]
            if not mods:
                app_cursor = self.pscreen is not None and _MODE_DECCKM in self.pscreen.mode
                return ("\x1bO" if app_cursor else "\x1b[") + letter
            code = 1 + ("shift" in mods) + 2 * ("alt" in mods) + 4 * ("ctrl" in mods)
            return f"\x1b[1;{code}{letter}"
        if k in _KEY_SEQ:
            return _KEY_SEQ[k]
        if k.startswith("ctrl+") and len(k) == 6 and k[5].isalpha():
            return chr(ord(k[5].lower()) - 96)
        if k.startswith("alt+") and event.character:
            return "\x1b" + event.character
        if event.character and event.is_printable:
            return event.character
        return None

    async def on_key(self, event: events.Key) -> None:
        self._set_selection(None)
        if not self.running:
            if event.key == "enter":
                event.stop()
                event.prevent_default()
                self.start()
            return
        if event.key in ("ctrl+v", "ctrl+shift+v", "shift+insert"):
            event.stop()
            event.prevent_default()
            self.paste_from_clipboard()
            return
        data = self._key_bytes(event)
        if data is None:
            return
        event.stop()
        event.prevent_default()
        self._scroll_to_bottom()
        self.write(data.encode("utf-8", errors="replace"))

    def paste_text(self, raw: str) -> None:
        """Fügt bereinigten Text ein – ohne Steuerzeichen-Müll, ohne Auto-Ausführen."""
        text = sanitize_paste(raw)
        if not text or not self.running:
            return
        self._scroll_to_bottom()
        data = text.replace("\n", "\r")
        # Bracketed Paste nur, wenn das Programm es selbst angefordert hat (z. B. bash/readline).
        # Dann sind die Marker unsichtbar; sonst würden sie als „~[[200~“ o. Ä. auftauchen.
        if self.pscreen is not None and _MODE_BRACKETED_PASTE in self.pscreen.mode:
            data = "\x1b[200~" + data + "\x1b[201~"
        self.write(data.encode("utf-8", errors="replace"))

    @work(thread=True, exclusive=True, group="paste")
    def paste_from_clipboard(self) -> None:
        text = self.app.sys_clipboard.paste()  # type: ignore[attr-defined]
        self.app.call_from_thread(self.paste_text, text)

    def on_paste(self, event: events.Paste) -> None:
        if not self.running:
            return
        event.stop()
        self.paste_text(event.text)

    @property
    def in_alt_screen(self) -> bool:
        """True, solange ein Vollbildprogramm (nano, less, vim …) läuft."""
        return self.pscreen is not None and not _ALT_SCREEN_MODES.isdisjoint(self.pscreen.mode)

    def _scroll(self, up: bool) -> None:
        scr = self.pscreen
        if scr is None:
            return
        keys = wheel_input(up, self.in_alt_screen, _MODE_DECCKM in scr.mode) if self.running else None
        if keys is not None:
            self.write(keys.encode())
            return
        with self.lock:
            if up:
                scr.prev_page()
            else:
                scr.next_page()
        self._set_selection(None)
        self.refresh()

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        event.stop()
        self._scroll(up=True)

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        event.stop()
        self._scroll(up=False)

    def on_click(self, event: events.Click) -> None:
        self.focus()
        if event.button == 3:  # Rechtsklick = Einfügen (wie in der Windows-Konsole)
            event.stop()
            self.paste_from_clipboard()

    # ---------- Maus: Markieren = Kopieren ----------
    def _set_selection(self, selection: TextSelection | None) -> None:
        if selection != self.selection:
            self.selection = selection
            self.refresh()

    def _cell_at(self, event: events.MouseEvent) -> Cell:
        """Zelle unter dem Mauszeiger – auf den Terminalbereich begrenzt (auch beim Ziehen nach außen)."""
        scr = self.pscreen
        return min(max(event.x, 0), scr.columns - 1), min(max(event.y, 0), scr.lines - 1)

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button != 1 or self.pscreen is None:
            return
        self._sel_anchor = self._cell_at(event)
        self._set_selection(None)
        self.capture_mouse()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self._sel_anchor is None:
            return
        cell = self._cell_at(event)
        if self.selection is not None or cell != self._sel_anchor:
            self._set_selection(TextSelection(self._sel_anchor, cell))

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if self._sel_anchor is None:
            return
        self._sel_anchor = None
        self.release_mouse()
        if self.selection is not None:
            self._copy_selection(self.selection)

    def _copy_selection(self, selection: TextSelection) -> None:
        with self.lock:
            text = selection.extract(screen_lines(self.pscreen))
        if text:
            self.app.sys_clipboard.copy(text)  # type: ignore[attr-defined]
            self.app.notify(copied_notice(text), title="Text kopiert", timeout=2.5, markup=False)

    # ---------- Darstellung ----------
    def _style(self, ch, invert: bool) -> Style:
        key = (ch.fg, ch.bg, ch.bold, ch.italics, ch.underscore, ch.strikethrough, ch.reverse, invert)
        st = self._styles.get(key)
        if st is None:
            st = Style(
                color=_pyte_color(ch.fg),
                bgcolor=_pyte_color(ch.bg),
                bold=ch.bold or None,
                italic=ch.italics or None,
                underline=ch.underscore or None,
                strike=ch.strikethrough or None,
                reverse=(ch.reverse != invert) or None,
            )
            self._styles[key] = st
        return st

    def render(self) -> Text:
        if pyte is None:
            return Text("Das Python-Paket 'pyte' fehlt – bitte install.sh erneut ausführen.", style="red")
        scr = self.pscreen
        if scr is None:
            return Text("Terminal startet …", style="dim")
        out = Text(no_wrap=True, overflow="crop")
        sel = self.selection
        with self.lock:
            cur = scr.cursor
            scrolled = scr.history.position < scr.history.size
            show_cursor = self.has_focus and not cur.hidden and not scrolled
            for y in range(scr.lines):
                row = scr.buffer[y]
                run: list[str] = []
                run_style: Style | None = None
                for x in range(scr.columns):
                    ch = row[x]
                    if ch.data == "":
                        continue
                    is_cursor = show_cursor and y == cur.y and x == cur.x
                    is_selected = sel is not None and sel.contains(x, y)
                    st = self._style(ch, is_cursor != is_selected)
                    if st is not run_style and run:
                        out.append("".join(run), run_style)
                        run = []
                    run_style = st
                    run.append(ch.data)
                if run:
                    out.append("".join(run), run_style)
                if y < scr.lines - 1:
                    out.append("\n")
        return out


class TerminalPanel(Vertical):
    DEFAULT_CSS = """
    TerminalPanel {
        display: none; height: 40%; min-height: 6;
        border: round $panel-lighten-2; border-title-color: $text-muted;
        border-subtitle-color: $text-muted; border-subtitle-align: right;
    }
    TerminalPanel.-on { display: block; }
    TerminalPanel.-big { height: 75%; }
    TerminalPanel:focus-within { border: round $success; border-title-color: $success; }
    """

    def compose(self) -> ComposeResult:
        yield TerminalView(id="term-view")

    def on_mount(self) -> None:
        self.border_title = Text(" >_ Terminal ", style="bold")
        self.border_subtitle = Text(" Ctrl+T ein/aus · F12 Fenster wechseln · Ctrl+↑ größer ")


class EditorPanel(Vertical):
    """Eingebetteter Editor (standardmäßig nano) rechts neben den Dateifenstern.

    Schließt sich von selbst, sobald der Editor beendet wird (bei nano: Ctrl+X).
    """

    DEFAULT_CSS = """
    EditorPanel {
        display: none; width: 55%; height: 1fr;
        border: round $panel-lighten-2; border-title-color: $text-muted;
        border-subtitle-color: $text-muted; border-subtitle-align: right;
    }
    EditorPanel.-on { display: block; }
    EditorPanel:focus-within { border: round $success; border-title-color: $success; }
    """

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.path: Path | None = None

    def compose(self) -> ComposeResult:
        yield TerminalView(id="editor-view")

    @property
    def view(self) -> TerminalView:
        return self.query_one("#editor-view", TerminalView)

    @property
    def is_open(self) -> bool:
        return self.has_class("-on")

    def open(self, path: Path, argv: list[str]) -> None:
        self.path = path
        self.border_title = Text(f" ✎ {path.name} ", style="bold")
        self.border_subtitle = Text(" Ctrl+X schließen · F12 Fenster wechseln ")
        self.add_class("-on")
        view = self.view
        view.command = argv
        view.start_dir = str(path.parent)
        self.call_after_refresh(view.start)  # erst nach dem Layout, damit die Größe stimmt
        view.focus()

    def close(self) -> None:
        self.view.stop()
        self.remove_class("-on")
        self.path = None


# --------------------------------------------------------------------------- #
# Drag & Drop (UI)
# --------------------------------------------------------------------------- #


class DragGhost(Static):
    """Schwebendes Etikett am Mauszeiger, solange Dateien gezogen werden."""

    DEFAULT_CSS = """
    DragGhost {
        display: none; overlay: screen; position: absolute;
        width: auto; height: 1; padding: 0 1;
        background: $accent; color: $text; text-style: bold;
    }
    DragGhost.-on { display: block; }
    """

    def show_at(self, x: int, y: int, label: Text) -> None:
        self.update(label)
        self.absolute_offset = Offset(x + 2, y)
        self.add_class("-on")
        self.refresh(layout=True)

    def hide(self) -> None:
        self.remove_class("-on")


class DragController:
    """Drag & Drop von Dateien zwischen (und innerhalb von) Dateifenstern.

    Ablauf: begin → move … → drop (oder cancel). Beim Ablegen entscheidet Strg/Shift
    direkt (kopieren/verschieben), sonst fragt ein kleines Menü nach – mit dem
    Windows-Vorschlag (gleiches Laufwerk: verschieben, sonst kopieren) oben.
    """

    def __init__(self, screen: "MainScreen") -> None:
        self._screen = screen
        self._entries: list[Entry] = []
        self._highlighted: Pane | None = None

    @property
    def active(self) -> bool:
        return bool(self._entries)

    @property
    def _sources(self) -> list[Path]:
        return [e.path for e in self._entries]

    @property
    def _ghost(self) -> DragGhost:
        return self._screen.query_one(DragGhost)

    def begin(self, source: Pane, entries: list[Entry]) -> None:
        self._entries = list(entries)

    def move(self, x: int, y: int) -> None:
        target = self._target_at(x, y)
        self._highlight(target[0] if target else None)
        self._ghost.show_at(x, y, self._label(target[1] if target else None))

    def drop(self, x: int, y: int, ctrl: bool = False, shift: bool = False) -> None:
        target, sources = self._target_at(x, y), self._sources
        self.cancel()
        if target is None:
            return
        directory = target[1]
        action = drop_action_from_modifiers(ctrl, shift)
        if action is not None:
            self._screen.transfer(action, sources, directory)
            return

        def chosen(result: str | None) -> None:
            if result in ("copy", "move"):
                self._screen.transfer(result, sources, directory)  # type: ignore[arg-type]

        menu = ContextMenu(drop_menu_items(preferred_drop_action(sources, directory)), x, y)
        self._screen.app.push_screen(menu, chosen)

    def cancel(self) -> None:
        self._entries = []
        self._highlight(None)
        self._ghost.hide()

    def _target_at(self, x: int, y: int) -> tuple[Pane, Path] | None:
        """Fenster und Zielordner unter dem Mauszeiger – None, wo Ablegen nichts bewirkt."""
        try:
            widget, _ = self._screen.get_widget_at(x, y)
        except NoWidget:
            return None
        pane = next((n for n in widget.ancestors_with_self if isinstance(n, Pane)), None)
        if pane is None:
            return None
        hovered = None
        if isinstance(widget, FileTable):
            row = self._screen.get_style_at(x, y).meta.get("row")
            hovered = pane.entry_at(row) if isinstance(row, int) else None
        directory = drop_dir(pane.path, hovered, self._sources)
        if is_noop_drop(self._sources, directory) or drops_into_itself(self._sources, directory):
            return None
        return pane, directory

    def _highlight(self, pane: Pane | None) -> None:
        if pane is self._highlighted:
            return
        if self._highlighted is not None:
            self._highlighted.remove_class("-droptarget")
        if pane is not None:
            pane.add_class("-droptarget")
        self._highlighted = pane

    def _label(self, directory: Path | None) -> Text:
        mode = self._screen.cfg["icons"]
        if len(self._entries) == 1:
            e = self._entries[0]
            label = Text(f"{icon_for(e.kind, mode)} {e.name}")
        else:
            label = Text(f"{icon_for('file', mode)} {len(self._entries)} Einträge")
        if directory is not None:
            label.append(f"  →  {icon_for('dir', mode)} {directory.name or directory}")
        return label


# --------------------------------------------------------------------------- #
# Dialoge
# --------------------------------------------------------------------------- #


class ContextMenu(ModalScreen[str | None]):
    DEFAULT_CSS = """
    ContextMenu { background: $background 0%; align: left top; }
    ContextMenu > OptionList, ContextMenu > OptionList:focus {
        width: 36; height: auto; max-height: 24;
        border: round $primary; background: $surface; padding: 0;
    }
    """
    BINDINGS = [Binding("escape", "dismiss_menu", show=False)]

    def __init__(self, items: list[tuple[str, str, str]], x: int, y: int) -> None:
        super().__init__()
        self.items = items
        self.pos = (x, y)

    def compose(self) -> ComposeResult:
        opts: list = []
        for ident, label, key in self.items:
            if ident == "sep":
                opts.append(None)
                continue
            t = Text(no_wrap=True)
            t.append(label.ljust(25))
            t.append(key.rjust(7), style="dim")
            opts.append(Option(t, id=ident))
        yield OptionList(*opts)

    def on_mount(self) -> None:
        menu = self.query_one(OptionList)
        h = sum(1 for o in self.items) + 2
        x = max(0, min(self.pos[0], self.size.width - 37))
        y = max(0, min(self.pos[1], self.size.height - h))
        menu.styles.offset = (x, y)
        menu.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def on_mouse_down(self, event: events.MouseDown) -> None:
        try:
            widget, _ = self.get_widget_at(event.screen_x, event.screen_y)
        except Exception:  # noqa: BLE001
            widget = self
        if widget is self:
            self.dismiss(None)

    def action_dismiss_menu(self) -> None:
        self.dismiss(None)


class InputDialog(ModalScreen[str | None]):
    DEFAULT_CSS = """
    InputDialog { align: center middle; background: $background 50%; }
    InputDialog > Vertical {
        width: 70; max-width: 95%; height: auto; border: round $accent;
        background: $surface; padding: 1 2;
    }
    InputDialog Label.title { text-style: bold; margin-bottom: 1; }
    InputDialog Label.hint { color: $text-muted; margin-top: 1; }
    """
    BINDINGS = [Binding("escape", "cancel", show=False)]

    def __init__(self, title: str, value: str = "", hint: str = "Enter = OK · Esc = Abbrechen") -> None:
        super().__init__()
        self.title_text, self.value, self.hint = title, value, hint

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self.title_text, classes="title")
            yield Input(value=self.value)
            yield Label(self.hint, classes="hint")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmDialog(ModalScreen[bool]):
    DEFAULT_CSS = """
    ConfirmDialog { align: center middle; background: $background 50%; }
    ConfirmDialog > Vertical {
        width: 64; max-width: 95%; height: auto; border: round $error;
        background: $surface; padding: 1 2;
    }
    ConfirmDialog .msg { margin-bottom: 1; }
    ConfirmDialog Horizontal { height: auto; align: right middle; }
    ConfirmDialog Button { margin-left: 2; }
    """
    BINDINGS = [
        Binding("escape,n", "no", show=False),
        Binding("y,j", "yes", show=False),
    ]

    def __init__(self, message: str, yes_label: str = "Ja", danger: bool = True, default_yes: bool = True) -> None:
        super().__init__()
        self.message, self.yes_label, self.danger = message, yes_label, danger
        self.default_yes = default_yes

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.message, classes="msg", markup=False)
            with Horizontal():
                yield Button("Abbrechen", id="no")
                yield Button(self.yes_label, id="yes", variant="error" if self.danger else "primary")

    def on_mount(self) -> None:
        self.query_one("#yes" if self.default_yes else "#no", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class BookmarkScreen(ModalScreen[str | None]):
    DEFAULT_CSS = """
    BookmarkScreen { align: center middle; background: $background 50%; }
    BookmarkScreen > Vertical {
        width: 70; max-width: 95%; height: auto; max-height: 80%;
        border: round $accent; background: $surface; padding: 0 1;
    }
    BookmarkScreen Label { padding: 1 1 0 1; text-style: bold; }
    BookmarkScreen Label.hint { color: $text-muted; text-style: none; padding: 0 1 1 1; }
    BookmarkScreen OptionList, BookmarkScreen OptionList:focus {
        border: none; height: auto; max-height: 30; background: $surface;
    }
    """
    BINDINGS = [
        Binding("escape,b,q", "cancel", show=False),
        Binding("delete,d", "remove", show=False),
    ]

    def __init__(self, entries: list[tuple[str, str, bool]], on_remove: Callable[[str], None]) -> None:
        super().__init__()
        self.entries = entries  # (label, path, user_defined)
        self.on_remove = on_remove

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("★ Favoriten & Laufwerke")
            yield OptionList(*self._options())
            yield Label("Enter/Doppelklick = öffnen · Entf = eigenen Favoriten entfernen · Esc", classes="hint")

    def _options(self) -> list:
        opts = []
        for i, (label, path, user) in enumerate(self.entries):
            t = Text(no_wrap=True, overflow="ellipsis")
            t.append(label, style="bold" + (" #ffcc00" if user else ""))
            t.append("  " + path, style="dim")
            opts.append(Option(t, id=str(i)))
        return opts

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self.entries[int(event.option.id)][1])

    def action_remove(self) -> None:
        ol = self.query_one(OptionList)
        if ol.highlighted is None:
            return
        label, path, user = self.entries[ol.highlighted]
        if not user:
            self.app.notify("Nur eigene Favoriten können entfernt werden", timeout=2)
            return
        self.on_remove(path)
        del self.entries[ol.highlighted]
        ol.clear_options()
        ol.add_options(self._options())

    def action_cancel(self) -> None:
        self.dismiss(None)


HELP_MD = """\
# WSLUI – Hilfe

## Maus
| Aktion | Wirkung |
|---|---|
| **Klick** auf Datei/Ordner | auswählen **und Namen kopieren** |
| **Doppelklick** | Ordner öffnen · Textdatei im Editor rechts (nano) · sonst Windows-Programm |
| **Rechtsklick** | Kontextmenü (Öffnen, In Windows öffnen, Pfad, Explorer, Umbenennen …) |
| **Ziehen** auf ein anderes Fenster / einen Ordner | verschieben oder kopieren – Menü beim Loslassen |
| … mit `Strg` / `Shift` beim Loslassen | sofort kopieren / sofort verschieben |
| Klick auf **Pfadleiste** | direkt in diesen Ordner springen |
| Klick auf **Spaltenkopf** | sortieren (nochmal = umdrehen) |
| Klick auf **Layout-Buttons** oben | Fensteraufteilung wechseln |

## Navigation
| Taste | Wirkung |
|---|---|
| `↑ ↓` / `j k` | bewegen |
| `Enter` / `→` / `l` | öffnen (Textdateien im Editor rechts) |
| `←` / `h` / `Backspace` | eine Ebene hoch |
| `~` | Home · `\\` = Root · `g` = Pfad eingeben (auch `C:\\...`) |
| `b` | Favoriten & Laufwerke · `B` = aktuellen Ordner merken |
| `Tab` / `Shift+Tab` | nächstes / voriges Fenster |
| `/` | Filter · `Esc` = Filter/Markierung löschen |

## Fenster-Layouts
| Taste | Layout |
|---|---|
| `1` | ein Fenster |
| `2` | halb \\| halb |
| `3` | halb \\| viertel/viertel |
| `4` | 4 Viertel |
| `5` | halb oben / halb unten |
| `6` | drei Spalten |
| `7` | viertel/viertel \\| halb |
| `8` | halb oben / zwei Viertel unten |
| `P` | Vorschau-Spalte an/aus |

## Editor (rechts neben den Dateien)
| Taste | Wirkung |
|---|---|
| `Enter`, Doppelklick, Kontextmenü **Öffnen** | Textdatei in **nano** öffnen |
| `e` | im Editor öffnen – auch Dateien, die sonst an Windows gingen |
| `O` / Kontextmenü **In Windows öffnen** | mit dem Windows-Standardprogramm öffnen |
| `Ctrl+X` | Editor schließen (nano fragt bei Änderungen nach dem Speichern) |
| `F12` | reihum: Dateien → Editor → Terminal |
| Mausrad / **Maus ziehen** | scrollen / markieren und sofort kopieren |

Bilder, PDFs, Office-Dokumente, Archive und andere Binärdateien öffnet **Öffnen**
direkt mit dem Windows-Programm. Anderer Editor: `"editor"` in `~/.config/wslui/config.json`.

## Terminal (z. B. für git)
| Taste | Wirkung |
|---|---|
| `Ctrl+T` | Terminal unten ein-/ausblenden (läuft im Hintergrund weiter) |
| `t` / `F12` | ins Terminal springen / zwischen Dateien und Terminal wechseln |
| `Ctrl+↑` / `Ctrl+↓` | Terminal groß / normal |
| **Maus ziehen** | markieren – beim Loslassen sofort kopiert (Hinweis unten rechts) |
| `Ctrl+V`, `Shift+Einfg`, **Rechtsklick** | Einfügen – bereinigt, ohne `~[[200~`-Müll, ohne Auto-Enter |
| Mausrad | im Verlauf zurückscrollen |
| `T` | Vollbild-Shell (mit `exit` zurück) |

Das Terminal wechselt automatisch in den Ordner des aktiven Fensters – und umgekehrt
folgt das Fenster, wenn du im Terminal mit `cd` woanders hin gehst.
Markierter Text wird 1:1 kopiert – umbrochene lange Zeilen (z. B. URLs) bleiben am Stück.

## Kopieren in die Zwischenablage
| Taste | Kopiert |
|---|---|
| `c` | Dateiname(n) |
| `p` | vollständiger Linux-Pfad |
| `w` | Windows-Pfad (`\\\\wsl.localhost\\...` bzw. `C:\\...`) |
| `Leertaste` / `a` | markieren / alle markieren (mehrere Namen auf einmal kopieren) |

## Dateien
| Taste | Wirkung |
|---|---|
| `F2` | umbenennen |
| `F5` / `F6` | in das andere Fenster kopieren / verschieben |
| `F7` / `n` | neuer Ordner / neue Datei |
| `Entf` | löschen (mit Nachfrage) |
| `e` / `O` | im Editor rechts / in Windows öffnen · `C` = VS Code |
| `o` | im Windows-Explorer zeigen |
| `.` | versteckte Dateien an/aus · `s` = Sortierung · `i` = Icon-Stil |
| `R` | neu laden |

## Sonstiges
| Taste | Wirkung |
|---|---|
| `q` | beenden – die Shell landet im aktuellen Ordner |
| `A` | Autostart beim Öffnen von WSL an/aus |
| `Ctrl+P` | Befehlspalette (u. a. **Farbschema** wechseln) |
"""


class HelpScreen(ModalScreen[None]):
    DEFAULT_CSS = """
    HelpScreen { align: center middle; background: $background 60%; }
    HelpScreen > VerticalScroll {
        width: 90; max-width: 96%; height: 90%; border: round $accent;
        background: $surface; padding: 0 1;
    }
    """
    BINDINGS = [Binding("escape,q,question_mark,f1", "close", show=False)]

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Markdown(HELP_MD)

    def action_close(self) -> None:
        self.dismiss(None)


# --------------------------------------------------------------------------- #
# Hauptbildschirm
# --------------------------------------------------------------------------- #

LAYOUTS: dict[str, tuple[str, object]] = {
    "1": ("▣", "P"),
    "2": ("½|½", ("h", "P", "P")),
    "3": ("½|¼¼", ("h", "P", ("v", "P", "P"))),
    "4": ("¼×4", ("h", ("v", "P", "P"), ("v", "P", "P"))),
    "5": ("½/½", ("v", "P", "P")),
    "6": ("⅓×3", ("h", "P", "P", "P")),
    "7": ("¼¼|½", ("h", ("v", "P", "P"), "P")),
    "8": ("½/¼¼", ("v", "P", ("h", "P", "P"))),
}


def count_panes(spec: object) -> int:
    if spec == "P":
        return 1
    return sum(count_panes(s) for s in spec[1:])  # type: ignore[index]


class MainScreen(Screen):
    BINDINGS = [
        Binding("q", "quit_cd", "Beenden"),
        Binding("question_mark,f1", "help", "Hilfe"),
        Binding("c", "copy('name')", "Name"),
        Binding("p", "copy('path')", "Pfad"),
        Binding("w", "copy('win')", "Win-Pfad"),
        Binding("b", "bookmarks", "Favoriten"),
        Binding("slash", "filter", "Filter"),
        Binding("full_stop", "toggle_hidden", "Versteckte"),
        Binding("f5", "fileop('copy')", "Kopieren"),
        Binding("delete,f8", "delete", "Löschen"),
        Binding("ctrl+t", "toggle_terminal", "Terminal", priority=True),
        Binding("f12", "switch_focus", show=False, priority=True),
        Binding("ctrl+up", "terminal_size(True)", show=False, priority=True),
        Binding("ctrl+down", "terminal_size(False)", show=False, priority=True),
        Binding("t", "focus_terminal", show=False),
        Binding("T", "terminal", show=False),
        Binding("P", "toggle_preview", show=False),
        Binding("o", "reveal", show=False),
        Binding("O", "open_windows", show=False),
        Binding("f2", "rename", show=False),
        Binding("f6", "fileop('move')", show=False),
        Binding("f7", "mkdir", show=False),
        Binding("B", "add_bookmark", show=False),
        Binding("n", "newfile", show=False),
        Binding("e", "edit", show=False),
        Binding("C", "vscode", show=False),
        Binding("s", "sort", show=False),
        Binding("i", "icons", show=False),
        Binding("A", "toggle_autostart", show=False),
        Binding("R", "reload", show=False),
        Binding("tilde", "goto_home", show=False),
        Binding("backslash", "goto_root", show=False),
        Binding("g,colon", "goto_prompt", show=False),
        *[Binding(k, f"layout('{k}')", show=False) for k in LAYOUTS],
    ]

    def __init__(self, start_path: Path | None) -> None:
        super().__init__()
        cfg = self.app.cfg  # type: ignore[attr-defined]
        self.states = [PaneState(**s) for s in cfg.get("panes", []) if isinstance(s, dict) and "path" in s][:4]
        for s in self.states:
            if not Path(s.path).is_dir():
                s.path = str(HOME)
            if s.sort not in SORTS:
                s.sort = "name"
        if start_path is not None:
            if self.states:
                self.states[0].path = str(start_path)
            else:
                self.states.append(PaneState(str(start_path)))
        if not self.states:
            self.states.append(PaneState(os.getcwd() if Path(os.getcwd()).is_dir() else str(HOME)))
        self.layout_key = cfg.get("layout", "2") if cfg.get("layout") in LAYOUTS else "2"
        self.active: Pane | None = None
        self._pv_timer = None
        self.drag = DragController(self)

    @property
    def cfg(self) -> dict:
        return self.app.cfg  # type: ignore[attr-defined]

    def compose(self) -> ComposeResult:
        yield ClickBar(id="topbar")
        with Horizontal(id="main"):
            yield Vertical(id="root")
            yield Preview(id="preview")
            yield EditorPanel(id="editor")
        yield TerminalPanel(id="termpanel")
        yield Footer()
        yield DragGhost()

    async def on_mount(self) -> None:
        if self.cfg.get("preview"):
            self.query_one("#preview").add_class("-on")
        await self.set_layout(self.layout_key, initial=True)
        self.set_interval(2.0, self._poll)

    def on_resize(self) -> None:
        self.update_topbar()
        for p in self.panes:
            p.update_pathbar()

    def _poll(self) -> None:
        for p in self.panes:
            p.check_changed()

    @property
    def panes(self) -> list[Pane]:
        return list(self.query(Pane))

    # ---------- Layout ----------
    async def set_layout(self, key: str, initial: bool = False) -> None:
        spec = LAYOUTS[key][1]
        n = count_panes(spec)
        panes = self.panes
        active_idx = panes.index(self.active) if self.active in panes else 0
        if active_idx >= n:
            self.states[0], self.states[active_idx] = self.states[active_idx], self.states[0]
            active_idx = 0
        while len(self.states) < n:
            src = self.states[min(active_idx, len(self.states) - 1)]
            self.states.append(PaneState(src.path, src.sort, src.rev))
        counter = iter(range(n))

        def build(s: object):
            if s == "P":
                return Pane(self.states[next(counter)])
            kind, *kids = s  # type: ignore[misc]
            cls = Horizontal if kind == "h" else Vertical
            return cls(*[build(k) for k in kids], classes="split")

        root = self.query_one("#root")
        await root.remove_children()
        self.active = None
        await root.mount(build(spec))
        self.layout_key = key
        self.cfg["layout"] = key
        panes = self.panes
        self.set_active(panes[min(active_idx, len(panes) - 1)])
        self.active.table.focus()  # type: ignore[union-attr]
        self.update_topbar()
        if not initial:
            self.app.notify(f"Layout {key}: {LAYOUTS[key][0]}", timeout=1.2)

    def action_layout(self, key: str) -> None:
        self.run_worker(self.set_layout(key), exclusive=True, group="layout")

    def set_active(self, pane: Pane) -> None:
        if self.active is pane:
            return
        for p in self.panes:
            p.set_class(p is pane, "-active")
        self.active = pane
        self.highlight_changed(pane)

    def pane_changed(self, pane: Pane) -> None:
        if pane is self.active:
            self.highlight_changed(pane)

    def highlight_changed(self, pane: Pane) -> None:
        if pane is not self.active or not self.query_one("#preview").has_class("-on"):
            return
        if self._pv_timer is not None:
            self._pv_timer.stop()
        self._pv_timer = self.set_timer(0.12, lambda: self._update_preview(pane))

    def _update_preview(self, pane: Pane) -> None:
        if pane.is_attached:
            self.query_one(Preview).show(pane.current, self.cfg["icons"])

    def _term_on(self) -> bool:
        try:
            return self.term_panel.has_class("-on")
        except Exception:  # noqa: BLE001
            return False

    def _click_terminal(self) -> None:
        if self._term_on():
            self.term_panel.remove_class("-on")
            self.update_topbar()
            if self.active:
                self.active.table.focus()
        else:
            self._open_terminal()

    def update_topbar(self) -> None:
        bar = self.query_one("#topbar", ClickBar)
        items: list[tuple[Text, Callable[[], None] | None]] = [
            (Text(" ◆ WSLUI ", style="bold reverse"), lambda: self.action_help()),
            (Text("  "), None),
        ]
        for k, (label, _) in LAYOUTS.items():
            active = k == self.layout_key
            items.append(
                (
                    Text(f" {k} {label} ", style="bold reverse" if active else "dim"),
                    (lambda k=k: self.action_layout(k)),
                )
            )
            items.append((Text(" "), None))
        right: list[tuple[Text, Callable[[], None]]] = [
            (Text(" ★ Favoriten ", style="bold"), self.action_bookmarks),
            (
                Text(" ◐ Versteckte ", style="bold reverse" if self.cfg["show_hidden"] else "bold"),
                self.action_toggle_hidden,
            ),
            (
                Text(" ◨ Vorschau ", style="bold reverse" if self.cfg.get("preview") else "bold"),
                self.action_toggle_preview,
            ),
            (
                Text(" >_ Terminal ", style="bold reverse" if self._term_on() else "bold"),
                self._click_terminal,
            ),
            (Text(" ? Hilfe ", style="bold"), self.action_help),
        ]
        left_w = sum(t.cell_len for t, _ in items)
        right_w = sum(t.cell_len for t, _ in right) + len(right) - 1
        gap = (bar.size.width or 120) - left_w - right_w
        if gap > 0:
            items.append((Text(" " * gap), None))
            for i, it in enumerate(right):
                if i:
                    items.append((Text(" "), None))
                items.append(it)
        bar.set_items(items)

    # ---------- Kopieren ----------
    def action_copy(self, what: str) -> None:
        pane = self.active
        if pane is None:
            return
        sel = pane.selection()
        if not sel:
            return
        if what == "name":
            text, title = shell_join([e.name for e in sel]), "Name kopiert"
        elif what == "path":
            text, title = shell_join([str(e.path) for e in sel]), "Pfad kopiert"
        else:
            wins = [to_windows_path(e.path) for e in sel]
            text = wins[0] if len(wins) == 1 else " ".join(f'"{w}"' for w in wins)
            title = "Windows-Pfad kopiert"
        self.app.sys_clipboard.copy(text)  # type: ignore[attr-defined]
        shown = text if len(text) < 80 else text[:77] + "…"
        self.app.notify(f"📋 {shown}", title=title + (f" ({len(sel)})" if len(sel) > 1 else ""), timeout=2.5)

    # ---------- Menü-Aktionen ----------
    def run_menu_action(self, ident: str) -> None:
        mapping: dict[str, Callable[[], None]] = {
            "copy_name": lambda: self.action_copy("name"),
            "copy_path": lambda: self.action_copy("path"),
            "copy_win": lambda: self.action_copy("win"),
            "open": lambda: self.active and self.active.open_entry(),
            "reveal": self.action_reveal,
            "open_windows": self.action_open_windows,
            "vscode": self.action_vscode,
            "terminal": self.action_focus_terminal,
            "mark": lambda: self.active and self.active.toggle_mark(),
            "rename": self.action_rename,
            "copy_to": lambda: self.action_fileop("copy"),
            "move_to": lambda: self.action_fileop("move"),
            "delete": self.action_delete,
            "mkdir": self.action_mkdir,
            "newfile": self.action_newfile,
        }
        fn = mapping.get(ident)
        if fn:
            fn()

    # ---------- Ansicht ----------
    def action_help(self) -> None:
        self.app.push_screen(HelpScreen())

    def action_filter(self) -> None:
        if self.active:
            self.active.action_filter()

    def action_toggle_hidden(self) -> None:
        self.cfg["show_hidden"] = not self.cfg["show_hidden"]
        for p in self.panes:
            p.render_rows()
        self.update_topbar()
        self.app.notify("Versteckte Dateien " + ("sichtbar" if self.cfg["show_hidden"] else "ausgeblendet"), timeout=1.5)

    def action_toggle_preview(self) -> None:
        pv = self.query_one("#preview")
        on = not pv.has_class("-on")
        pv.set_class(on, "-on")
        self.cfg["preview"] = on
        self.update_topbar()
        if on and self.active:
            self._update_preview(self.active)

    def action_sort(self) -> None:
        if self.active:
            self.active.cycle_sort()

    def action_icons(self) -> None:
        modes = ["emoji", "nerd", "plain"]
        self.cfg["icons"] = modes[(modes.index(self.cfg["icons"]) + 1) % 3] if self.cfg["icons"] in modes else "emoji"
        for p in self.panes:
            p.render_rows()
            p.update_pathbar()
        names = {"emoji": "Emoji", "nerd": "Nerd Font", "plain": "Einfach"}
        self.app.notify(f"Icons: {names[self.cfg['icons']]}", timeout=1.5)

    def action_reload(self) -> None:
        for p in self.panes:
            p.reload()

    def action_toggle_autostart(self) -> None:
        on = not AUTOSTART_FLAG.exists()
        set_autostart(on)
        self.app.notify(
            "WSLUI startet jetzt automatisch beim Öffnen von WSL" if on else "Autostart deaktiviert",
            title="Autostart",
        )

    # ---------- Navigation ----------
    def action_goto_home(self) -> None:
        if self.active:
            self.active.goto(HOME)

    def action_goto_root(self) -> None:
        if self.active:
            self.active.goto(Path("/"))

    def action_goto_prompt(self) -> None:
        pane = self.active
        if pane is None:
            return

        def done(value: str | None) -> None:
            if value:
                target = Path(os.path.expanduser(from_windows_path(value)))
                if not target.is_absolute():
                    target = pane.path / target
                if target.is_file():
                    pane.goto(target.parent, keep=target.name)
                else:
                    pane.goto(target)

        self.app.push_screen(
            InputDialog("Gehe zu Pfad", str(pane.path), "Linux- oder Windows-Pfad (z. B. C:\\Users) · Enter = los"),
            done,
        )

    def bookmark_entries(self) -> list[tuple[str, str, bool]]:
        e: list[tuple[str, str, bool]] = [("🏠 Home", str(HOME), False), ("💻 Root /", "/", False)]
        if _win_profile:
            e.append(("🪟 Windows-Benutzer", str(_win_profile), False))
            for sub, label in [
                ("Desktop", "📌 Desktop"),
                ("Downloads", "📥 Downloads"),
                ("Documents", "📝 Dokumente"),
                ("OneDrive", "📂 OneDrive"),
            ]:
                if (_win_profile / sub).is_dir():
                    e.append((label, str(_win_profile / sub), False))
        if os.path.isdir("/mnt"):
            try:
                for d in sorted(os.listdir("/mnt")):
                    if len(d) == 1 and os.path.isdir(f"/mnt/{d}") and os.listdir(f"/mnt/{d}"):
                        e.append((f"💽 Laufwerk {d.upper()}:", f"/mnt/{d}", False))
            except OSError:
                pass
        for b in self.cfg.get("bookmarks", []):
            e.append((f"★ {Path(b).name or b}", b, True))
        return e

    def action_bookmarks(self) -> None:
        pane = self.active
        if pane is None:
            return

        def remove(path: str) -> None:
            self.cfg["bookmarks"] = [b for b in self.cfg.get("bookmarks", []) if b != path]

        def done(path: str | None) -> None:
            if path:
                pane.goto(Path(path))

        self.app.push_screen(BookmarkScreen(self.bookmark_entries(), remove), done)

    def action_add_bookmark(self) -> None:
        if not self.active:
            return
        p = str(self.active.path)
        bms = self.cfg.setdefault("bookmarks", [])
        if p in bms:
            self.app.notify("Schon in den Favoriten", timeout=1.5)
        else:
            bms.append(p)
            self.app.notify(f"★ {p}", title="Favorit hinzugefügt", timeout=2)

    # ---------- Externe Programme ----------
    def action_reveal(self) -> None:
        pane = self.active
        if pane is None:
            return
        e = pane.current
        target = pane.path if e is None or e.is_parent else e.path
        if not reveal_in_explorer(target):
            self.app.notify("Explorer nicht verfügbar", severity="error")

    def action_vscode(self) -> None:
        pane = self.active
        if pane is None:
            return
        if not shutil.which("code"):
            self.app.notify("VS Code ('code') nicht gefunden", severity="error")
            return
        e = pane.current
        target = pane.path if e is None or e.is_parent else e.path
        spawn(["code", str(target)], cwd=str(pane.path))
        self.app.notify(f"VS Code: {target.name or target}", timeout=2)

    def action_open_windows(self) -> None:
        if self.active:
            self.active.open_in_windows()

    def action_edit(self) -> None:
        """e: im eingebetteten Editor öffnen – auch Dateien, die „Öffnen“ an Windows gäbe."""
        e = self.active.current if self.active else None
        if e is not None and not e.is_dir:
            self.open_in_editor(e.path)

    # ---------- Eingebetteter Editor (rechts) ----------
    @property
    def editor(self) -> EditorPanel:
        return self.query_one(EditorPanel)

    @property
    def editor_busy(self) -> bool:
        """True, solange im Editor eine Datei offen ist (ungespeicherte Änderungen möglich)."""
        return self.editor.is_open and self.editor.view.running

    def open_in_editor(self, path: Path) -> None:
        editor = self.editor
        if editor.is_open:
            if editor.path != path:
                self.app.notify(
                    f"Im Editor ist noch „{editor.path.name if editor.path else '?'}“ offen – "
                    "bitte zuerst speichern und schließen (Ctrl+X).",
                    title="Editor belegt",
                    severity="warning",
                    markup=False,
                )
            editor.view.focus()
            return
        configured = self.cfg.get("editor") or "nano"
        argv = editor_command(configured, path)
        if argv is None:
            self.app.notify(
                f"Editor „{configured}“ nicht gefunden – z. B. mit 'sudo apt install nano' installieren.",
                title="Kein Editor",
                severity="error",
                markup=False,
            )
            return
        self.query_one("#main").add_class("-editing")
        editor.open(path, argv)

    def _editor_closed(self) -> None:
        path = self.editor.path
        self.editor.close()
        self.query_one("#main").remove_class("-editing")
        if self.active:
            self.active.table.focus()
        for pane in self.panes:
            if path is not None and pane.path == path.parent:
                pane.reload()

    # ---------- Eingebettetes Terminal ----------
    @property
    def term(self) -> TerminalView:
        return self.query_one("#term-view", TerminalView)

    @property
    def term_panel(self) -> TerminalPanel:
        return self.query_one(TerminalPanel)

    def _open_terminal(self) -> None:
        panel, tv = self.term_panel, self.term
        if not panel.has_class("-on"):
            panel.add_class("-on")
            self.update_topbar()
        if not tv.running:
            if self.active:
                tv.start_dir = str(self.active.path)
                self._term_synced_pane = str(self.active.path)
                self._term_synced_cwd = str(self.active.path)
            # Start erst nach dem Layout, damit die Größe stimmt
            self.call_after_refresh(tv.start)
        tv.focus()

    def action_toggle_terminal(self) -> None:
        panel, tv = self.term_panel, self.term
        if not panel.has_class("-on"):
            self._open_terminal()
        elif tv.has_focus:
            panel.remove_class("-on")
            self.update_topbar()
            if self.active:
                self.active.table.focus()
        else:
            tv.focus()

    def action_focus_terminal(self) -> None:
        self._open_terminal()

    def action_switch_focus(self) -> None:
        """F12: Dateien → Editor (falls offen) → Terminal → Dateien."""
        stops = ["files", *(["editor"] if self.editor.is_open else []), "terminal"]
        if self.term.has_focus:
            current = "terminal"
        elif self.editor.view.has_focus:
            current = "editor"
        else:
            current = "files"
        target = stops[(stops.index(current) + 1) % len(stops)]
        if target == "editor":
            self.editor.view.focus()
        elif target == "terminal":
            self._open_terminal()
        elif self.active:
            self.active.table.focus()

    def action_terminal_size(self, bigger: bool) -> None:
        panel = self.term_panel
        if panel.has_class("-on"):
            panel.set_class(bigger, "-big")

    _term_synced_pane: str | None = None
    _term_synced_cwd: str | None = None

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        if event.widget is self.term:
            self._sync_pane_to_terminal()

    def on_descendant_blur(self, event: events.DescendantBlur) -> None:
        if event.widget is self.term:
            self._sync_terminal_to_pane()

    def _sync_pane_to_terminal(self) -> None:
        """Terminal folgt dem Ordner des aktiven Fensters (nur wenn die Shell frei ist)."""
        tv, pane = self.term, self.active
        if pane is None or not tv.is_idle():
            return
        path = str(pane.path)
        if path != self._term_synced_pane and path != tv.shell_cwd():
            tv.cd(path)
            self._term_synced_cwd = path
        self._term_synced_pane = path

    def _sync_terminal_to_pane(self) -> None:
        """Hat man im Terminal per cd gewechselt, folgt das aktive Fenster."""
        tv, pane = self.term, self.active
        cwd = tv.shell_cwd()
        if pane is None or not cwd:
            return
        if cwd != self._term_synced_cwd and cwd != str(pane.path) and os.path.isdir(cwd):
            pane.goto(Path(cwd))
            self._term_synced_pane = cwd
        self._term_synced_cwd = cwd

    def on_terminal_view_exited(self, event: TerminalView.Exited) -> None:
        if event.view is self.editor.view:
            self._editor_closed()
            return
        self.term_panel.remove_class("-on")
        self.update_topbar()
        if self.active:
            self.active.table.focus()
        self.app.notify("Shell beendet – Ctrl+T startet eine neue", timeout=2)

    def action_terminal(self) -> None:
        pane = self.active
        if pane is None:
            return
        sh = os.environ.get("SHELL") or "/bin/bash"
        env = dict(os.environ, WSLUI_ACTIVE="1")
        with self.app.suspend():
            print(f"\n\033[1;36m▶ Shell in {pane.path}\033[0m  –  mit \033[1mexit\033[0m zurück zu WSLUI\n")
            subprocess.call([sh], cwd=str(pane.path), env=env)
        pane.reload()

    # ---------- Dateioperationen ----------
    def other_pane(self) -> Pane | None:
        panes = self.panes
        if self.active is None or len(panes) < 2:
            return None
        return panes[(panes.index(self.active) + 1) % len(panes)]

    def action_fileop(self, op: str) -> None:
        pane = self.active
        if pane is None:
            return
        sel = pane.selection()
        if not sel:
            return
        verb = "Kopieren" if op == "copy" else "Verschieben"
        what = sel[0].name if len(sel) == 1 else f"{len(sel)} Einträge"

        def start(target: str | None) -> None:
            if not target:
                return
            tdir = Path(os.path.expanduser(from_windows_path(target)))
            if not tdir.is_dir():
                self.app.notify(f"Zielordner existiert nicht: {tdir}", severity="error")
                return
            self._run_fileop(op, [e.path for e in sel], tdir)

        other = self.other_pane()
        default = str(other.path) if other else str(pane.path)
        self.app.push_screen(InputDialog(f"{verb}: {what}  →  Zielordner", default), start)

    def transfer(self, op: DropAction, sources: list[Path], target: Path) -> None:
        """Kopiert/verschiebt `sources` nach `target` (im Hintergrund, mit Abschlussmeldung)."""
        self._run_fileop(op, sources, target)

    @work(thread=True, group="fileop")
    def _run_fileop(self, op: str, sources: list[Path], target: Path) -> None:
        done = skipped = 0
        errors: list[str] = []
        for src in sources:
            dst = target / src.name
            try:
                if dst.exists() or dst.is_symlink():
                    skipped += 1
                    continue
                if src.is_dir() and not src.is_symlink():
                    if target.resolve() == src.resolve() or src.resolve() in target.resolve().parents:
                        errors.append(f"{src.name}: Ziel liegt im Quellordner")
                        continue
                if op == "copy":
                    if src.is_dir() and not src.is_symlink():
                        shutil.copytree(src, dst, symlinks=True)
                    else:
                        shutil.copy2(src, dst, follow_symlinks=False)
                else:
                    shutil.move(str(src), str(dst))
                done += 1
            except OSError as exc:
                errors.append(f"{src.name}: {exc.strerror or exc}")
        self.app.call_from_thread(self._fileop_done, op, done, skipped, errors)

    def _fileop_done(self, op: str, done: int, skipped: int, errors: list[str]) -> None:
        verb = "kopiert" if op == "copy" else "verschoben"
        msg = f"{done} {verb}"
        if skipped:
            msg += f", {skipped} übersprungen (existiert bereits)"
        self.app.notify(msg, title="Fertig", severity="warning" if skipped else "information")
        for err in errors[:5]:
            self.app.notify(err, severity="error")
        for p in self.panes:
            p.marked.clear()
            p.reload()

    def action_delete(self) -> None:
        pane = self.active
        if pane is None:
            return
        sel = pane.selection()
        if not sel:
            return
        if len(sel) == 1:
            msg = f"„{sel[0].name}“{' (Ordner mit Inhalt)' if sel[0].is_dir and not sel[0].is_link else ''} endgültig löschen?"
        else:
            names = ", ".join(e.name for e in sel[:5]) + (" …" if len(sel) > 5 else "")
            msg = f"{len(sel)} Einträge endgültig löschen?\n{names}"

        def done(ok: bool | None) -> None:
            if ok:
                self._run_delete([e.path for e in sel], pane)

        self.app.push_screen(ConfirmDialog(msg, "Löschen"), done)

    @work(thread=True, group="fileop")
    def _run_delete(self, paths: list[Path], pane: Pane) -> None:
        errors: list[str] = []
        n = 0
        for p in paths:
            try:
                if p.is_dir() and not p.is_symlink():
                    shutil.rmtree(p)
                else:
                    p.unlink()
                n += 1
            except OSError as exc:
                errors.append(f"{p.name}: {exc.strerror or exc}")

        def finish() -> None:
            self.app.notify(f"{n} gelöscht", title="Löschen", timeout=2)
            for err in errors[:5]:
                self.app.notify(err, severity="error")
            for pn in self.panes:
                pn.marked.clear()
                pn.reload()

        self.app.call_from_thread(finish)

    def action_rename(self) -> None:
        pane = self.active
        e = pane.current if pane else None
        if pane is None or e is None or e.is_parent:
            return

        def done(new: str | None) -> None:
            if not new or new == e.name:
                return
            if "/" in new:
                self.app.notify("Name darf kein '/' enthalten", severity="error")
                return
            dst = e.path.with_name(new)
            if dst.exists():
                self.app.notify(f"„{new}“ existiert bereits", severity="error")
                return
            try:
                e.path.rename(dst)
            except OSError as exc:
                self.app.notify(str(exc.strerror or exc), severity="error")
                return
            pane.marked.discard(e.name)
            pane.reload(keep=new)

        self.app.push_screen(InputDialog("Umbenennen", e.name), done)

    def _create(self, kind: str) -> None:
        pane = self.active
        if pane is None:
            return

        def done(name: str | None) -> None:
            if not name:
                return
            p = pane.path / name
            try:
                if kind == "dir":
                    p.mkdir(parents=True)
                else:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.touch(exist_ok=False)
            except FileExistsError:
                self.app.notify(f"„{name}“ existiert bereits", severity="error")
                return
            except OSError as exc:
                self.app.notify(str(exc.strerror or exc), severity="error")
                return
            pane.reload(keep=Path(name).parts[0])

        self.app.push_screen(InputDialog("Neuer Ordner" if kind == "dir" else "Neue Datei", ""), done)

    def action_mkdir(self) -> None:
        self._create("dir")

    def action_newfile(self) -> None:
        self._create("file")

    # ---------- Beenden ----------
    def confirm_quit(self, quit_now: Callable[[], None]) -> None:
        """Beendet – mit Rückfrage, solange im Editor eine Datei offen ist."""
        if not self.editor_busy:
            quit_now()
            return
        name = self.editor.path.name if self.editor.path else "eine Datei"

        def done(ok: bool | None) -> None:
            if ok:
                quit_now()

        message = f"Im Editor ist noch „{name}“ geöffnet.\nUngespeicherte Änderungen gehen beim Beenden verloren."
        self.app.push_screen(ConfirmDialog(message, "Trotzdem beenden", default_yes=False), done)

    def action_quit_cd(self) -> None:
        def quit_now() -> None:
            if self.active:
                self.app.final_dir = str(self.active.path)  # type: ignore[attr-defined]
            self.app.exit()

        self.confirm_quit(quit_now)


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #


class WSLUI(App):
    TITLE = "WSLUI"
    CSS = """
    MainScreen { background: $background; }
    #topbar { height: 1; background: $panel; color: $foreground; }
    #main { height: 1fr; }
    #root { width: 1fr; height: 1fr; }
    #root .split { width: 1fr; height: 1fr; }
    #preview {
        display: none; width: 42%; height: 1fr;
        border: round $panel-lighten-2; border-title-color: $accent;
        padding: 0 1; scrollbar-size-vertical: 1;
    }
    #preview.-on { display: block; }
    #main.-editing #preview { display: none; }  /* der Editor nimmt den Platz der Vorschau ein */
    .pane {
        width: 1fr; height: 1fr;
        border: round $panel-lighten-2;
        border-title-color: $text-muted; border-subtitle-color: $text-muted;
        border-title-align: left; border-subtitle-align: right;
    }
    .pane.-active {
        border: round $primary;
        border-title-color: $primary; border-subtitle-color: $foreground;
    }
    .pane.-droptarget {
        border: round $success;
        border-title-color: $success; border-subtitle-color: $foreground;
    }
    .pathbar { height: 1; padding: 0 1; background: $boost; }
    FileTable { height: 1fr; background: transparent; scrollbar-size-vertical: 1; }
    FileTable > .datatable--header { background: transparent; color: $text-muted; }
    FileTable > .datatable--cursor { background: $panel-lighten-1; }
    FileTable:focus > .datatable--cursor { background: $primary 30%; }
    FileTable > .datatable--hover { background: $boost; }
    .pane .filter { display: none; height: 1; border: none; padding: 0 1; background: $boost; }
    .pane .filter.-on { display: block; }
    Footer { background: $panel; }
    """
    BINDINGS = [Binding("ctrl+q", "quit", "Beenden", show=False, priority=True)]

    def __init__(self, start_path: Path | None, clipboard: Clipboard | None = None) -> None:
        super().__init__()
        self.cfg = load_config()
        self.start_path = start_path
        # Nicht `clipboard` nennen – das ist in Textual bereits die app-interne Zwischenablage.
        self.sys_clipboard: Clipboard = clipboard or SystemClipboard(self)
        self.final_dir: str | None = None
        self.main_screen: MainScreen | None = None

    def on_mount(self) -> None:
        try:
            self.theme = self.cfg.get("theme", "tokyo-night")
        except Exception:  # noqa: BLE001
            self.theme = "tokyo-night"
        threading.Thread(target=detect_windows_profile, daemon=True).start()
        self.main_screen = MainScreen(self.start_path)
        self.push_screen(self.main_screen)

    def get_system_commands(self, screen: Screen) -> Iterable[SystemCommand]:
        yield from super().get_system_commands(screen)
        if isinstance(screen, MainScreen):
            yield SystemCommand("Autostart umschalten", "WSLUI beim Öffnen von WSL automatisch starten", screen.action_toggle_autostart)
            yield SystemCommand("Icons umschalten", "Emoji / Nerd Font / Einfach", screen.action_icons)
            yield SystemCommand("Klick kopiert Namen an/aus", "Einfacher Klick kopiert den Dateinamen", self._toggle_click_copy)
            yield SystemCommand("Versteckte Dateien an/aus", "Dotfiles anzeigen", screen.action_toggle_hidden)
            yield SystemCommand("Vorschau an/aus", "Vorschau-Spalte rechts", screen.action_toggle_preview)
            for k, (label, _) in LAYOUTS.items():
                yield SystemCommand(f"Layout {k}: {label}", "Fensteraufteilung", lambda k=k: screen.action_layout(k))

    async def action_quit(self) -> None:
        if self.main_screen is not None and self.main_screen.is_attached:
            self.main_screen.confirm_quit(self.exit)
        else:
            self.exit()

    def _toggle_click_copy(self) -> None:
        self.cfg["click_copy"] = not self.cfg["click_copy"]
        self.notify("Klick kopiert Namen: " + ("an" if self.cfg["click_copy"] else "aus"))

    def persist(self) -> None:
        self.cfg["theme"] = self.theme
        if self.main_screen is not None:
            self.cfg["panes"] = [asdict(s) for s in self.main_screen.states[:4]]
        save_config(self.cfg)


def main() -> None:
    ap = argparse.ArgumentParser(prog="wui", description="WSLUI – Dateimanager für WSL")
    ap.add_argument("path", nargs="?", help="Startordner")
    ap.add_argument("--cd-file", help="schreibt beim Beenden (q) den letzten Ordner in diese Datei")
    ap.add_argument("--autostart", choices=["on", "off", "status"], help="Autostart beim Öffnen von WSL")
    args = ap.parse_args()

    if args.autostart:
        if args.autostart == "status":
            print("Autostart ist", "AN" if AUTOSTART_FLAG.exists() else "AUS")
        else:
            set_autostart(args.autostart == "on")
            print("Autostart", "aktiviert" if args.autostart == "on" else "deaktiviert")
        return

    start = None
    if args.path:
        p = Path(os.path.expanduser(from_windows_path(args.path))).resolve()
        if p.is_file():
            p = p.parent
        if not p.is_dir():
            print(f"Kein Ordner: {p}", file=sys.stderr)
            sys.exit(1)
        start = p

    app = WSLUI(start)
    app.run()
    app.persist()
    if args.cd_file and app.final_dir:
        try:
            Path(args.cd_file).write_text(app.final_dir, encoding="utf-8")
        except OSError:
            pass


if __name__ == "__main__":
    main()
