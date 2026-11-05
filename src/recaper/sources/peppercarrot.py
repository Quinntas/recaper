"""Pepper&Carrot connector - David Revoy's open webcomic, CC-BY 4.0 (free to adapt, credit required).

Uses the site's public JSON (episodes.json / episodes-v1.json / langs.json). 39+ episodes in 70+ languages.
"""

from __future__ import annotations

import re
import time
from functools import cached_property
from urllib.parse import urlparse

import httpx

from .. import __version__
from .base import ChapterInfo, Connector, MangaInfo, Page

BASE = "https://www.peppercarrot.com/0_sources"
SERIES_ID = "pepper-carrot"
ATTRIBUTION = "Pepper&Carrot by David Revoy - www.peppercarrot.com - licensed CC-BY 4.0 (translations by the Pepper&Carrot community)"


class PepperCarrot(Connector):
    id = "peppercarrot"
    name = "Pepper&Carrot"
    description = "Open-source webcomic by David Revoy, CC-BY 4.0: free to adapt into videos with credit. 70+ languages."
    sorts = ["relevance"]
    availability = ["any", "downloadable", "free"]
    supports_nsfw = False
    licence_info = "Every episode is published by the author under Creative Commons Attribution 4.0 - credit is required."

    def __init__(self) -> None:
        self.client = httpx.Client(
            headers={"User-Agent": f"recaper/{__version__} (personal recap tool)"},
            timeout=httpx.Timeout(60.0, connect=15.0),
            follow_redirects=True,
        )

    @cached_property
    def _langs(self) -> dict:
        try:
            return self.client.get(f"{BASE}/langs.json").json()
        except Exception:  # noqa: BLE001
            return {"en": {"name": "English", "local_name": "English"}, "pt": {"name": "Portuguese (Brazil)", "local_name": "Português"}}

    @property
    def languages(self) -> list[tuple[str, str]]:  # type: ignore[override]
        order = ["en", "pt", "es", "fr", "de", "it", "ja", "kr", "cn"]
        items = sorted(self._langs.items(), key=lambda kv: (order.index(kv[0]) if kv[0] in order else 99, kv[1].get("name", "")))
        return [(code, f"{v.get('local_name') or v.get('name')} ({v.get('name')})") for code, v in items]

    def _episodes(self) -> tuple[list[dict], dict]:
        cached = getattr(self, "_eps_cache", None)
        if cached and time.time() - cached[0] < 600:
            return cached[1]
        eps = self.client.get(f"{BASE}/episodes.json").json()
        v1 = {e["name"]: e for e in self.client.get(f"{BASE}/episodes-v1.json").json()}
        self._eps_cache = (time.time(), (eps, v1))
        return eps, v1

    def _series(self) -> MangaInfo:
        return MangaInfo(
            id=SERIES_ID,
            title="Pepper&Carrot",
            alt_titles=["by David Revoy"],
            description=("Pepper, a young witch, and her cat Carrot live in the magical world of Hereva. Pepper "
                         "studies chaosah magic with her three godmothers and dreams of becoming a great witch. "
                         "Free and open-source webcomic, funded by its readers, published under CC-BY 4.0."),  # fmt: skip
            cover_url=f"{BASE}/ep01_Potion-of-Flight/low-res/en_Pepper-and-Carrot_by-David-Revoy_E01.jpg",
            year=2014,
            status="ongoing",
            tags=["Fantasy", "Comedy", "Magic", "Full Color", "Webcomic", "Open licence"],
            languages=list(self._langs),
            original_language="fr",
            content_rating="safe",
            url="https://www.peppercarrot.com",
            license="CC-BY 4.0",
            license_verified=True,
            attribution=ATTRIBUTION,
        )

    # --------------------------------------------------------------- connector
    def search(self, query: str, lang: str | None = None, limit: int = 24, offset: int = 0,
               sort: str = "relevance", availability: str = "any", nsfw: bool = False) -> list[MangaInfo]:  # fmt: skip
        q = (query or "").lower()
        if offset or (q and not any(w in q for w in ("pepper", "carrot", "revoy", "hereva", "witch"))):
            return []
        return [self._series()]

    def manga(self, manga_id: str) -> MangaInfo:
        if manga_id != SERIES_ID:
            raise KeyError(manga_id)
        return self._series()

    def chapters(self, manga_id: str, lang: str) -> list[ChapterInfo]:
        eps, v1 = self._episodes()
        out = []
        for i, ep in enumerate(eps, start=1):
            if lang not in ep.get("translated_languages", []):
                continue
            pages = [k for k in v1.get(ep["name"], {}).get("pages", {}) if k.isdigit()]
            title = re.sub(r"^ep\d+_", "", ep["name"]).replace("-", " ")
            out.append(ChapterInfo(id=f"{lang}:{ep['name']}", number=str(i), volume=None, title=title,
                                   pages=len(pages), lang=lang, group="David Revoy"))  # fmt: skip
        return out

    def pages(self, chapter_id: str, quality: str = "data") -> list[Page]:
        lang, _, ep = chapter_id.partition(":")
        _, v1 = self._episodes()
        files = v1[ep]["pages"]
        res = "low-res" if quality == "data-saver" else "hi-res"
        nums = sorted((k for k in files if k.isdigit()), key=int)
        return [Page(url=f"{BASE}/{ep}/{res}/{lang}_{files[k]}", filename=f"{int(k):03d}.jpg") for k in nums]

    def fetch_image(self, page: Page) -> bytes:
        r = self.client.get(page.url)
        r.raise_for_status()
        return r.content

    def fetch_cover(self, url: str) -> tuple[bytes, str]:
        if urlparse(url).netloc != "www.peppercarrot.com":
            raise ValueError("not a peppercarrot.com url")
        r = self.client.get(url)
        r.raise_for_status()
        return r.content, r.headers.get("content-type", "image/jpeg")
