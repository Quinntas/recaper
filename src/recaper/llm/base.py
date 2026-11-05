from __future__ import annotations

import base64
import io
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence, Union

from PIL import Image

from ..log import console


class ProviderError(RuntimeError):
    """Recoverable failure: retry, then fall back to the next provider."""


class QuotaError(ProviderError):
    """Usage limit / rate cap hit: skip straight to the next provider for the rest of the run."""


# Phrases providers use when a plan / free-tier cap (not a transient rate limit) is hit.
QUOTA_MARKERS = (
    "usage limit",
    "limit reached",
    "hit your",  # Claude: "You've hit your 5-hour limit" / "weekly limit"
    "5-hour limit",
    "weekly limit",
    "free usage",
    "freetiererror",
    "quota",
    "resource_exhausted",
)


def is_quota_message(text: str) -> bool:
    t = text.lower()
    return any(m in t for m in QUOTA_MARKERS)


class UnavailableError(ProviderError):
    """Provider can't run here at all (missing CLI, key, package)."""


@dataclass
class ImagePart:
    path: Path
    max_edge: int = 1568  # Claude's native resolution ceiling; others are fine with it too

    _cache: bytes | None = None

    def jpeg_bytes(self) -> bytes:
        if self._cache is None:
            with Image.open(self.path) as im:
                im = im.convert("RGB")
                s = self.max_edge / max(im.size)
                if s < 1:
                    im = im.resize((round(im.width * s), round(im.height * s)), Image.LANCZOS)
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=88)
                self._cache = buf.getvalue()
        return self._cache

    def b64(self) -> str:
        return base64.b64encode(self.jpeg_bytes()).decode()


Part = Union[str, ImagePart]


class Provider:
    name = "base"
    vision = True

    def __init__(self, model: str | None):
        self.model = model

    def label(self) -> str:
        return f"{self.name}:{self.model}" if self.model else self.name

    def check(self) -> None:
        """Raise UnavailableError if this provider can't run on this machine."""

    def complete(self, system: str, parts: Sequence[Part]) -> str:
        raise NotImplementedError


def text_only(parts: Sequence[Part]) -> str:
    return "\n".join(p for p in parts if isinstance(p, str))


# ------------------------------------------------------------------ json utils


def extract_json(text: str) -> Any:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    candidates = [fence.group(1)] if fence else []
    candidates.append(text)
    for c in candidates:
        c = c.strip()
        try:
            return json.loads(c)
        except json.JSONDecodeError:
            pass
        for open_, close in (("{", "}"), ("[", "]")):
            a, b = c.find(open_), c.rfind(close)
            if a != -1 and b > a:
                try:
                    return json.loads(c[a : b + 1])
                except json.JSONDecodeError:
                    continue
    raise ProviderError("model did not return valid JSON")


# -------------------------------------------------------------- fallback chain


class Chain:
    """Try providers in order. Quota/unavailable errors skip a provider for the rest of the run."""

    def __init__(self, providers: list[Provider], role: str):
        if not providers:
            raise ValueError(f"no providers configured for {role}")
        self.providers = providers
        self.role = role
        self._dead: set[int] = set()

    def describe(self) -> str:
        return " -> ".join(p.label() for p in self.providers)

    def complete_json(self, system: str, parts: Sequence[Part], retries: int = 3) -> Any:
        last: Exception | None = None
        for idx, provider in enumerate(self.providers):
            if idx in self._dead:
                continue
            for attempt in range(retries + 1):
                try:
                    raw = provider.complete(system, parts)
                    return extract_json(raw)
                except (QuotaError, UnavailableError) as e:
                    console.print(f"[yellow]{self.role}: {provider.label()} unavailable ({e}); falling back[/]")
                    self._dead.add(idx)
                    last = e
                    break
                except Exception as e:  # noqa: BLE001 - any provider failure is retryable
                    if is_quota_message(str(e)):  # cap reported through a generic error
                        console.print(f"[yellow]{self.role}: {provider.label()} hit its usage cap ({e}); falling back[/]")
                        self._dead.add(idx)
                        last = QuotaError(str(e))
                        break
                    last = e
                    wait = 5 * 2**attempt  # 5, 10, 20s: rides out rate limits from parallel calls
                    console.print(f"[yellow]{self.role}: {provider.label()} failed ({e}); retry {attempt + 1}/{retries} in {wait}s[/]")
                    time.sleep(wait)
        if isinstance(last, QuotaError) and len(self._dead) == len(self.providers):
            raise QuotaError(
                f"every {self.role} provider is out of quota ({last}). Wait for the reset or add a fallback, "
                f"e.g. --{self.role} {self.describe().replace(' -> ', ',')},gemini. Finished work is cached; re-run to resume."
            )
        raise ProviderError(f"all {self.role} providers failed: {last}")
