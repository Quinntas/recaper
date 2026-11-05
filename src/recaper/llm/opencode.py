"""Models served through the OpenCode CLI, e.g. the free `opencode/deepseek-v4.1-flash-free`.

OpenCode's free tier only works from inside OpenCode, so we drive `opencode run` headlessly:
* inline config (OPENCODE_CONFIG_CONTENT) defining a tool-less agent with deny-all
  permissions, run from a throwaway dir, so the model can't run commands or touch files;
* the prompt is attached as a file (Windows caps argv at ~32k chars).
The free endpoint is text-only, so this provider can write scripts but not read panels.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Sequence

from .base import Part, Provider, ProviderError, QuotaError, UnavailableError, text_only

AGENT = "recaper"
_CONFIG = {
    "$schema": "https://opencode.ai/config.json",
    "agents": {
        AGENT: {
            "description": "Tool-less text generator used by recaper",
            "mode": "primary",
            "system": "You are a text generator with no tools. Output only what the request asks for.",
            "permissions": [{"action": "*", "resource": "*", "effect": "deny"}],
        }
    },
}
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_QUOTA_MARKERS = ("free usage limit", "usage limit reached", "rate limit", "freetiererror", "quota")


class OpencodeProvider(Provider):
    name = "opencode"
    vision = False

    def __init__(self, model: str | None):
        if model and "/" not in model:
            model = f"opencode/{model}"
        super().__init__(model or "opencode/deepseek-v4.1-flash-free")

    def check(self) -> None:
        if not shutil.which("opencode"):
            raise UnavailableError("`opencode` CLI not found (bun i -g opencode-ai)")

    def complete(self, system: str, parts: Sequence[Part]) -> str:
        self.check()
        workdir = Path(tempfile.mkdtemp(prefix="recaper-opencode-"))
        # Project-level opencode.json isn't reliably picked up by `run` in fresh dirs;
        # inline config via env is.
        env = {**os.environ, "OPENCODE_CONFIG_CONTENT": json.dumps(_CONFIG)}
        try:
            request = workdir / "request.md"
            request.write_text(f"# Instructions\n\n{system}\n\n# Input\n\n{text_only(parts)}\n", "utf-8")
            cmd = [
                shutil.which("opencode") or "opencode",
                "run",
                "--standalone",
                "--agent",
                AGENT,
                "-m",
                self.model,
                "--format",
                "json",
                "-f",
                str(request),
                "The attached file request.md contains your complete instructions and input. "
                "Follow it exactly and reply with only the requested output.",
            ]
            try:
                proc = subprocess.run(cmd, cwd=workdir, env=env, capture_output=True, text=True, encoding="utf-8", timeout=900)
            except subprocess.TimeoutExpired as e:
                raise ProviderError("opencode timed out") from e
            out, err = proc.stdout, proc.stderr
            blob = _ANSI.sub("", out + "\n" + err).lower()
            if any(m in blob for m in _QUOTA_MARKERS):
                msg = "OpenCode free usage limit reached"
                if m := re.search(r'"retryAfterMs":\s*(\d+)', out + err):
                    msg += f" (resets in ~{int(m.group(1)) / 3_600_000:.1f}h)"
                raise QuotaError(msg)
            if proc.returncode != 0:
                raise ProviderError(f"opencode exit {proc.returncode}: {_ANSI.sub('', err or out).strip()[-400:]}")
            text = _text_from_events(out)
            if not text.strip():
                raise ProviderError("opencode returned no text")
            return text
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


def _text_from_events(stdout: str) -> str:
    """`--format json` prints one JSON event per line; collect assistant text parts.

    Text parts can be re-emitted as they grow, so keep the latest version per part id."""
    parts: dict[str, str] = {}
    order: list[str] = []
    plain: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            plain.append(_ANSI.sub("", line))
            continue
        for pid, text in _find_text_parts(event):
            if pid not in parts:
                order.append(pid)
            parts[pid] = text
    if parts:
        return "\n".join(parts[p] for p in order)
    return "\n".join(l for l in plain if not l.startswith(">"))


def _find_text_parts(node: Any, _counter: list[int] | None = None):
    counter = _counter if _counter is not None else [0]
    if isinstance(node, dict):
        if node.get("type") == "text" and isinstance(node.get("text"), str) and not node.get("synthetic"):
            counter[0] += 1
            yield str(node.get("id") or f"anon{counter[0]}"), node["text"]
            return
        for v in node.values():
            yield from _find_text_parts(v, counter)
    elif isinstance(node, list):
        for v in node:
            yield from _find_text_parts(v, counter)
