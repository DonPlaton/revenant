#!/bin/sh
# Double-click this on macOS to install Revenant. Same thing install.sh does,
# wrapped so Finder can run it. Add --native-window below for a frameless window
# instead of a chromeless browser one; it installs pywebview, which is why it is
# not the default.
#
# If double-clicking does nothing, the executable bit was lost (downloading a zip
# does that). Run `chmod +x "Install Revenant.command"` once and try again.
cd "$(dirname "$0")" || exit 1
# Called through bash rather than run directly, because a zip download strips the
# executable bit from install.sh too.
bash ./install.sh
status=$?
echo
[ $status -eq 0 ] && echo "You can close this window." || echo "Install failed with status $status."
exit $status
