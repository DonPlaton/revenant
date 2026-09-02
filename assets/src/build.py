#!/usr/bin/env python3
"""Regenerate every image in `assets/` from the sources next to this file.

Needs headless Chrome (or Edge) for rendering and Pillow for the ICO and the GIF:

    python -m pip install pillow
    python assets/src/build.py [icon|card|infographic|gif]

The demo GIF captures the *real* UI: it starts the desktop backend against your own
Claude Code config and screenshots the page at a series of slider positions, so the
numbers in it are whatever your machine actually has.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ASSETS = HERE.parent
ROOT = ASSETS.parent

BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "google-chrome",
    "chromium",
]

GIF_STOPS = [0.25, 1, 3, 7, 14, 30, 90]
GIF_WIDTH = 760
WINDOW = (1000, 700)


def _pretty(path: Path) -> str:
    """Repo-relative when it lives here, absolute otherwise (temp frames do not)."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def browser() -> str:
    from shutil import which

    for candidate in BROWSERS:
        if Path(candidate).exists() or which(candidate):
            return candidate
    raise SystemExit("Headless Chrome or Edge is required to render the images.")


def shoot(url: str, out: Path, size: tuple[int, int], *, scale: int = 1) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            browser(),
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            f"--screenshot={out}",
            f"--window-size={size[0]},{size[1]}",
            f"--force-device-scale-factor={scale}",
            "--virtual-time-budget=9000",
            url,
        ],
        capture_output=True,
        timeout=180,
        check=False,
    )
    if not out.exists():
        raise SystemExit(f"Chrome did not produce {out}")
    print(f"  {_pretty(out)}")


def build_icon() -> None:
    from PIL import Image

    print("icon")
    with tempfile.TemporaryDirectory() as tmp:
        png = Path(tmp) / "icon.png"
        shoot((ASSETS / "icon.svg").as_uri(), png, (256, 256))
        image = Image.open(png).convert("RGBA")
    image.save(ASSETS / "icon.png")
    image.save(
        ASSETS / "icon.ico",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print(f"  {_pretty(ASSETS / 'icon.ico')}")


def build_card() -> None:
    print("social card")
    shoot((HERE / "social-card.html").as_uri(), ASSETS / "social-card.png", (1280, 640))


def build_infographic() -> None:
    print("infographic")
    shoot((HERE / "infographic.html").as_uri(), ASSETS / "how-it-works.png", (1200, 600))


def build_gif() -> None:
    """Screenshot the UI at each slider stop, then play it forward and back.

    Runs against a synthetic config directory, never against `~/.claude` - the images
    in this repo must not carry anyone's real paths or prompts.
    """
    import dataclasses

    from PIL import Image

    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(HERE))
    import demo_fixture
    import revenant
    import revenant_gui

    print("demo gif (synthetic sessions)")
    # Two long-lived children stand in for running sessions, so the locked rows are real.
    stand_ins = [
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(900)"])
        for _ in range(len(demo_fixture.LIVE))
    ]
    frames = []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = demo_fixture.build(Path(tmp) / "config", [p.pid for p in stand_ins])
            agent = dataclasses.replace(
                revenant.CLAUDE_CODE,
                process_images=revenant.CLAUDE_CODE.process_images | {"python.exe", "python", "python3"},
            )
            backend = revenant_gui.Backend(agent=agent, root=str(root))
            server, url = revenant_gui.serve(backend)
            base = url.split("/?")[0]
            try:
                for index, days in enumerate(GIF_STOPS):
                    shot = Path(tmp) / f"{index}.png"
                    shoot(f"{base}/?t={backend.token}&still=1&days={days}", shot, WINDOW)
                    height = round(GIF_WIDTH * WINDOW[1] / WINDOW[0])
                    frames.append(
                        Image.open(shot).convert("RGB").resize((GIF_WIDTH, height), Image.LANCZOS)
                    )
            finally:
                server.shutdown()
    finally:
        for process in stand_ins:
            process.terminate()

    sequence = frames + frames[-2:0:-1]
    durations = [900] * len(sequence)
    durations[0] = durations[len(frames) - 1] = 1500  # hold both ends
    out = ASSETS / "demo.gif"
    sequence[0].save(
        out,
        save_all=True,
        append_images=sequence[1:],
        duration=durations,
        loop=0,
        optimize=True,
        disposal=2,
    )
    print(f"  {_pretty(out)} ({out.stat().st_size // 1024} KB, {len(sequence)} frames)")


TARGETS = {
    "icon": build_icon,
    "card": build_card,
    "infographic": build_infographic,
    "gif": build_gif,
}


def main(argv: list[str]) -> int:
    wanted = argv or list(TARGETS)
    unknown = [name for name in wanted if name not in TARGETS]
    if unknown:
        raise SystemExit(f"Unknown target(s): {', '.join(unknown)}. Pick from: {', '.join(TARGETS)}")
    for name in wanted:
        TARGETS[name]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
