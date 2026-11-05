"""Internet Archive connector (advancedsearch + metadata APIs).

Licence data on archive.org is supplied by uploaders and is often wrong (popular "public domain" comics
include Spider-Man and Berserk), and so are many uploaded dates. The *copyright-free* filter therefore
only returns items that were physically scanned by IA / partner libraries (scanningcenter set, dates from
library catalogues) and published before 1931 (public domain in the US). Everything else shows the
uploader's licence as an unverified claim.

A "chapter" here is one downloadable file of the item (PDF / CBZ / ZIP), or the item's page images.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx

from .. import __version__
from ..ingest import natural_key
from .base import ChapterInfo, Connector, MangaInfo, Page, RateLimiter

API = "https://archive.org"
PD_CUTOFF = 1930  # published <= 1930 => public domain in the US (as of 2026)
COMICS = (
    'mediatype:texts AND (collection:comics OR collection:manga_library OR subject:(manga OR manhwa OR manhua '
    'OR comic OR comics OR "comic books" OR "comic strips" OR "comic books, strips, etc" OR webcomic '
    'OR "graphic novel" OR cartoons OR caricatures))'
)
# copyright-free mode: comic subjects only (not every illustrated book), library scans, pre-1931
FREE_BASE = (
    'mediatype:texts AND subject:("comic books, strips, etc" OR "comic strips" OR "comic books" OR comics OR comic '
    'OR manga OR "graphic novels" OR cartoons)'
)
FREE = (
    f"date:[1000-01-01 TO {PD_CUTOFF}-12-31] AND scanningcenter:* "
    "AND NOT title:(catalogue OR catalog OR index OR bibliography OR dictionary)"
)
SORTS = {
    "relevance": None,
    "popular": "downloads desc",
    "rating": "avg_rating desc",
    "latest": "addeddate desc",
    "newest": "date desc",
    "title": "titleSorter asc",
}
LANGS = {
    "": None,
    "en": "(eng OR English)",
    "pt": "(por OR Portuguese)",
    "es": "(spa OR Spanish)",
    "fr": "(fre OR fra OR French)",
    "it": "(ita OR Italian)",
    "de": "(ger OR deu OR German)",
    "ja": "(jpn OR Japanese)",
}
FILE_EXTS = (".pdf", ".cbz", ".zip")
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
SKIP_SUFFIXES = ("_jp2.zip", "_daisy.zip", "_text.pdf", "_bw.pdf", "_encrypted.pdf", "_raw_jp2.zip", "_tif.zip")


def _escape(q: str) -> str:
    return re.sub(r'([+\-!(){}\[\]^"~*?:\\/]|&&|\|\|)', r"\\\1", q.strip())


def _year(v) -> int | None:
    m = re.match(r"(\d{4})", str(v or ""))
    return int(m.group(1)) if m else None


class InternetArchive(Connector):
    id = "archive"
    name = "Internet Archive"
    description = "Comics, strips and manga on archive.org. Copyright-free filter = pre-1931 library scans."
    languages = [(k, v) for k, v in [("", "Any language"), ("en", "English"), ("pt", "Português"), ("es", "Español"),
                                    ("fr", "Français"), ("it", "Italiano"), ("de", "Deutsch"), ("ja", "日本語")]]  # fmt: skip
    sorts = ["relevance", "popular", "rating", "latest", "newest", "title"]
    availability = ["any", "free"]
    supports_nsfw = False
    licence_info = (
        "archive.org licence and date fields are set by uploaders and are often wrong, so the copyright-free "
        "filter only keeps items physically scanned by libraries and published before 1931 (US public domain)."
    )

    def __init__(self) -> None:
        self.client = httpx.Client(
            headers={"User-Agent": f"recaper/{__version__} (personal recap tool)"},
            timeout=httpx.Timeout(60.0, connect=15.0),
            follow_redirects=True,
        )
        self.limit = RateLimiter(2.0)

    def _json(self, url: str, params=None) -> dict:
        self.limit.wait()
        r = self.client.get(url, params=params)
        r.raise_for_status()
        return r.json()

    # ------------------------------------------------------------------ helpers
    def _to_manga(self, d: dict, files: list | None = None) -> MangaInfo:
        ident = d["identifier"]
        year = _year(d.get("date") or d.get("year"))
        scanned = bool(d.get("scanningcenter"))
        free = bool(scanned and year and year <= PD_CUTOFF)
        lic = d.get("licenseurl")
        desc = d.get("description") or ""
        if isinstance(desc, list):
            desc = " ".join(desc)
        desc = re.sub(r"<[^>]+>", " ", desc)
        subjects = d.get("subject") or []
        if isinstance(subjects, str):
            subjects = [subjects]
        langs = d.get("language") or []
        if isinstance(langs, str):
            langs = [langs]
        creator = d.get("creator")
        if isinstance(creator, list):
            creator = ", ".join(creator)
        return MangaInfo(
            id=ident,
            title=str(d.get("title") or ident),
            alt_titles=[creator] if creator else [],
            description=re.sub(r"\s+", " ", desc).strip()[:1200],
            cover_url=f"{API}/services/img/{ident}",
            year=year,
            status=None,
            tags=[str(s) for s in subjects][:8],
            languages=[str(x) for x in langs],
            content_rating=None,
            url=f"{API}/details/{ident}",
            license=f"Public domain in the US (library scan, published {year})" if free else None,
            license_verified=free,
            license_note=None if free else (f"Uploader-claimed licence (unverified): {lic}" if lic else "No licence information - assume copyrighted"),
            attribution=f"\"{d.get('title') or ident}\" via the Internet Archive ({API}/details/{ident})",
            popularity=int(d["downloads"]) if str(d.get("downloads", "")).isdigit() else None,
        )

    # --------------------------------------------------------------- connector
    def search(self, query: str, lang: str | None = None, limit: int = 24, offset: int = 0,
               sort: str = "relevance", availability: str = "any", nsfw: bool = False) -> list[MangaInfo]:  # fmt: skip
        m = re.search(r"archive\.org/details/([^/?#\s]+)", query or "")
        if m:
            return [self.manga(m.group(1))]
        q = [FREE_BASE if availability == "free" else COMICS]
        if (query or "").strip():
            t = _escape(query)
            q.append(f"(title:({t}) OR creator:({t}) OR subject:({t}))")
        if availability == "free":
            q.append(FREE)
        if lang and LANGS.get(lang):
            q.append(f"language:{LANGS[lang]}")
        if not (query or "").strip() and sort == "relevance":
            sort = "popular"
        params: list[tuple[str, str | int]] = [("q", " AND ".join(q)), ("rows", limit), ("page", offset // max(limit, 1) + 1), ("output", "json")]
        for f in ("identifier", "title", "creator", "date", "downloads", "licenseurl", "language", "subject", "description", "scanningcenter"):
            params.append(("fl[]", f))
        if SORTS.get(sort):
            params.append(("sort[]", SORTS[sort]))
        docs = self._json(f"{API}/advancedsearch.php", params)["response"]["docs"]
        return [self._to_manga(d) for d in docs]

    def _metadata(self, ident: str) -> dict:
        data = self._json(f"{API}/metadata/{quote(ident)}")
        if not data or "metadata" not in data:
            raise KeyError(f"archive.org item {ident!r} not found")
        return data

    def manga(self, manga_id: str) -> MangaInfo:
        data = self._metadata(manga_id)
        return self._to_manga(data["metadata"])

    def chapters(self, manga_id: str, lang: str) -> list[ChapterInfo]:
        files = self._metadata(manga_id).get("files", [])
        originals = [f for f in files if f.get("source") == "original"]

        def wanted(f: dict) -> bool:
            n = f.get("name", "").lower()
            return n.endswith(FILE_EXTS) and not n.endswith(SKIP_SUFFIXES) and "/" not in n.strip("/")

        picked = [f for f in originals if wanted(f)]
        if not picked:  # scanned books: the derived full PDF ("Text PDF") is the readable file
            picked = [f for f in files if f.get("format") in ("Text PDF", "Image Container PDF") and wanted(f)]
        chapters = [
            ChapterInfo(
                id=f"{manga_id}/{f['name']}",
                number=str(i),
                volume=None,
                title=Path(f["name"]).stem,
                pages=0,
                lang=lang or "",
                group=f.get("format", ""),
                kind="file",
                size=int(f.get("size") or 0),
            )
            for i, f in enumerate(sorted(picked, key=lambda f: natural_key(f["name"])), start=1)
        ]
        if not chapters:  # loose page images uploaded as originals
            images = sorted((f for f in originals if f.get("name", "").lower().endswith(IMAGE_EXTS)), key=lambda f: natural_key(f["name"]))
            if images:
                chapters.append(ChapterInfo(id=f"{manga_id}/__images__", number="1", volume=None, title="Pages",
                                            pages=len(images), lang=lang or "", group="images"))  # fmt: skip
        return chapters

    def pages(self, chapter_id: str, quality: str = "data") -> list[Page]:
        ident, _, name = chapter_id.partition("/")
        if name == "__images__":
            files = self._metadata(ident).get("files", [])
            images = sorted((f["name"] for f in files if f.get("source") == "original" and f.get("name", "").lower().endswith(IMAGE_EXTS)), key=natural_key)
            return [Page(url=f"{API}/download/{quote(ident)}/{quote(n)}", filename=f"{i:03d}{Path(n).suffix.lower()}") for i, n in enumerate(images, 1)]
        return [Page(url=f"{API}/download/{quote(ident)}/{quote(name)}", filename=name, kind="file")]

    def fetch_image(self, page: Page) -> bytes:
        r = self.client.get(page.url)
        r.raise_for_status()
        return r.content

    def fetch_to_file(self, page: Page, dest: Path, progress=None) -> None:
        tmp = dest.with_suffix(dest.suffix + ".part")
        with self.client.stream("GET", page.url) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0)
            done = 0
            with tmp.open("wb") as fh:
                for chunk in r.iter_bytes(1 << 20):
                    fh.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
        tmp.replace(dest)

    def fetch_cover(self, url: str) -> tuple[bytes, str]:
        host = urlparse(url).netloc
        if not (host == "archive.org" or host.endswith(".archive.org")):
            raise ValueError("not an archive.org url")
        r = self.client.get(url)
        r.raise_for_status()
        return r.content, r.headers.get("content-type", "image/jpeg")
