# WSLUI

Ein moderner, mausfreundlicher Dateimanager mit eingebautem Terminal für die WSL.

- **Klick** auf eine Datei kopiert ihren Namen in die Windows-Zwischenablage
- **Doppelklick** öffnet Ordner – und Textdateien in **nano**, eingebettet rechts neben den Dateien
  (Bilder, PDFs, Office-Dokumente … weiterhin mit dem Windows-Standardprogramm)
- **Rechtsklick** öffnet ein Kontextmenü (Öffnen, In Windows öffnen, Pfad / Windows-Pfad kopieren,
  Explorer, VS Code, Umbenennen …)
- **Drag & Drop** zwischen Fenstern: Datei(en) auf ein anderes Fenster oder einen Ordner ziehen,
  loslassen, *Hierher verschieben* / *Hierher kopieren* wählen. Mit `Strg` bzw. `Shift` beim Loslassen
  geht es ohne Nachfrage – sofern das Terminal die Taste weitergibt (Windows Terminal nutzt
  Shift+Maus selbst für seine Textauswahl)
- **8 Fenster-Layouts**: ½|½, ½|¼¼, 4×¼, ½/½, ⅓×3 … (Tasten `1`–`8` oder oben anklicken)
- **Terminal unten** (`Ctrl+T`) für git & Co. – folgt automatisch dem aktuellen Ordner;
  die Höhe lässt sich mit der Maus am oberen Rand ziehen (wird gemerkt)
- **Docker / Podman live** (`d` / `D` oder oben anklicken): wie `docker ps -a`, alle 2 s aktualisiert –
  Name, Status samt Laufzeit, Image, Ports; abgestürzte oder „unhealthy“ Container stehen rot oben
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
| `Enter`, `←`/`Backspace` | öffnen (Textdateien in nano rechts) / eine Ebene hoch |
| `e` / `O` | im Editor rechts / in Windows öffnen – `Ctrl+X` schließt nano |
| `1`–`8` | Layout wechseln |
| `Ctrl+T`, `F12` | Terminal ein/aus, reihum zwischen Dateien, Editor und Terminal wechseln |
| `Ctrl+↑` / `Ctrl+↓` | Terminal groß / zurück zur eingestellten Höhe |
| `d` / `D` | Container-Übersicht für Docker / Podman ein/aus (neben dem Terminal) |
| `/` | Filter |
| `b` / `B` | Favoriten / aktuellen Ordner merken |
| `g` | Pfad eingeben (auch `C:\Users\...`) |
| `F2` `F5` `F6` `F7` `Entf` | umbenennen, kopieren, verschieben, neuer Ordner, löschen |
| `P` | Vorschau |
| `.` | versteckte Dateien |
| `Ctrl+P` | Befehlspalette (u. a. Farbschema) |
| `?` | komplette Hilfe |

## Entwicklung

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
```

```bash
.venv/bin/pytest
```

Die Tests steuern die App headless über Textuals Pilot (Maus, Tastatur) und nutzen eine
Fake-Zwischenablage; der nano-Test wird übersprungen, wenn nano nicht installiert ist.

## Tipps

- Icons sehen mit einer [Nerd Font](https://www.nerdfonts.com/) noch besser aus – mit `i` umschalten (Emoji / Nerd Font / Einfach).
- Text im Terminal mit der Maus markieren – beim Loslassen ist er sofort 1:1 in der Zwischenablage
  (Hinweis unten rechts). Umbrochene lange Zeilen wie URLs bleiben dabei am Stück.
- Einstellungen liegen in `~/.config/wslui/config.json` – dort lässt sich z. B. mit
  `"editor": "micro"` ein anderer eingebetteter Editor wählen (Standard: `nano`).
- Die Container-Übersicht zeigt oben nur Knöpfe für installierte Programme (`docker`, `podman`).
  Ist der Dienst nicht erreichbar, steht die Fehlermeldung des Programms im Fenster.
