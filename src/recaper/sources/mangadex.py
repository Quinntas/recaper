"""MangaDex connector (official public API: https://api.mangadex.org/docs).

Follows the API rules: honest User-Agent, ~4 req/s on the API, the at-home endpoint is
throttled to 40/min, images come from the MangaDex@Home node returned per chapter and each
fetch from a non-mangadex.org node is reported back for network health. Chapters that only
link to an official publisher (externalUrl) have no pages here and are marked as such.
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import httpx

from .. import __version__
from .base import ChapterInfo, Connector, MangaInfo, Page, RateLimiter

API = "https://api.mangadex.org"
UPLOADS = "https://uploads.mangadex.org"
REPORT = "https://api.mangadex.network/report"
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)

LANGUAGES = [
    ("en", "English"),
    ("pt-br", "Português (Brasil)"),
    ("es-la", "Español (LatAm)"),
    ("es", "Español"),
    ("fr", "Français"),
    ("it", "Italiano"),
    ("de", "Deutsch"),
    ("ru", "Русский"),
    ("id", "Bahasa Indonesia"),
    ("vi", "Tiếng Việt"),
    ("tr", "Türkçe"),
    ("pl", "Polski"),
    ("ja", "日本語"),
    ("ko", "한국어"),
    ("zh", "中文"),
]


class MangaDex(Connector):
    id = "mangadex"
    name = "MangaDex"
    description = "Official MangaDex API. Licensed series often only link to official sites (not downloadable)."
    languages = LANGUAGES
    sorts = ["relevance", "popular", "rating", "latest", "newest", "title"]
    availability = ["any", "downloadable"]
    supports_nsfw = True
    licence_info = "MangaDex has no licence data: scanlations are not copyright-free, so the free filter isn't offered here."

    def __init__(self) -> None:
        self.client = httpx.Client(
            headers={"User-Agent": f"recaper/{__version__} (personal recap tool; +https://github.com/)"},
            timeout=httpx.Timeout(30.0, connect=15.0),
            follow_redirects=True,
        )
        self.api_limit = RateLimiter(4.0)
        self.athome_limit = RateLimiter(0.6)  # 40 per minute
        # health reports are fire-and-forget so they don't add latency to every image
        self._reporter = ThreadPoolExecutor(2, thread_name_prefix="md-report")

    # ------------------------------------------------------------------ api
    def _get(self, path: str, params=None, *, athome: bool = False) -> dict:
        for attempt in range(5):
            (self.athome_limit if athome else self.api_limit).wait()
            r = self.client.get(API + path, params=params)
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", 5 * (attempt + 1))))
                continue
            if r.status_code >= 500:
                time.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()
        return r.json()

    @staticmethod
    def _pick(d: dict | None, lang: str | None = None) -> str:
        if not d:
            return ""
        for key in (lang, "en", "ja-ro", "ko-ro", "zh-ro"):
            if key and d.get(key):
                return d[key]
        return next(iter(d.values()), "")

    def _to_manga(self, item: dict, lang: str | None = None) -> MangaInfo:
        a = item["attributes"]
        cover = None
        for rel in item.get("relationships", []):
            if rel["type"] == "cover_art" and rel.get("attributes", {}).get("fileName"):
                cover = f"{UPLOADS}/covers/{item['id']}/{rel['attributes']['fileName']}.256.jpg"
        alts = [self._pick(t) for t in a.get("altTitles", [])][:6]
        return MangaInfo(
            id=item["id"],
            title=self._pick(a.get("title"), lang) or (alts[0] if alts else item["id"]),
            alt_titles=[t for t in alts if t],
            description=self._pick(a.get("description"), lang)[:1200],
            cover_url=cover,
            year=a.get("year"),
            status=a.get("status"),
            tags=[self._pick(t["attributes"]["name"]) for t in a.get("tags", [])][:12],
            languages=a.get("availableTranslatedLanguages") or [],
            original_language=a.get("originalLanguage"),
            content_rating=a.get("contentRating"),
            url=f"https://mangadex.org/title/{item['id']}",
        )

    # ------------------------------------------------------------ connector
    ORDER = {
        "relevance": ("relevance", "desc"),
        "popular": ("followedCount", "desc"),
        "rating": ("rating", "desc"),
        "latest": ("latestUploadedChapter", "desc"),
        "newest": ("createdAt", "desc"),
        "title": ("title", "asc"),
    }

    def search(
        self,
        query: str,
        lang: str | None = None,
        limit: int = 24,
        offset: int = 0,
        sort: str = "relevance",
        availability: str = "any",
        nsfw: bool = False,
    ) -> list[MangaInfo]:
        m = UUID.search(query or "")
        if m:  # pasted a MangaDex URL / id
            return [self.manga(m.group(0))]
        if sort == "relevance" and not (query or "").strip():
            sort = "popular"  # relevance needs a query
        field, direction = self.ORDER.get(sort, self.ORDER["relevance"])
        params: list[tuple[str, str | int]] = [
            ("limit", limit),
            ("offset", offset),
            ("includes[]", "cover_art"),
            (f"order[{field}]", direction),
        ]
        if (query or "").strip():
            params.append(("title", query.strip()))
        ratings = ("safe", "suggestive", "erotica", "pornographic") if nsfw else ("safe", "suggestive")
        params += [("contentRating[]", r) for r in ratings]
        if lang:
            params.append(("availableTranslatedLanguage[]", lang))
        if availability in ("downloadable", "free"):
            params.append(("hasAvailableChapters", "true"))
        data = self._get("/manga", params)
        results = [self._to_manga(x, lang) for x in data.get("data", [])]
        if availability in ("downloadable", "free"):
            # hasAvailableChapters also counts official external links (e.g. licensed series), so verify
            # each result really has chapters hosted on MangaDex in this language.
            with ThreadPoolExecutor(4) as pool:
                keep = list(pool.map(lambda m: self._has_hosted_chapters(m.id, lang), results))
            results = [m for m, ok in zip(results, keep) if ok]
        return results

    def _has_hosted_chapters(self, manga_id: str, lang: str | None) -> bool:
        params: list[tuple[str, str | int]] = [("limit", 1), ("includeExternalUrl", 0)]
        params += [("contentRating[]", r) for r in ("safe", "suggestive", "erotica", "pornographic")]
        if lang:
            params.append(("translatedLanguage[]", lang))
        try:
            return self._get(f"/manga/{manga_id}/feed", params).get("total", 0) > 0
        except Exception:  # noqa: BLE001 - keep the result if we can't tell
            return True

    def manga(self, manga_id: str) -> MangaInfo:
        data = self._get(f"/manga/{manga_id}", [("includes[]", "cover_art")])
        return self._to_manga(data["data"])

    def chapters(self, manga_id: str, lang: str) -> list[ChapterInfo]:
        out: list[ChapterInfo] = []
        offset = 0
        while True:
            params: list[tuple[str, str | int]] = [
                ("translatedLanguage[]", lang),
                ("order[volume]", "asc"),
                ("order[chapter]", "asc"),
                ("limit", 500),
                ("offset", offset),
                ("includes[]", "scanlation_group"),
            ]
            params += [("contentRating[]", r) for r in ("safe", "suggestive", "erotica", "pornographic")]
            data = self._get(f"/manga/{manga_id}/feed", params)
            for c in data.get("data", []):
                a = c["attributes"]
                if a.get("isUnavailable"):
                    continue
                groups = [
                    r.get("attributes", {}).get("name", "")
                    for r in c.get("relationships", [])
                    if r["type"] == "scanlation_group"
                ]
                out.append(
                    ChapterInfo(
                        id=c["id"],
                        number=a.get("chapter"),
                        volume=a.get("volume"),
                        title=a.get("title") or "",
                        pages=int(a.get("pages") or 0),
                        lang=a.get("translatedLanguage") or lang,
                        group=", ".join(g for g in groups if g) or "No group",
                        external_url=a.get("externalUrl"),
                        published=a.get("publishAt"),
                    )
                )
            offset += 500
            if offset >= data.get("total", 0):
                break

        def key(ch: ChapterInfo):
            try:
                return (0, float(ch.number)) if ch.number else (1, 0.0)
            except ValueError:
                return (1, 0.0)

        out.sort(key=key)
        return out

    def pages(self, chapter_id: str, quality: str = "data") -> list[Page]:
        data = self._get(f"/at-home/server/{chapter_id}", athome=True)
        base, ch = data["baseUrl"], data["chapter"]
        files = ch["dataSaver"] if quality == "data-saver" else ch["data"]
        folder = "data-saver" if quality == "data-saver" else "data"
        report = "mangadex.org" not in urlparse(base).netloc
        return [
            Page(url=f"{base}/{folder}/{ch['hash']}/{name}", filename=f"{i:03d}{_ext(name)}", report=report)
            for i, name in enumerate(files, start=1)
        ]

    def fetch_image(self, page: Page) -> bytes:
        last: Exception | None = None
        for attempt in range(4):
            t0 = time.monotonic()
            try:
                # short read timeout: a stalled @Home node is retried instead of waited out
                r = self.client.get(page.url, timeout=httpx.Timeout(15.0, connect=8.0))
                ok = r.status_code == 200 and len(r.content) > 0
                if page.report:
                    self._report(page.url, ok, r.headers.get("X-Cache", "").startswith("HIT"), len(r.content), t0)
                if ok:
                    return r.content
                last = RuntimeError(f"HTTP {r.status_code}")
            except httpx.HTTPError as e:
                if page.report:
                    self._report(page.url, False, False, 0, t0)
                last = e
            time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"failed to fetch {page.url}: {last}")

    def fetch_cover(self, url: str) -> tuple[bytes, str]:
        if urlparse(url).netloc != "uploads.mangadex.org":
            raise ValueError("not a MangaDex cover url")
        r = self.client.get(url)
        r.raise_for_status()
        return r.content, r.headers.get("content-type", "image/jpeg")

    def _report(self, url: str, success: bool, cached: bool, size: int, t0: float) -> None:
        duration = int((time.monotonic() - t0) * 1000)
        self._reporter.submit(self._send_report, url, success, cached, size, duration)

    def _send_report(self, url: str, success: bool, cached: bool, size: int, duration: int) -> None:
        try:
            self.client.post(
                REPORT,
                json={"url": url, "success": success, "cached": cached, "bytes": size, "duration": duration},
                timeout=5,
            )
        except httpx.HTTPError:
            pass  # reporting is best-effort


def _ext(name: str) -> str:
    m = re.search(r"\.(jpe?g|png|webp|gif)$", name, re.I)
    return f".{m.group(1).lower()}" if m else ".jpg"
