"""Source connectors: each site/API that chapters can be downloaded from is one connector.

Every connector supports the same filter model so the UI can offer one set of controls:
  sort          relevance | popular | rating | latest | newest | title   (each connector lists what it supports)
  availability  any | downloadable | free                                 ("free" = copyright-free, only where
                                                                           the source has trustworthy data)
  nsfw          include adult (erotica / pornographic) works            (never blurred)
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

SORT_LABELS = {
    "relevance": "Best match",
    "popular": "Most popular",
    "rating": "Top rated",
    "latest": "Recently updated",
    "newest": "Newest",
    "title": "Title A-Z",
}
AVAILABILITY_LABELS = {
    "any": "Everything",
    "downloadable": "Downloadable here",
    "free": "Copyright-free (public domain / open licence)",
}


@dataclass
class MangaInfo:
    id: str
    title: str
    alt_titles: list[str] = field(default_factory=list)
    description: str = ""
    cover_url: str | None = None
    year: int | None = None
    status: str | None = None
    tags: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    original_language: str | None = None
    content_rating: str | None = None  # safe | suggestive | erotica | pornographic
    url: str | None = None
    license: str | None = None  # e.g. "CC-BY 4.0", "Public domain (published 1912)"
    license_verified: bool = False  # True only when the source vouches for it
    license_note: str | None = None  # e.g. uploader-claimed licence that we could not verify
    attribution: str | None = None  # credit line required by the licence
    popularity: int | None = None  # follows / downloads, for display

    @property
    def nsfw(self) -> bool:
        return self.content_rating in ("erotica", "pornographic")


@dataclass
class ChapterInfo:
    id: str
    number: str | None  # "12", "12.5", None for oneshots
    volume: str | None
    title: str
    pages: int
    lang: str
    group: str = ""
    external_url: str | None = None  # official site link: not downloadable here
    published: str | None = None
    kind: str = "images"  # images (page by page) | file (one .cbz/.zip/.pdf)
    size: int = 0  # bytes, when known (file chapters)

    @property
    def downloadable(self) -> bool:
        return not self.external_url and (self.pages > 0 or self.kind == "file")


@dataclass
class Page:
    url: str
    filename: str
    report: bool = False  # MangaDex@Home asks clients to report fetch health
    kind: str = "image"  # image | file


class RateLimiter:
    """Simple spacing limiter shared by threads."""

    def __init__(self, per_second: float):
        self.interval = 1.0 / per_second
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self.interval
        if delay > 0:
            time.sleep(delay)


class Connector(ABC):
    id: str = "base"
    name: str = "Base"
    description: str = ""
    languages: list[tuple[str, str]] = []  # (code, label); "" = any language
    sorts: list[str] = ["relevance"]
    availability: list[str] = ["any"]
    supports_nsfw: bool = False
    licence_info: str = ""  # how this source's "free" filter is decided (shown in the UI)

    @abstractmethod
    def search(
        self,
        query: str,
        lang: str | None = None,
        limit: int = 24,
        offset: int = 0,
        sort: str = "relevance",
        availability: str = "any",
        nsfw: bool = True,
    ) -> list[MangaInfo]: ...

    @abstractmethod
    def manga(self, manga_id: str) -> MangaInfo: ...

    @abstractmethod
    def chapters(self, manga_id: str, lang: str) -> list[ChapterInfo]: ...

    @abstractmethod
    def pages(self, chapter_id: str, quality: str = "data") -> list[Page]: ...

    def fetch_image(self, page: Page) -> bytes:  # pragma: no cover - overridden
        raise NotImplementedError

    def fetch_to_file(self, page: Page, dest, progress=None) -> None:
        """Whole-file chapters (cbz/pdf); default buffers through fetch_image."""
        dest.write_bytes(self.fetch_image(page))

    def fetch_cover(self, url: str) -> tuple[bytes, str]:  # pragma: no cover - overridden
        raise NotImplementedError

    def info(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "languages": self.languages,
            "sorts": [[s, SORT_LABELS[s]] for s in self.sorts],
            "availability": [[a, AVAILABILITY_LABELS[a]] for a in self.availability],
            "supports_nsfw": self.supports_nsfw,
            "licence_info": self.licence_info,
        }
