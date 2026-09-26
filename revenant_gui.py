#!/usr/bin/env python3
"""Revenant desktop app.

One backend, three ways to show it:

1. a native window via `pywebview` (preferred, since it looks and behaves like an app),
2. a chromeless Chrome/Edge window via `--app=` (no extra dependency),
3. the default browser (always works).

The backend is a stdlib HTTP server bound to 127.0.0.1 on an ephemeral port. Every
request must carry a token minted at startup and must be addressed to that exact
host:port, so nothing else on the machine can drive it. The server stops when the
window closes.
"""

from __future__ import annotations

import io
import json
import math
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import timedelta, datetime, timezone
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import revenant_agents as agent_registry
import revenant
import revenant_terminals as terminals
from revenant import Agent, CLAUDE_CODE

def _ui_dir() -> Path:
    """Where `index.html` lives: next to this module in a clone, or the data dir.

    A wheel installs the modules into site-packages and the UI under
    `<prefix>/share/revenant/ui`, so both layouts have to work.
    """
    beside = Path(__file__).resolve().parent / "ui"
    if (beside / "index.html").is_file():
        return beside
    shared = Path(sys.prefix) / "share" / "revenant" / "ui"
    return shared if (shared / "index.html").is_file() else beside


UI_DIR = _ui_dir()
MAX_BODY_BYTES = 256 * 1024
#: Sessions one request may name. More than this is almost certainly a mistake,
#: and the page says which ones were left behind.
MAX_IDS = 200
#: The longest a revival may be held back to meet its cards, and the widest gap
#: between two of its windows. More is cut to these.
MAX_WAIT_MS = 5000.0
MAX_STAGGER_MS = 400.0
#: How long after the call a terminal's window shows, so the page can start the
#: launch this much before the cards land. Windows Terminal was filmed at about
#: 330 ms from the call to its first frame; the rest are a guess on the late side.
LEAD_MS = {"wt": 250, "tmux": 0, "": 400}
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


class Backend:
    """Everything the UI can ask for. Deliberately small."""

    #: A scan costs a directory walk plus a `tasklist` subprocess, and the UI fires
    #: several requests per click, so an identical scan is reused for a moment.
    CACHE_SECONDS = 8.0

    def __init__(self, *, agent: Agent = CLAUDE_CODE, root: str | None = None) -> None:
        self.agent = agent
        self.root = revenant.config_root(root, agent=agent)
        self.explicit_root = root is not None
        self.token = secrets.token_urlsafe(24)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._cache: tuple[tuple[float, str], float, list[revenant.Session]] | None = None
        self._layouts: dict | None = None
        self.last_seen = time.monotonic()

    # -- data ------------------------------------------------------------- #

    def choices(self) -> list[dict]:
        """The agents this machine can offer, plus the combined view."""
        found = [
            {"key": agent.key, "label": agent.label, "note": agent.liveness_note}
            for agent in self.available()
        ]
        if len(found) > 1:
            found.append({"key": "all", "label": "All", "note": ""})
        return found

    def available(self) -> list[Agent]:
        if self.explicit_root:
            return [self.agent]
        installed = agent_registry.installed_agents()
        return installed or [self.agent]

    def _scan(self, days: float, which: str) -> list[revenant.Session]:
        """Scan back `days` for one agent or all of them, reusing a recent scan."""
        now = time.monotonic()
        key = (days, which)
        with self._lock:
            cached = self._cache
        if cached and cached[0] == key and now - cached[1] < self.CACHE_SECONDS:
            return cached[2]

        since = datetime.now(timezone.utc) - timedelta(days=max(days, 1 / 24))
        if which == "all" and not self.explicit_root:
            found = revenant.scan_all(since=since, agents=self.available())
        else:
            agent = next((a for a in self.available() if a.key == which), self.agent)
            root = self._root_for(agent)
            found = revenant.scan_sessions(root, since=since, agent=agent)
        with self._lock:
            self._cache = (key, time.monotonic(), found)
        return found

    def invalidate(self) -> None:
        with self._lock:
            self._cache = None

    def layouts(self) -> dict:
        """What each layout choice really opens here, by the rule a launch uses.

        The page draws the deal from this and marks a choice the terminal cannot
        make, instead of promising tabs a console will open as windows.
        """
        if self._layouts is None:
            found = {}
            for wanted in terminals.LAYOUTS:
                terminal = terminals.choose(layout=wanted)
                found[wanted] = {
                    "terminal": terminal.label,
                    "opens": terminal.settle(wanted),
                    "note": terminal.demoted(wanted),
                    # How long its windows take to show after the call, so the
                    # page can ask for them to arrive as the cards land.
                    "lead": LEAD_MS.get(terminal.key, LEAD_MS[""]),
                }
            self._layouts = found
        return self._layouts

    def sessions(self, *, days: float, which: str = "", include_live: bool = True) -> dict:
        which = which or self.agent.key
        choices = self.choices()
        if which not in {c["key"] for c in choices}:
            which = choices[0]["key"] if choices else self.agent.key

        if which != "all" and not self.root.is_dir() and self.explicit_root:
            return {
                "error": f"{self.agent.label} config directory not found: {self.root}",
                "sessions": [],
                "agents": choices,
                "agent": which,
                "layouts": self.layouts(),
            }

        found = self._scan(days, which)
        selected = revenant.filter_sessions(found, include_live=include_live, limit=None)
        revenant.name_sessions(selected)
        # An explicit --root is what was actually read, so it is what gets shown.
        if self.explicit_root:
            where = str(self.root)
        elif which == "all":
            where = "several places"
        else:
            where = str(
                next((a.config_dir() for a in self.available() if a.key == which), self.root)
            )
        return {
            "sessions": [revenant.session_to_dict(s) for s in selected],
            "agents": choices,
            "agent": which,
            "root": where,
            "error": None,
            "layouts": self.layouts(),
        }

    def _by_id(self, ids: list[str], *, days: float, which: str = "") -> list[revenant.Session]:
        if not ids:
            return []
        by_id = {s.session_id: s for s in self._scan(days, which or self.agent.key)}
        return [by_id[i] for i in dict.fromkeys(ids) if i in by_id]

    # -- placing -------------------------------------------------------- #

    def _spot(self, layout: str, room: object) -> tuple[terminals.Spot, terminals.Cells, float] | None:
        """Where the page asked for a tabbed revival's window, in screen pixels,
        or None when the terminal cannot be told or the window would not fit.

        `room` is in the page's own units, which are screen pixels divided by
        `scale`: the work area as left, top, right, bottom, where the window may
        start, and the band its tab strip has to fall in.
        """
        if not isinstance(room, dict) or layout != terminals.LAYOUT_TABS:
            return None
        try:
            scale = float(room["scale"])
            screen = [float(value) for value in room["screen"]]
            left, top, bottom = float(room["left"]), float(room["top"]), float(room["bottom"])
        except (KeyError, TypeError, ValueError):
            return None
        if len(screen) != 4 or not all(math.isfinite(value) for value in (scale, left, top, bottom, *screen)):
            return None
        terminal = terminals.choose(layout=layout)
        if terminal.key != "wt" or terminal.settle(layout) != layout:
            return None
        cells = terminals.wt_cells(terminals.wt_settings(), scale)
        if cells is None:
            return None

        def px(value: float) -> int:
            return round(value * scale)

        spot = terminals.place(terminals.Room(tuple(px(v) for v in screen), px(left), px(top), px(bottom)), cells)
        return (spot, cells, scale) if spot else None

    def place(self, layout: str, room: object) -> dict:
        """Where the window of a tabbed revival will come up, in the page's units,
        so the deal can throw each card into the tab it will become."""
        found = self._spot(layout, room)
        if not found:
            return {"spots": []}
        spot, cells, scale = found
        return {
            "spots": [{"x": spot.x / scale, "y": spot.y / scale,
                       "width": spot.width / scale, "height": spot.height / scale}],
            "strip": cells.strip / scale,
        }

    # -- actions ---------------------------------------------------------- #

    def revive(
        self,
        ids: list[str],
        *,
        days: float,
        which: str = "",
        layout: str = "",
        room: object = None,
        wait_ms: float = 0.0,
        stagger_ms: float = 0.0,
    ) -> dict:
        """Launch the sessions. With a `room` a tabbed revival's window goes where
        the page drew it. `wait_ms` from now the first is started, so it comes
        up as the cards land, and `stagger_ms` spaces the rest."""
        arrived = time.monotonic()
        chosen = self._by_id(ids, days=days, which=which)
        if not chosen:
            return {"ok": False, "message": "Those sessions are no longer on disk.", "count": 0, "raised": []}

        revenant.refresh_liveness(chosen, self._root_for)
        running = [s for s in chosen if s.is_live]
        chosen = [s for s in chosen if not s.is_live]
        if not chosen:
            return {
                "ok": False,
                "count": 0,
                "raised": [],
                "message": "Every session you picked is still running, so there is nothing to bring back.",
            }
        # A session with no folder on record cannot be opened anywhere, and the
        # launch drops it, so it is not counted as raised either.
        placed = [s for s in chosen if s.cwd]

        sink = io.StringIO()
        # An unknown layout is the UI being out of step with the backend, which is
        # no reason to refuse the rescue: fall back to the default and open them.
        wanted = layout if layout in terminals.LAYOUTS else terminals.DEFAULT_LAYOUT
        came_up: list[revenant.Session] = []
        found = self._spot(wanted, room) if room else None
        start_in = max(0.0, arrived + min(max(wait_ms, 0.0), MAX_WAIT_MS) / 1000 - time.monotonic())
        code = revenant.launch(chosen, layout=wanted, stream=sink, landed=came_up,
                               place=found[0] if found else None, start_in=start_in,
                               stagger=min(max(stagger_ms, 0.0), MAX_STAGGER_MS) / 1000)
        self.invalidate()  # a revived session becomes live as soon as it registers
        note = [line for line in sink.getvalue().strip().splitlines() if line]
        message = " ".join(note[-2:]) if note else ""
        if running:
            message += f" {len(running)} held back."
        if len(placed) < len(chosen):
            message += f" {len(chosen) - len(placed)} had no folder on record."
        raised = [s.session_id for s in came_up] if code == 0 else []
        return {"ok": code == 0, "count": len(raised), "raised": raised, "message": message.strip()}

    def _root_for(self, agent: Agent) -> Path:
        """Where an agent's files are read from, by the same rule as a scan."""
        if self.explicit_root or agent.key == self.agent.key:
            return self.root
        return agent.config_dir()

    def commands(self, ids: list[str], *, days: float, which: str = "") -> dict:
        chosen = self._by_id(ids, days=days, which=which)
        shell = "pwsh" if os.name == "nt" else "bash"
        return {"text": revenant.render_commands(chosen, shell=shell), "count": len(chosen)}

    def reveal(self, path: str) -> dict:
        """Open a session's folder in the file manager."""
        target = Path(path)
        if not target.is_dir():
            return {"ok": False, "message": "Folder no longer exists."}
        try:
            if os.name == "nt":
                os.startfile(str(target))  # noqa: S606 - user-initiated, path came from our own scan
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(target)])
            else:
                subprocess.Popen(["xdg-open", str(target)])
        except OSError as exc:
            return {"ok": False, "message": str(exc)}
        return {"ok": True}

    # -- lifecycle -------------------------------------------------------- #

    def request_stop(self) -> None:
        self._stop.set()

    def touch(self) -> None:
        self.last_seen = time.monotonic()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def wait_until_idle(self, *, grace: float = 20.0) -> None:
        """Block until the UI asks to quit or stops sending heartbeats.

        A window can go away in ways that never reach us: the OS close button, a
        browser crash, or a second launch handing the URL to an already running
        browser and exiting at once. Silence is therefore the signal, not the
        lifetime of the process we spawned.
        """
        # The clock starts now, not when the backend was built: a browser doing a
        # cold start with a fresh profile can easily eat the whole grace period
        # before it ever asks for the page.
        self.touch()
        while not self._stop.wait(2.0):
            if time.monotonic() - self.last_seen > grace:
                return


class Handler(BaseHTTPRequestHandler):
    """Serves the UI and a tiny JSON API. Token-gated, localhost-only."""

    server_version = f"Revenant/{revenant.__version__}"
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, backend: Backend, **kwargs) -> None:
        self.backend = backend
        super().__init__(*args, **kwargs)

    # Quiet by default; the console belongs to the user.
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        if os.environ.get("REVENANT_DEBUG"):
            super().log_message(fmt, *args)

    # -- helpers ---------------------------------------------------------- #

    def _host_is_ours(self) -> bool:
        """Reject cross-origin/rebinding attempts aimed at our port."""
        expected = f"127.0.0.1:{self.server.server_address[1]}"
        return self.headers.get("Host", "") == expected

    def _authorised(self, query: dict[str, list[str]]) -> bool:
        supplied = (query.get("t") or [self.headers.get("X-Revenant-Token", "")])[0]
        # compare_digest raises TypeError on non-ASCII text; compare bytes instead.
        return secrets.compare_digest(
            supplied.encode("utf-8", "surrogatepass"), self.backend.token.encode("utf-8")
        )

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _frame(self, which: str) -> dict:
        """What the page needs to stay usable around a failed scan: the agent
        tabs to switch away with, and what each layout opens."""
        frame: dict = {"agent": which or self.backend.agent.key}
        for key, read, empty in (("agents", self.backend.choices, []), ("layouts", self.backend.layouts, {})):
            try:
                frame[key] = read()
            except Exception:  # noqa: BLE001 - the frame is best effort around an error
                frame[key] = empty
        return frame

    def _guarded(self, work) -> dict:
        """Run a route, turning a failure into an answer the page can show.

        An exception that escapes a handler closes the socket without a word, and
        the page can only say the service stopped answering.
        """
        try:
            return work()
        except Exception as exc:  # noqa: BLE001 - any failure is reported, none is fatal
            message = f"Something went wrong in the local service: {type(exc).__name__}: {exc}"
            return {"ok": False, "count": 0, "raised": [], "message": message,
                    "error": message, "sessions": [], "text": ""}

    def _json(self, payload: dict, status: int = 200) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _read_json(self) -> dict:
        """Read the body, always draining it so a kept-alive connection stays in sync."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.close_connection = True
            return {}
        if length <= 0:
            return {}

        remaining, body = length, b""
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 64 * 1024))
            if not chunk:
                self.close_connection = True
                break
            remaining -= len(chunk)
            if len(body) < MAX_BODY_BYTES:
                body += chunk
        if length > MAX_BODY_BYTES:
            return {}
        try:
            return json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    # -- routes ----------------------------------------------------------- #

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if not self._host_is_ours():
            self._send(403, b"forbidden", "text/plain; charset=utf-8")
            return
        if not self._authorised(query):
            self._send(403, b"forbidden", "text/plain; charset=utf-8")
            return

        self.backend.touch()
        if parsed.path in {"/", "/index.html"}:
            self._serve_file(UI_DIR / "index.html")
            return
        if parsed.path == "/api/sessions":
            days = _as_float(query.get("days", ["7"])[0], 7.0)
            which = (query.get("agent") or [""])[0][:40]
            result = self._guarded(lambda: self.backend.sessions(days=days, which=which))
            if "agents" not in result:
                result.update(self._frame(which))
            self._json(result)
            return
        if parsed.path == "/api/ping":
            self._json({"ok": True})
            return
        if parsed.path.startswith("/ui/"):
            self._serve_file(UI_DIR / parsed.path[4:])
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if not self._host_is_ours() or not self._authorised(query):
            self._send(403, b"forbidden", "text/plain; charset=utf-8")
            return

        self.backend.touch()
        payload = self._read_json()
        # A body that is not an object, or ids that are not a list, is a caller
        # out of step with this service; it gets an answer, not a dropped socket.
        if not isinstance(payload, dict):
            payload = {}
        offered = payload.get("ids")
        # An id named twice would be launched twice on one transcript.
        every = list(dict.fromkeys(str(i) for i in offered)) if isinstance(offered, list) else []
        ids = every[:MAX_IDS]
        days = _as_float(payload.get("days", 7), 7.0)
        which = str(payload.get("agent", ""))[:40]

        layout = str(payload.get("layout", ""))[:16]
        if parsed.path == "/api/revive":
            # Where and when are the page's wish, never a condition: a room or a
            # timing that does not read is ignored and the sessions still open.
            result = self._guarded(lambda: self.backend.revive(
                ids, days=days, which=which, layout=layout, room=payload.get("room"),
                wait_ms=_as_float(payload.get("wait"), 0.0, low=0.0, high=MAX_WAIT_MS),
                stagger_ms=_as_float(payload.get("stagger"), 0.0, low=0.0, high=MAX_STAGGER_MS),
            ))
            if len(every) > MAX_IDS:
                result["message"] = (f"{result.get('message', '')} Only the first {MAX_IDS} were sent; "
                                     "the rest are still marked.").strip()
            self._json(result)
        elif parsed.path == "/api/commands":
            result = self._guarded(lambda: self.backend.commands(ids, days=days, which=which))
            result["dropped"] = len(every) - len(ids)
            self._json(result)
        elif parsed.path == "/api/place":
            result = self._guarded(lambda: self.backend.place(layout, payload.get("room")))
            result.setdefault("spots", [])
            self._json(result)
        elif parsed.path == "/api/reveal":
            self._json(self.backend.reveal(str(payload.get("path", ""))))
        elif parsed.path == "/api/quit":
            self._json({"ok": True})
            self.backend.request_stop()
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def _serve_file(self, path: Path) -> None:
        try:
            resolved = path.resolve()
            resolved.relative_to(UI_DIR.resolve())  # no traversal outside ui/
            body = resolved.read_bytes()
        except (OSError, ValueError):
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        self._send(200, body, _CONTENT_TYPES.get(resolved.suffix.lower(), "application/octet-stream"))


def _as_float(value: object, fallback: float, *, low: float = 0.04, high: float = 3650.0) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    # NaN slips through min and max unchanged and then fails inside timedelta.
    if not math.isfinite(number):
        return fallback
    return min(max(number, low), high)


def serve(backend: Backend) -> tuple[ThreadingHTTPServer, str]:
    """Start the local server and return it with the URL the window should open."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, backend=backend))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    return server, f"http://127.0.0.1:{port}/?t={backend.token}"


def _browser_binary() -> str | None:
    """A Chromium that can host a chromeless window, when pywebview is missing."""
    named = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
             "chrome", "microsoft-edge", "msedge", "brave-browser"]
    paths = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        str(Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    ]
    for name in named:
        found = shutil.which(name)
        if found:
            return found
    return next((p for p in paths if Path(p).exists()), None)


def run_gui(*, agent: Agent = CLAUDE_CODE, root: str | None = None) -> int:
    """Open the desktop app. Returns a process exit code."""
    if not (UI_DIR / "index.html").is_file():
        print(
            f"The desktop UI is missing (looked in {UI_DIR}).\n"
            "Run it from a clone of the repository, or reinstall the package.",
            file=sys.stderr,
        )
        return 1

    backend = Backend(agent=agent, root=root)
    server, url = serve(backend)

    try:
        import webview  # type: ignore
    except ImportError:
        webview = None

    if webview is not None:
        window = webview.create_window(
            "Revenant",
            url,
            width=1000,
            height=720,
            min_size=(760, 560),
            background_color="#1f1e1d",
            frameless=True,
            easy_drag=False,
        )
        window.events.closed += backend.request_stop

        def _bind(win) -> None:
            # Expose window controls to the custom titlebar.
            win.expose(win.destroy, win.minimize, win.toggle_fullscreen)

        try:
            webview.start(_bind, window)
        finally:
            server.shutdown()
        return 0

    binary = _browser_binary()
    if binary:
        profile = revenant.state_dir() / "browser-profile"
        profile.mkdir(parents=True, exist_ok=True)
        process = subprocess.Popen(
            [
                binary,
                f"--app={url}",
                f"--user-data-dir={profile}",
                "--window-size=1000,760",
                "--no-first-run",
                "--no-default-browser-check",
            ]
        )
        try:
            backend.wait_until_idle()
        except KeyboardInterrupt:
            pass
        finally:
            server.shutdown()
            if process.poll() is None:
                process.terminate()
        return 0

    webbrowser.open(url)
    print(f"Revenant is running at {url}\nPress Ctrl+C to stop.")
    try:
        backend.wait_until_idle()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(run_gui())
