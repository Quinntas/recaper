"""Machine-readable progress events for the web UI.

When RECAPER_PROGRESS=1 (set by the UI's job runner), stages print lines like
  @@progress {"stage": "read", "done": 3, "total": 5, "msg": "c0003"}
which the UI parses into progress bars. Otherwise this is a no-op.
"""

from __future__ import annotations

import json
import os
import sys
import threading

PREFIX = "@@progress "
_lock = threading.Lock()


def enabled() -> bool:
    return os.environ.get("RECAPER_PROGRESS") == "1"


def emit(stage: str, done: int | None = None, total: int | None = None, msg: str | None = None, **extra) -> None:
    if not enabled():
        return
    payload = {"stage": stage, "done": done, "total": total, "msg": msg, **extra}
    with _lock:
        sys.stdout.write(PREFIX + json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()
