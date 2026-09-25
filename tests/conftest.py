"""Keep the suite from opening anything on the machine that runs it.

A test that forgot to stub the launcher once started real consoles running
`claude --resume`. Every process now goes through a check: Python, node and ps
may start, anything else fails the test that asked for it.
"""

from __future__ import annotations

import inspect
import os
import re
import subprocess
import sys
import webbrowser
from pathlib import Path

import pytest

ALLOWED = {"node", "node.exe", "ps", Path(sys.executable).name.lower()}
_PROGRAM = re.compile(r'\s*(?:"([^"]+)"|(\S+))')


def _program(args, executable) -> str:
    target = executable
    if not target:
        if isinstance(args, (str, bytes, os.PathLike)):
            text = os.fsdecode(args)
            found = _PROGRAM.match(text)
            target = (found.group(1) or found.group(2)) if found else text
        else:
            target = next(iter(args), "")
    return Path(os.fsdecode(target)).name.lower()


def _allowed(name: str) -> bool:
    return name in ALLOWED or name.startswith("python")


class Blocked(RuntimeError):
    """Not an OSError, so the launcher's own error handling cannot swallow it."""


@pytest.fixture(autouse=True)
def nothing_opens(monkeypatch: pytest.MonkeyPatch):
    attempts: list[str] = []
    original = subprocess.Popen._execute_child
    shape = inspect.signature(original)

    def guarded(self, *args, **kwargs):
        bound = shape.bind(self, *args, **kwargs).arguments
        name = _program(bound["args"], bound.get("executable"))
        if bound.get("shell") or not _allowed(name):
            attempts.append(f"{name}: {bound['args']!r}")
            raise Blocked(f"a test tried to start {name!r}; stub the launcher instead")
        return original(self, *args, **kwargs)

    def refuse(what: str):
        def stand_in(target, *args, **kwargs):
            attempts.append(f"{what}: {target!r}")
            raise Blocked(f"a test tried to {what} {target!r}")
        return stand_in

    monkeypatch.setattr(subprocess.Popen, "_execute_child", guarded)
    monkeypatch.setattr(os, "system", refuse("run a shell command"), raising=False)
    monkeypatch.setattr(webbrowser, "open", refuse("open a browser on"))
    if hasattr(os, "startfile"):
        monkeypatch.setattr(os, "startfile", refuse("open"))
    yield attempts
    if attempts:
        pytest.fail("tried to start outside programs:\n" + "\n".join(attempts))
