"""Tiny .env loader (no extra dependency).

Looks for `.env` in the current directory, the recaper repo root and ~/.recaper.env. Variables already set in
the real environment always win, so CI / shell exports override the file. Values are never printed.
"""

from __future__ import annotations

import os
from pathlib import Path

_LOADED = False


def load_env() -> list[str]:
    """Load once; returns the names (not values) of variables that were set."""
    global _LOADED
    if _LOADED:
        return []
    _LOADED = True
    candidates = [Path.cwd() / ".env", Path(__file__).resolve().parents[2] / ".env", Path.home() / ".recaper.env"]
    names: list[str] = []
    for path in dict.fromkeys(candidates):
        if not path.is_file():
            continue
        for line in path.read_text("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip().removeprefix("export ").strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
                names.append(key)
    return names
