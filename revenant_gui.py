#!/usr/bin/env python3
"""Revenant desktop app.

One backend, three ways to show it:

1. a native window via `pywebview` (preferred - looks and behaves like an app),
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

import revenant
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
        self.token = secrets.token_urlsafe(24)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._cache: tuple[float, float, list[revenant.Session]] | None = None
        self.last_seen = time.monotonic()

    # -- data ------------------------------------------------------------- #

    def _scan(self, days: float) -> list[revenant.Session]:
        """Scan back `days`, reusing an identical scan taken moments ago."""
        now = time.monotonic()
        with self._lock:
            cached = self._cache
        if cached and cached[0] == days and now - cached[1] < self.CACHE_SECONDS:
            return cached[2]
        since = datetime.now(timezone.utc) - timedelta(days=max(days, 1 / 24))
        found = revenant.scan_sessions(self.root, since=since, agent=self.agent)
        with self._lock:
            self._cache = (days, time.monotonic(), found)
        return found

    def invalidate(self) -> None:
        with self._lock:
            self._cache = None

    def sessions(self, *, days: float, include_live: bool = True) -> dict:
        if not self.root.is_dir():
            return {
                "error": f"{self.agent.label} config directory not found: {self.root}",
                "sessions": [],
                "agent": self.agent.label,
            }
        found = self._scan(days)
        selected = revenant.filter_sessions(found, include_live=include_live, limit=None)
        return {
            "sessions": [revenant.session_to_dict(s) for s in selected],
            "agent": self.agent.label,
            "root": str(self.root),
            "error": None,
        }

    def _by_id(self, ids: list[str], *, days: float) -> list[revenant.Session]:
        if not ids:
            return []
        by_id = {s.session_id: s for s in self._scan(days)}
        return [by_id[i] for i in ids if i in by_id]

    # -- actions ---------------------------------------------------------- #

    def revive(self, ids: list[str], *, days: float) -> dict:
        chosen = self._by_id(ids, days=days)
        if not chosen:
            return {"ok": False, "message": "Those sessions are no longer on disk.", "count": 0}

        running = [s for s in chosen if s.is_live]
        chosen = [s for s in chosen if not s.is_live]
        if not chosen:
            return {
                "ok": False,
                "count": 0,
                "message": "Every selected session is still running - nothing to bring back.",
            }

        sink = io.StringIO()
        code = revenant.launch(chosen, stream=sink)
        self.invalidate()  # a revived session becomes live as soon as it registers
        note = sink.getvalue().strip().splitlines()
        message = note[-1] if note else ""
        if running:
            message += f" ({len(running)} still-running session(s) skipped)"
        return {"ok": code == 0, "count": len(chosen) if code == 0 else 0, "message": message}

    def commands(self, ids: list[str], *, days: float) -> dict:
        chosen = [s for s in self._by_id(ids, days=days)]
        return {"text": revenant.render_commands(chosen, shell="pwsh"), "count": len(chosen)}

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

        A window can go away in ways that never reach us - the OS close button, a
        browser crash, or a second launch handing the URL to an already running
        browser and exiting at once - so silence is the signal, not the lifetime of
        the process we spawned.
        """
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

        if parsed.path in {"/", "/index.html"}:
            self._serve_file(UI_DIR / "index.html")
            return
        self.backend.touch()
        if parsed.path == "/api/sessions":
            days = _as_float(query.get("days", ["7"])[0], 7.0)
            self._json(self.backend.sessions(days=days))
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
        ids = [str(i) for i in payload.get("ids", [])][:200]
        days = _as_float(payload.get("days", 7), 7.0)

        if parsed.path == "/api/revive":
            self._json(self.backend.revive(ids, days=days))
        elif parsed.path == "/api/commands":
            self._json(self.backend.commands(ids, days=days))
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


def _as_float(value: object, fallback: float) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    return min(max(number, 0.04), 3650.0)


def serve(backend: Backend) -> tuple[ThreadingHTTPServer, str]:
    """Start the local server and return it with the URL the window should open."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, backend=backend))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    return server, f"http://127.0.0.1:{port}/?t={backend.token}"


def _browser_binary() -> str | None:
    candidates = [
        shutil.which("chrome"),
        shutil.which("msedge"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    return next((c for c in candidates if c and Path(c).exists()), None)


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
