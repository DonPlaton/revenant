#!/usr/bin/env python3
"""Build a synthetic agent-config directory for the README images.

The demo GIF must not show anyone's real directories or prompts, so the recorder
runs against this fixture instead of `~/.claude`. The shapes are exactly the ones
Revenant reads: `projects/<slug>/<uuid>.jsonl`, `history.jsonl`, `sessions/<pid>.json`.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

#: (directory, prompt, hours ago, turns). Ordered so each slider stop reveals more.
SESSIONS: list[tuple[str, str, float, int]] = [
    (r"C:\dev\payments-api", "the retry path double-charges on a gateway timeout, find where", 0.3, 24),
    (r"C:\dev\checkout-web", "port the checkout form to the new design tokens", 1.5, 12),
    (r"C:\dev\ml-pipeline", "why does the training loop OOM at batch 64 but not 32?", 4.0, 31),
    (r"C:\dev\infra", "add a readiness probe to the ingress and roll it out to staging", 9.0, 8),
    (r"C:\dev\docs-site", "rewrite the quickstart so it works on a clean machine", 20.0, 15),
    (r"C:\dev\mobile-app", "the list stutters on scroll after the last release, profile it", 30.0, 19),
    (r"C:\dev\payments-api", "backfill the reconciliation job for August", 46.0, 6),
    (r"C:\dev\analytics", "the weekly report double-counts refunds", 60.0, 11),
    (r"C:\dev\scraper", "rate limiting broke when they moved to cursor pagination", 96.0, 22),
    (r"C:\dev\checkout-web", "make the error states match the spec", 130.0, 9),
    (r"C:\dev\ml-pipeline", "swap the tokenizer and re-run the ablation", 200.0, 27),
    (r"C:\dev\infra", "cut the nightly build from 14 minutes to under 5", 280.0, 17),
    (r"C:\dev\docs-site", "add an API reference generated from the OpenAPI spec", 400.0, 13),
    (r"C:\dev\analytics", "move the dashboard queries off the primary", 600.0, 21),
    (r"C:\dev\scraper", "retire the old parser once the new one matches on 1k pages", 900.0, 14),
    (r"C:\dev\mobile-app", "wire up the deep links for the campaign", 1300.0, 7),
    (r"C:\dev\payments-api", "audit every place we assume the currency is USD", 1700.0, 26),
]

#: Sessions that are "still running" in the screenshots - they render locked.
LIVE = [
    (r"C:\dev\payments-api", "payments-api-7c", "review the diff before I push"),
    (r"C:\dev\ml-pipeline", "ml-pipeline-1a", "keep going"),
]


def _slug(path: str) -> str:
    return path.replace(":", "-").replace("\\", "-")


def build(root: Path, live_pids: list[int] | None = None) -> Path:
    """Populate `root` with a believable config directory and return it.

    `live_pids` must be pids of processes that are genuinely running, otherwise the
    rows render as revivable rather than locked.
    """
    now = datetime.now(timezone.utc)
    (root / "projects").mkdir(parents=True, exist_ok=True)
    (root / "sessions").mkdir(parents=True, exist_ok=True)
    history: list[str] = []

    def add(cwd: str, prompt: str, hours: float, turns: int, *, live_pid: int | None = None,
            live_name: str | None = None) -> None:
        session_id = str(uuid.uuid4())
        moment = now - timedelta(hours=hours)
        directory = root / "projects" / _slug(cwd)
        directory.mkdir(parents=True, exist_ok=True)
        transcript = directory / f"{session_id}.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "type": "user",
                    "cwd": cwd,
                    "version": "2.1.257",
                    "gitBranch": "main",
                    "timestamp": moment.isoformat().replace("+00:00", "Z"),
                    "sessionId": session_id,
                    "message": {"role": "user", "content": prompt},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        stamp = moment.timestamp()
        os.utime(transcript, (stamp, stamp))

        for index in range(turns):
            step = moment - timedelta(minutes=(turns - index) * 3)
            text = prompt if index == turns - 1 else f"step {index + 1}"
            history.append(
                json.dumps(
                    {
                        "display": text,
                        "pastedContents": {},
                        "timestamp": str(int(step.timestamp() * 1000)),
                        "project": cwd,
                        "sessionId": session_id,
                    },
                    ensure_ascii=False,
                )
            )

        if live_pid is not None:
            (root / "sessions" / f"{live_pid}.json").write_text(
                json.dumps(
                    {
                        "pid": live_pid,
                        "sessionId": session_id,
                        "cwd": cwd,
                        "name": live_name,
                        "status": "busy",
                        "startedAt": int(moment.timestamp() * 1000),
                    }
                ),
                encoding="utf-8",
            )

    for cwd, prompt, hours, turns in SESSIONS:
        add(cwd, prompt, hours, turns)

    # Live rows need pids that are genuinely alive; the recorder widens the agent's
    # image allowlist so those processes count.
    for offset, ((cwd, name, prompt), pid) in enumerate(zip(LIVE, live_pids or [os.getpid()])):
        add(cwd, prompt, 0.02 + offset * 0.01, 5 + offset * 7, live_pid=pid, live_name=name)

    (root / "history.jsonl").write_text("\n".join(history) + "\n", encoding="utf-8")
    return root


if __name__ == "__main__":
    import sys

    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("demo-config")
    print(build(target))
