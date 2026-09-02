#!/usr/bin/env bash
# Install Revenant as a desktop app on macOS or Linux.
#
# Two ways in, and it works out which one you used:
#
#   - Run it from a clone and the launcher points at that folder. Nothing is copied.
#   - Pipe it from the web and it downloads the app into ~/.local/share/revenant
#     first, then points the launcher there.
#
#   ./install.sh                  the app, and nothing else
#   ./install.sh --native-window  also install pywebview, for a real app window
#   ./install.sh --cli            also link `revenant` into ~/.local/bin
#   ./install.sh --ref v1.2.0     which version to download, when run from the web
#
# ./uninstall.sh removes exactly what this adds.

set -euo pipefail

REPO="DonPlaton/revenant"
MARKER=".revenant-managed"
NATIVE=0
CLI=0
REF="main"

while [ $# -gt 0 ]; do
  case "$1" in
    --native-window) NATIVE=1 ;;
    --cli) CLI=1 ;;
    --ref) REF="${2:?--ref needs a branch, tag or commit}"; shift ;;
    --ref=*) REF="${1#--ref=}" ;;
    -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

case "$(uname -s)" in
  Darwin) PLATFORM=mac ;;
  Linux)  PLATFORM=linux ;;
  *) echo "This installer covers macOS and Linux. On Windows run install.ps1." >&2; exit 1 ;;
esac

# --------------------------------------------------------------------------- #
# python
# --------------------------------------------------------------------------- #

# Named versions come first: the bare `python3` on an older macOS is 3.9, which
# cannot run this, while a newer one sits right beside it under its own name.
PYTHON=""
for candidate in python3.14 python3.13 python3.12 python3.11 python3.10 python3 python; do
  path="$(command -v "$candidate" 2>/dev/null || true)"
  [ -n "$path" ] || continue
  if "$path" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PYTHON="$path"
    break
  fi
done

if [ -z "$PYTHON" ]; then
  echo "Revenant needs Python 3.10 or newer, and none was found." >&2
  echo >&2
  if [ "$PLATFORM" = mac ]; then
    echo "  brew install python@3.13" >&2
  else
    echo "  sudo apt install python3      # or your distribution's equivalent" >&2
  fi
  echo >&2
  echo "or download it from https://python.org/downloads, then run this again." >&2
  exit 1
fi

# --------------------------------------------------------------------------- #
# where the app is going to live
# --------------------------------------------------------------------------- #

SOURCE=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

if [ -n "$SOURCE" ] && [ -f "$SOURCE/revenant_gui.py" ]; then
  HERE="$SOURCE"
  echo "Installing Revenant from $HERE"
else
  # Piped from the web: there is no folder yet, so make one.
  HERE="${XDG_DATA_HOME:-$HOME/.local/share}/revenant"
  echo "Downloading Revenant ($REF) to $HERE"

  if command -v curl >/dev/null 2>&1; then
    fetch() { curl -fsSL "$1"; }
  elif command -v wget >/dev/null 2>&1; then
    fetch() { wget -qO- "$1"; }
  else
    echo "Neither curl nor wget is available, so the download cannot start." >&2
    exit 1
  fi

  STAGING="$(mktemp -d)"
  trap 'rm -rf "$STAGING"' EXIT
  # This form of the URL takes a branch, a tag or a commit alike. The override is
  # for installing from a mirror, and for testing this path without GitHub.
  fetch "${REVENANT_ARCHIVE_URL:-https://github.com/$REPO/archive/$REF.tar.gz}" | tar -xz -C "$STAGING"
  UNPACKED="$(find "$STAGING" -mindepth 1 -maxdepth 1 -type d | head -1)"
  [ -n "$UNPACKED" ] || { echo "The download did not contain a source folder." >&2; exit 1; }

  if [ -d "$HERE" ] && [ ! -f "$HERE/$MARKER" ]; then
    echo "$HERE already exists and was not put there by this installer. Move it aside first." >&2
    exit 1
  fi
  rm -rf "$HERE"
  mkdir -p "$(dirname "$HERE")"
  mv "$UNPACKED" "$HERE"
  echo "$REF" > "$HERE/$MARKER"
  echo "  downloaded"
fi

TARGET="$HERE/revenant_gui.py"
[ -f "$TARGET" ] || { echo "revenant_gui.py is missing from $HERE" >&2; exit 1; }
VERSION="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$HERE/revenant.py" | head -1)"
VERSION="${VERSION:-1.0.0}"

# --------------------------------------------------------------------------- #
# the native window, which is optional by design
# --------------------------------------------------------------------------- #

if [ "$NATIVE" = 1 ]; then
  echo "Installing pywebview for a native window..."
  # Distributions that manage their own Python refuse this (PEP 668). Losing the
  # native window is no reason to leave the machine with no launcher at all.
  "$PYTHON" -m pip install --quiet --upgrade pywebview 2>/dev/null ||
    "$PYTHON" -m pip install --quiet --upgrade --user pywebview 2>/dev/null || {
      echo "  pywebview would not install; the app will open in a browser window instead."
      echo "  For the native window, install pywebview into a virtualenv or with pipx."
    }
fi

# --------------------------------------------------------------------------- #
# the app itself
# --------------------------------------------------------------------------- #

if [ "$PLATFORM" = mac ]; then
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
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
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
exec "$PYTHON" "$TARGET"
LAUNCHER
  chmod +x "$APP/Contents/MacOS/revenant"

  # iconutil wants a full iconset; sips is on every macOS.
  if command -v sips >/dev/null && command -v iconutil >/dev/null && [ -f "$HERE/assets/icon.png" ]; then
    SET="$(mktemp -d)/revenant.iconset"
    mkdir -p "$SET"
    for size in 16 32 64 128 256 512; do
      sips -z $size $size "$HERE/assets/icon.png" --out "$SET/icon_${size}x${size}.png" >/dev/null 2>&1 || true
      sips -z $((size * 2)) $((size * 2)) "$HERE/assets/icon.png" \
        --out "$SET/icon_${size}x${size}@2x.png" >/dev/null 2>&1 || true
    done
    iconutil -c icns "$SET" -o "$APP/Contents/Resources/revenant.icns" 2>/dev/null || true
  fi
  echo "  app -> $APP"
  WHERE="Open it from Launchpad or Spotlight."
else
  DESKTOP="$HOME/.local/share/applications/revenant.desktop"
  ICONS="$HOME/.local/share/icons/hicolor/256x256/apps"
  mkdir -p "$(dirname "$DESKTOP")" "$ICONS"
  cp -f "$HERE/assets/icon.png" "$ICONS/revenant.png" 2>/dev/null || true
  cat > "$DESKTOP" <<DESKTOPFILE
[Desktop Entry]
Type=Application
Name=Revenant
Comment=Bring your agent sessions back from the dead
Exec="$PYTHON" "$TARGET"
Path=$HERE
Icon=revenant
Terminal=false
Categories=Development;Utility;
StartupNotify=true
DESKTOPFILE
  chmod +x "$DESKTOP"
  command -v update-desktop-database >/dev/null &&
    update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
  echo "  launcher -> $DESKTOP"
  WHERE="Look for Revenant in your application menu."
fi

if [ "$CLI" = 1 ]; then
  mkdir -p "$HOME/.local/bin"
  ln -sf "$HERE/revenant.py" "$HOME/.local/bin/revenant"
  chmod +x "$HERE/revenant.py"
  echo "  command -> $HOME/.local/bin/revenant"
  case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) echo "  (add $HOME/.local/bin to your PATH to call it from anywhere)" ;;
  esac
fi

echo
echo "Revenant $VERSION is installed."
echo "$WHERE"
