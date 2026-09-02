#!/bin/sh
# Double-click this on macOS to install Revenant. Same thing install.sh does, with
# the native window turned on, wrapped so Finder can run it.
#
# If double-clicking does nothing, the executable bit was lost (downloading a zip
# does that). Run `chmod +x "Install Revenant.command"` once and try again.
cd "$(dirname "$0")" || exit 1
./install.sh --native-window
status=$?
echo
[ $status -eq 0 ] && echo "You can close this window." || echo "Install failed with status $status."
exit $status
