#!/usr/bin/env python3
"""Check that an install produced a launcher that actually points at the app.

Used by CI on all three platforms, after install and again after uninstall:

    python .github/verify-install.py            # everything is there
    python .github/verify-install.py --absent    # everything is gone

Not a pytest module. The tests run against a synthetic config directory and touch
nothing; this one inspects the real machine, which only a throwaway runner should do.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

HOME = Path.home()


def shortcut_target(link: Path) -> str:
    """Resolve a Windows .lnk through the same COM object that wrote it."""
    script = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{0}');"
        "Write-Output $s.TargetPath; Write-Output $s.Arguments"
    ).format(str(link).replace("'", "''"))
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout


def managed_dir() -> Path:
    """Where the installer puts the app when it had to download it."""
    if sys.platform == "win32":
        return Path(os.environ["LOCALAPPDATA"]) / "Programs/Revenant"
    data = os.environ.get("XDG_DATA_HOME") or str(HOME / ".local/share")
    return Path(data) / "revenant"


def launchers() -> list[Path]:
    """Every file an install is expected to leave behind, for this platform."""
    if sys.platform == "win32":
        start_menu = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs"
        return [HOME / "Desktop/Revenant.lnk", start_menu / "Revenant.lnk"]
    if sys.platform == "darwin":
        return [HOME / "Applications/Revenant.app/Contents/MacOS/revenant"]
    return [HOME / ".local/share/applications/revenant.desktop"]


def app_path_in(launcher: Path) -> str:
    """The path to revenant_gui.py that this launcher will run."""
    if launcher.suffix == ".lnk":
        return shortcut_target(launcher)
    text = launcher.read_text(encoding="utf-8")
    if launcher.suffix == ".desktop":
        match = re.search(r"^Exec=(.*)$", text, re.MULTILINE)
        return match.group(1) if match else ""
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--absent", action="store_true", help="assert the install is gone instead")
    parser.add_argument(
        "--managed", action="store_true", help="assert the launcher runs a downloaded copy"
    )
    args = parser.parse_args()

    problems: list[str] = []
    found = 0
    managed = managed_dir()

    if args.absent and managed.exists():
        problems.append(f"the downloaded copy is still there: {managed}")

    for launcher in launchers():
        exists = launcher.exists()
        if args.absent:
            if exists:
                problems.append(f"still there after uninstall: {launcher}")
            continue
        if not exists:
            # Windows offers two places and a machine may not have both, so the bar
            # is one working launcher rather than every one of them.
            print(f"  (not created: {launcher})")
            continue

        found += 1
        command = app_path_in(launcher)
        print(f"  {launcher}\n    -> {command.strip()}")

        quoted = re.findall(r'"([^"]+)"', command) or command.split()
        targets = [Path(part) for part in quoted if part.endswith("revenant_gui.py")]
        if not targets:
            problems.append(f"{launcher} does not name revenant_gui.py")
        elif not targets[0].is_file():
            problems.append(f"{launcher} points at {targets[0]}, which does not exist")
        elif args.managed and managed.resolve() not in targets[0].resolve().parents:
            problems.append(f"{launcher} points at {targets[0]}, which is not inside {managed}")

        if sys.platform != "win32" and launcher.suffix != ".desktop" and not os.access(launcher, os.X_OK):
            problems.append(f"{launcher} is not executable")

    if not args.absent and not found:
        problems.append("no launcher was created at all")

    for problem in problems:
        print(f"FAIL: {problem}", file=sys.stderr)
    if problems:
        return 1

    print("absent, as expected" if args.absent else f"{found} launcher(s) verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
