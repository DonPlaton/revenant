#!/usr/bin/env bash
# Remove what install.sh added. The folder itself is left alone.
set -euo pipefail

removed=0
for path in \
  "$HOME/Applications/Revenant.app" \
  "$HOME/.local/share/applications/revenant.desktop" \
  "$HOME/.local/share/icons/hicolor/256x256/apps/revenant.png" \
  "$HOME/.local/bin/revenant"
do
  if [ -e "$path" ] || [ -L "$path" ]; then
    rm -rf "$path"
    echo "removed $path"
    removed=1
  fi
done
[ "$removed" = 1 ] || echo "nothing to remove"

state="${XDG_STATE_HOME:-$HOME/.local/state}/revenant"
[ -d "$state" ] && echo "Revenant's own state is in $state, delete it by hand if you want it gone."
exit 0
