#!/usr/bin/env bash
# Install Revenant as a desktop app on macOS or Linux.
#
#   ./install.sh                 shortcuts only
#   ./install.sh --native-window also install pywebview, for a real app window
#   ./install.sh --cli           also link `revenant` into ~/.local/bin
#
# Nothing is copied out of this folder. ./uninstall.sh removes what this adds.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NATIVE=0
CLI=0
for arg in "$@"; do
  case "$arg" in
    --native-window) NATIVE=1 ;;
    --cli) CLI=1 ;;
    -h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

PYTHON="$(command -v python3 || command -v python || true)"
[ -n "$PYTHON" ] || { echo "Python 3.10+ is required and was not found on PATH." >&2; exit 1; }

if [ "$NATIVE" = 1 ]; then
  echo "Installing pywebview for a native window..."
  "$PYTHON" -m pip install --quiet --upgrade pywebview
fi

case "$(uname -s)" in
  Darwin)
    APP="$HOME/Applications/Revenant.app"
    mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
    cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Revenant</string>
  <key>CFBundleDisplayName</key><string>Revenant</string>
  <key>CFBundleIdentifier</key><string>dev.revenant.app</string>
  <key>CFBundleVersion</key><string>1.1.0</string>
  <key>CFBundleExecutable</key><string>revenant</string>
  <key>CFBundleIconFile</key><string>revenant.icns</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST
    cat > "$APP/Contents/MacOS/revenant" <<LAUNCHER
#!/bin/sh
exec "$PYTHON" "$HERE/revenant_gui.py"
LAUNCHER
    chmod +x "$APP/Contents/MacOS/revenant"

    # iconutil wants a full iconset; sips is on every macOS.
    if command -v sips >/dev/null && command -v iconutil >/dev/null; then
      SET="$(mktemp -d)/revenant.iconset"
      mkdir -p "$SET"
      for size in 16 32 64 128 256 512; do
        sips -z $size $size "$HERE/assets/icon.png" --out "$SET/icon_${size}x${size}.png" >/dev/null 2>&1 || true
        sips -z $((size * 2)) $((size * 2)) "$HERE/assets/icon.png" \
          --out "$SET/icon_${size}x${size}@2x.png" >/dev/null 2>&1 || true
      done
      iconutil -c icns "$SET" -o "$APP/Contents/Resources/revenant.icns" 2>/dev/null || true
    fi
    echo "Installed $APP"
    echo "Open it from Launchpad or Spotlight."
    ;;

  Linux)
    DESKTOP="$HOME/.local/share/applications/revenant.desktop"
    ICONS="$HOME/.local/share/icons/hicolor/256x256/apps"
    mkdir -p "$(dirname "$DESKTOP")" "$ICONS"
    cp -f "$HERE/assets/icon.png" "$ICONS/revenant.png" 2>/dev/null || true
    cat > "$DESKTOP" <<DESKTOPFILE
[Desktop Entry]
Type=Application
Name=Revenant
Comment=Bring your agent sessions back from the dead
Exec=$PYTHON $HERE/revenant_gui.py
Path=$HERE
Icon=revenant
Terminal=false
Categories=Development;Utility;
StartupNotify=true
DESKTOPFILE
    chmod +x "$DESKTOP"
    command -v update-desktop-database >/dev/null && \
      update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
    echo "Installed $DESKTOP"
    echo "Look for Revenant in your application menu."
    ;;

  *)
    echo "This installer covers macOS and Linux. On Windows run install.ps1." >&2
    exit 1
    ;;
esac

if [ "$CLI" = 1 ]; then
  mkdir -p "$HOME/.local/bin"
  ln -sf "$HERE/revenant.py" "$HOME/.local/bin/revenant"
  chmod +x "$HERE/revenant.py"
  echo
  echo "Linked $HOME/.local/bin/revenant"
  case ":$PATH:" in
    *":$HOME/.local/bin:"*) echo "That folder is already on your PATH." ;;
    *) echo "Add $HOME/.local/bin to your PATH to call it from anywhere." ;;
  esac
fi
