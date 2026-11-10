# WSLUI

Ein moderner, mausfreundlicher Dateimanager mit eingebautem Terminal für die WSL.

- **Klick** auf eine Datei kopiert ihren Namen in die Windows-Zwischenablage
- **Doppelklick** öffnet Ordner (bzw. Dateien mit dem Windows-Standardprogramm)
- **Rechtsklick** öffnet ein Kontextmenü (Pfad / Windows-Pfad kopieren, Explorer, VS Code, Umbenennen …)
- **8 Fenster-Layouts**: ½|½, ½|¼¼, 4×¼, ½/½, ⅓×3 … (Tasten `1`–`8` oder oben anklicken)
- **Terminal unten** (`Ctrl+T`) für git & Co. – folgt automatisch dem aktuellen Ordner
- **Sauberes Einfügen** mit `Ctrl+V` / Rechtsklick – ohne `~[[200~`-Sonderzeichen
- **Markieren = Kopieren** im Terminal: Maus ziehen, loslassen – fertig
- Farbige Dateitypen + Icons, Vorschau mit Syntax-Highlighting, Favoriten & Laufwerke
- **Autostart** beim Öffnen von WSL (abschaltbar)

## Installation

Voraussetzungen: WSL mit Ubuntu/Debian, `git` und Python 3 mit `venv`.
Falls nötig, vorher einmal installieren:

```bash
sudo apt update && sudo apt install -y git python3-venv
```

Dann in der WSL (nicht in PowerShell):

```bash
git clone https://github.com/lcsl90/WUI.git ~/wslui-src
bash ~/wslui-src/install.sh --autostart
```

Der Installer legt eine eigene Python-Umgebung unter `~/.local/share/wslui` an und
installiert dort `textual` und `pyte` – am System-Python wird nichts verändert.

Optionen: `--autostart`, `--no-autostart`, `--uninstall`.
Ohne Option fragt der Installer beim ersten Mal, ob WSLUI beim Öffnen von WSL automatisch
starten soll; bei späteren Updates bleibt die bisherige Einstellung erhalten.
Danach ein neues WSL-Fenster öffnen (oder `source ~/.bashrc`) und `wui` eingeben.

### Aktualisieren

```bash
git -C ~/wslui-src pull && bash ~/wslui-src/install.sh
```

### Deinstallieren

```bash
bash ~/wslui-src/install.sh --uninstall
```

Die `.bashrc` wird vorher einmalig nach `~/.bashrc.wslui-backup` gesichert.

## Autostart

- In WSLUI: Taste `A`
- In der Shell: `wslui --autostart on` / `off` / `status`
- Einmalig ohne Autostart: `WSLUI_NO_AUTOSTART=1 wsl`

Der Autostart greift nicht in VS Code, tmux, verschachtelten Shells oder im WSLUI-Terminal selbst.
Beenden mit `q` – die Shell steht danach im zuletzt geöffneten Ordner.

## Wichtigste Tasten

| Taste | Wirkung |
|---|---|
| `c` / `p` / `w` | Name / Linux-Pfad / Windows-Pfad kopieren |
| `Leertaste`, `a` | markieren / alle markieren |
| `Enter`, `←`/`Backspace` | öffnen / eine Ebene hoch |
| `1`–`8` | Layout wechseln |
| `Ctrl+T`, `F12` | Terminal ein/aus, zwischen Dateien und Terminal wechseln |
| `/` | Filter |
| `b` / `B` | Favoriten / aktuellen Ordner merken |
| `g` | Pfad eingeben (auch `C:\Users\...`) |
| `F2` `F5` `F6` `F7` `Entf` | umbenennen, kopieren, verschieben, neuer Ordner, löschen |
| `P` | Vorschau |
| `.` | versteckte Dateien |
| `Ctrl+P` | Befehlspalette (u. a. Farbschema) |
| `?` | komplette Hilfe |

## Tipps

- Icons sehen mit einer [Nerd Font](https://www.nerdfonts.com/) noch besser aus – mit `i` umschalten (Emoji / Nerd Font / Einfach).
- Text im Terminal mit der Maus markieren – beim Loslassen ist er sofort 1:1 in der Zwischenablage
  (Hinweis unten rechts). Umbrochene lange Zeilen wie URLs bleiben dabei am Stück.
- Einstellungen liegen in `~/.config/wslui/config.json`.
