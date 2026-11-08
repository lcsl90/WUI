#!/usr/bin/env bash
# WSLUI Installer – in der WSL ausführen:  bash install.sh [--autostart | --no-autostart | --uninstall]
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$HOME/.local/share/wslui"
BIN_DIR="$HOME/.local/bin"
CFG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/wslui"
RC_FILES=()
[ -f "$HOME/.bashrc" ] && RC_FILES+=("$HOME/.bashrc")
[ -f "$HOME/.zshrc" ] && RC_FILES+=("$HOME/.zshrc")
[ ${#RC_FILES[@]} -eq 0 ] && { touch "$HOME/.bashrc"; RC_FILES+=("$HOME/.bashrc"); }

AUTOSTART=ask
UNINSTALL=0
for arg in "$@"; do
  case "$arg" in
    --autostart) AUTOSTART=yes ;;
    --no-autostart) AUTOSTART=no ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help) sed -n 2p "$0"; exit 0 ;;
    *) echo "Unbekannte Option: $arg" >&2; exit 1 ;;
  esac
done

c_ok=$'\e[1;32m'; c_info=$'\e[1;36m'; c_off=$'\e[0m'

remove_block() {
  local rc="$1"
  if grep -q '# >>> wslui >>>' "$rc" 2>/dev/null; then
    sed -i '/# >>> wslui >>>/,/# <<< wslui <<</d' "$rc"
  fi
}

if [ "$UNINSTALL" = 1 ]; then
  for rc in "${RC_FILES[@]}"; do remove_block "$rc"; done
  rm -rf "$APP_DIR" "$BIN_DIR/wslui"
  rm -f "$CFG_DIR/autostart"
  echo "${c_ok}✔ WSLUI entfernt${c_off} (Einstellungen bleiben in $CFG_DIR)"
  exit 0
fi

echo "${c_info}▶ Installiere WSLUI nach $APP_DIR${c_off}"
if ! python3 -c 'import venv, ensurepip' >/dev/null 2>&1; then
  echo "python3-venv fehlt. Bitte zuerst ausführen:  sudo apt install -y python3-venv" >&2
  exit 1
fi
mkdir -p "$APP_DIR" "$BIN_DIR" "$CFG_DIR"
[ -x "$APP_DIR/venv/bin/python" ] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --disable-pip-version-check --upgrade "textual>=8,<9" "pyte>=0.8"
install -m 644 "$SRC_DIR/wslui.py" "$APP_DIR/wslui.py"

cat > "$BIN_DIR/wslui" <<EOF
#!/usr/bin/env bash
exec "$APP_DIR/venv/bin/python" "$APP_DIR/wslui.py" "\$@"
EOF
chmod +x "$BIN_DIR/wslui"

read -r -d '' BLOCK <<'EOF' || true
# >>> wslui >>>
# WSLUI-Dateimanager. 'wui' startet ihn; beim Beenden mit q landet die Shell im letzten Ordner.
wui() {
  local _f _d
  _f="$(mktemp "${TMPDIR:-/tmp}/wslui-cd.XXXXXX")" || return
  WSLUI_ACTIVE=1 "$HOME/.local/share/wslui/venv/bin/python" "$HOME/.local/share/wslui/wslui.py" --cd-file "$_f" "$@"
  _d="$(cat "$_f" 2>/dev/null)"; rm -f "$_f"
  if [ -n "$_d" ] && [ -d "$_d" ] && [ "$_d" != "$PWD" ]; then cd -- "$_d" || true; fi
}
# Autostart (an/aus: in WSLUI Taste A, oder: wslui --autostart on|off)
case "$-" in *i*)
  if [ -z "${WSLUI_ACTIVE:-}" ] && [ -z "${WSLUI_NO_AUTOSTART:-}" ] \
     && [ -f "${XDG_CONFIG_HOME:-$HOME/.config}/wslui/autostart" ] \
     && [ -z "${TMUX:-}" ] && [ "${TERM_PROGRAM:-}" != "vscode" ] \
     && [ -t 0 ] && [ -t 1 ] && [ "${SHLVL:-1}" -le 1 ]; then
    wui
  fi
;; esac
# <<< wslui <<<
EOF

for rc in "${RC_FILES[@]}"; do
  [ -f "$rc.wslui-backup" ] || cp "$rc" "$rc.wslui-backup"
  remove_block "$rc"
  printf '\n%s\n' "$BLOCK" >> "$rc"
done

if [ "$AUTOSTART" = ask ]; then
  if [ -t 0 ]; then
    read -r -p "WSLUI beim Öffnen von WSL automatisch starten? [J/n] " ans
    case "${ans,,}" in n|nein|no) AUTOSTART=no ;; *) AUTOSTART=yes ;; esac
  else
    AUTOSTART=no
  fi
fi
if [ "$AUTOSTART" = yes ]; then touch "$CFG_DIR/autostart"; else rm -f "$CFG_DIR/autostart"; fi

echo "${c_ok}✔ Fertig!${c_off}"
echo "  Starten:     wui            (neues Terminal öffnen oder: source ~/.bashrc)"
echo "  Autostart:   $([ "$AUTOSTART" = yes ] && echo AN || echo AUS)  – umschalten mit Taste A in WSLUI"
echo "  Hilfe:       ? in WSLUI"
