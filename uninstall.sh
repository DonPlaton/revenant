#!/usr/bin/env bash
# Remove what install.sh added.
#
# A clone is never touched. A copy the installer downloaded carries a marker file,
# and only a folder with that marker is deleted.

set -euo pipefail

MARKER=".revenant-managed"
removed=0

for path in \
  "$HOME/Applications/Revenant.app" \
  "$HOME/.local/share/applications/revenant.desktop" \
  "$HOME/.local/share/icons/hicolor/256x256/apps/revenant.png"
do
  if [ -e "$path" ] || [ -L "$path" ]; then
    rm -rf "$path"
    echo "removed $path"
    removed=1
  fi
done

# `revenant` in ~/.local/bin may just as well be the script `pip install --user`
# wrote, so only the launcher install.sh writes, the one that runs revenant.py, goes.
BIN="$HOME/.local/bin/revenant"
if grep -qs '/revenant.py"' "$BIN"; then
  rm -f "$BIN"
  echo "removed $BIN"
  removed=1
elif [ -e "$BIN" ]; then
  echo "left $BIN alone: it is not the launcher install.sh wrote"
fi

APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/revenant"
if [ -f "$APP_DIR/$MARKER" ]; then
  rm -rf "$APP_DIR"
  echo "removed $APP_DIR"
  removed=1
elif [ -d "$APP_DIR" ]; then
  echo "left $APP_DIR alone: no marker, so it is not a copy this installer made"
fi

command -v update-desktop-database >/dev/null &&
  update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true

[ "$removed" = 1 ] || echo "nothing to remove"

state="${XDG_STATE_HOME:-$HOME/.local/state}/revenant"
[ -d "$state" ] && echo "Revenant's own state is in $state, delete it by hand if you want it gone."
exit 0
