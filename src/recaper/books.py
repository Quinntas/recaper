"""Light novels / books: EPUB, text PDF and TXT volumes -> chapters of prose + the book's own illustrations.

Chapter boundaries, best first:
  EPUB: table of contents (nav / NCX) mapped onto the spine
  PDF : bookmarks (outline); else headings like "Chapter 3" / "Prologue" at the top of a page
  any : fixed-size chunks (BookConfig.split_words) as a last resort
Short front-matter sections (title page, copyright, contents...) are dropped, but their images (colour
inserts!) are kept and attached to the next real chapter.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import zipfile
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree as ET

from .ingest import WORK_DIR_NAME, _chapter_sort_key, natural_key

BOOK_EXTS = {".epub", ".txt", ".md"}
HEADING = re.compile(
    r"^\s*(chapter|chap\.|prologue|epilogue|interlude|intermission|afterword|side story|extra|part|cap[ií]tulo|pr[óo]logo|ep[íi]logo)\b[^\n]{0,80}$",
    re.I | re.M,
)
SKIP_TITLES = re.compile(
    r"(copyright|table of contents|^contents$|newsletter|title page|^cover$|credits|about the (author|translator)|sign up|project gutenberg|licen[cs]e)",
    re.I,
)
JUNK_TITLE = re.compile(r"^\s*[\[(]?\s*[\divxlc]+\s*[\])]?\s*$", re.I)  # page-number links like "[14]"


@dataclass
class BookChapter:
    title: str
    text: str
    volume: str
    images: list[str] = field(default_factory=list)  # extracted illustration files (relative to work dir)

    @property
    def words(self) -> int:
        return len(self.text.split())


# ------------------------------------------------------------------ detection


def pdf_is_text(path: Path, sample: int = 8) -> bool:
    """A PDF counts as a book if its pages carry a real text layer (manga PDFs are just images)."""
    import pymupdf

    with pymupdf.open(path) as doc:
        n = len(doc)
        if not n:
            return False
        idx = sorted({int(i * (n - 1) / max(sample - 1, 1)) for i in range(sample)})
        words = [len(doc[i].get_text().split()) for i in idx]
    words.sort()
    return words[len(words) // 2] >= 120  # median page has real prose


def is_output_file(f: Path) -> bool:
    """recaper's own outputs living next to the inputs (YouTube chapter timestamps, credits...)."""
    return f.name.endswith((".chapters.txt", ".credits.txt")) or f.name == "recaper.toml"


def book_files(input_dir: Path) -> list[Path]:
    files = []
    for f in input_dir.iterdir():
        if f.name.startswith(".") or not f.is_file() or is_output_file(f):
            continue
        ext = f.suffix.lower()
        if ext in BOOK_EXTS or (ext == ".pdf" and pdf_is_text(f)):
            files.append(f)
    return sorted(files, key=lambda f: _chapter_sort_key(f.stem))


def is_book_project(input_dir: Path) -> bool:
    files = book_files(input_dir)
    if not files:
        return False
    images = [d for d in input_dir.iterdir() if d.is_dir() and not d.name.startswith(".") and d.name != WORK_DIR_NAME]
    return len(files) >= len(images)


# --------------------------------------------------------------------- html


class _Text(HTMLParser):
    BLOCK = {"p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6", "li", "section", "blockquote", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.images: list[str] = []
        self.headings: list[str] = []
        self._skip = 0
        self._in_h = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style", "head"):
            self._skip += 1
        if tag in ("img", "image"):
            src = a.get("src") or a.get("xlink:href") or a.get("href")
            if src:
                self.images.append(src)
        if tag in ("h1", "h2", "h3"):
            self._in_h = True
            self.headings.append("")
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head"):
            self._skip = max(0, self._skip - 1)
        if tag in ("h1", "h2", "h3"):
            self._in_h = False
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip:
            return
        self.parts.append(data)
        if self._in_h and self.headings:
            self.headings[-1] += data

    def text(self) -> str:
        t = "".join(self.parts)
        t = re.sub(r"[ \t\r\f\v]+", " ", t)
        return re.sub(r"\n\s*\n+", "\n\n", t).strip()


# --------------------------------------------------------------------- epub


def _epub(path: Path, out_dir: Path, rel_root: Path, min_px: int) -> list[BookChapter]:
    from PIL import Image

    zf = zipfile.ZipFile(path)
    container = ET.fromstring(zf.read("META-INF/container.xml"))
    opf_path = next(e.get("full-path") for e in container.iter() if e.tag.endswith("rootfile"))
    opf = ET.fromstring(zf.read(opf_path))
    base = posixpath.dirname(opf_path)
    ns = lambda e: e.tag.split("}")[-1]  # noqa: E731
    manifest = {e.get("id"): e for e in opf.iter() if ns(e) == "item"}
    spine = [e.get("idref") for e in opf.iter() if ns(e) == "itemref"]

    # titles from nav.xhtml or toc.ncx: href (without fragment) -> title
    titles: dict[str, str] = {}
    for item in manifest.values():
        href = posixpath.join(base, item.get("href", ""))
        props = item.get("properties") or ""
        try:
            if "nav" in props:
                p = _Toc()
                p.feed(zf.read(href).decode("utf-8", "replace"))
                for h, t in p.links:
                    titles.setdefault(posixpath.normpath(posixpath.join(posixpath.dirname(href), h.split("#")[0])), t.strip())
            elif item.get("media-type") == "application/x-dtbncx+xml":
                ncx = ET.fromstring(zf.read(href))
                for np_ in ncx.iter():
                    if ns(np_) != "navPoint":
                        continue
                    label = next((x.text for x in np_.iter() if ns(x) == "text"), "") or ""
                    src = next((x.get("src") for x in np_.iter() if ns(x) == "content"), "") or ""
                    titles.setdefault(posixpath.normpath(posixpath.join(posixpath.dirname(href), src.split("#")[0])), label.strip())
        except (KeyError, ET.ParseError):
            continue

    sections: list[tuple[str | None, str, list[str]]] = []  # (toc title or None, text, image files)
    img_n = 0
    for idref in spine:
        item = manifest.get(idref)
        if item is None:
            continue
        href = posixpath.normpath(posixpath.join(base, item.get("href", "")))
        try:
            html = zf.read(href).decode("utf-8", "replace")
        except KeyError:
            continue
        p = _Text()
        p.feed(html)
        imgs = []
        for src in p.images:
            ipath = posixpath.normpath(posixpath.join(posixpath.dirname(href), src.split("#")[0]))
            try:
                data = zf.read(ipath)
            except KeyError:
                continue
            img_n += 1
            dest = out_dir / f"img{img_n:04d}{Path(ipath).suffix.lower() or '.jpg'}"
            dest.write_bytes(data)
            try:
                with Image.open(dest) as im:
                    if min(im.size) < min_px:
                        dest.unlink()
                        continue
            except Exception:  # noqa: BLE001 - unreadable image
                dest.unlink(missing_ok=True)
                continue
            imgs.append(dest.relative_to(rel_root).as_posix())
        title = titles.get(href)
        heading = next((h.strip() for h in p.headings if h.strip()), "") or _text_heading(p.text())
        if title and JUNK_TITLE.match(title):
            title = heading or title
        if not title and heading and HEADING.match(heading):
            title = heading
        sections.append((title, p.text(), imgs))
    return _group(sections, path.stem)


def _text_heading(text: str) -> str:
    """'CHAPTER III' (+ a short subtitle line) found at the very start of a section's text."""
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()][:4]
    for i, line in enumerate(lines):
        if HEADING.match(line):
            sub = lines[i + 1] if i + 1 < len(lines) and len(lines[i + 1]) <= 60 and not lines[i + 1].endswith(".") else ""
            return f"{line} - {sub}" if sub else line
    return ""


class _Toc(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._buf = ""

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._buf = ""

    def handle_data(self, data):
        if self._href is not None:
            self._buf += data

    def handle_endtag(self, tag):
        if tag == "a" and self._href:
            self.links.append((self._href, self._buf))
            self._href = None


def _group(sections: list[tuple[str | None, str, list[str]]], volume: str) -> list[BookChapter]:
    """Sections with a title start a chapter; untitled ones continue the previous chapter."""
    chapters: list[BookChapter] = []
    pending_images: list[str] = []
    for title, text, imgs in sections:
        if title and SKIP_TITLES.search(title):
            pending_images += imgs
            continue
        if title or not chapters:
            chapters.append(BookChapter(title or volume, text, volume, pending_images + imgs))
            pending_images = []
        else:
            chapters[-1].text += "\n\n" + text
            chapters[-1].images += imgs
    if pending_images and chapters:
        chapters[-1].images += pending_images
    return chapters


# ---------------------------------------------------------------------- pdf


def _pdf(path: Path, out_dir: Path, rel_root: Path, min_px: int) -> list[BookChapter]:
    import pymupdf

    sections: list[tuple[str | None, str, list[str]]] = []
    with pymupdf.open(path) as doc:
        pages_text = [doc[i].get_text() for i in range(len(doc))]
        page_imgs: list[list[str]] = [[] for _ in range(len(doc))]
        seen: set[int] = set()
        n = 0
        for i in range(len(doc)):
            page = doc[i]
            page_area = max(page.rect.width * page.rect.height, 1)
            words = len(pages_text[i].split())
            for info in page.get_images(full=True):
                xref = info[0]
                if xref in seen:
                    continue
                seen.add(xref)
                # scanned books: every page is a full-page image with an OCR text layer - that's text, not art.
                # A real illustration page has (almost) no text, or the picture covers only part of the page.
                try:
                    covered = max((r.width * r.height for r in page.get_image_rects(xref)), default=0) / page_area
                except Exception:  # noqa: BLE001
                    covered = 0
                if covered > 0.75 and words > 80:
                    continue
                try:
                    img = doc.extract_image(xref)
                except Exception:  # noqa: BLE001
                    continue
                if not img or min(img.get("width", 0), img.get("height", 0)) < min_px:
                    continue
                n += 1
                dest = out_dir / f"img{n:04d}.{img.get('ext', 'jpg')}"
                dest.write_bytes(img["image"])
                page_imgs[i].append(dest.relative_to(rel_root).as_posix())
        # boundaries: bookmarks, else headings at the top of pages
        starts: list[tuple[int, str]] = []
        toc = [t for t in doc.get_toc() if t[2] >= 1]
        if len(toc) >= 3:
            levels = {}
            for lvl, _, _ in toc:
                levels[lvl] = levels.get(lvl, 0) + 1
            lvl = min((l for l, c in levels.items() if c >= 3), default=1)
            starts = [(page - 1, title) for l, title, page in toc if l == lvl]
        else:
            for i, t in enumerate(pages_text):
                head = "\n".join(t.strip().splitlines()[:4])
                m = HEADING.search(head)
                if m:
                    starts.append((i, m.group(0).strip()))
    if not starts:
        starts = [(0, None)]
    if starts[0][0] > 0:
        starts.insert(0, (0, None))
    for k, (pg, title) in enumerate(starts):
        end = starts[k + 1][0] if k + 1 < len(starts) else len(pages_text)
        text = "\n".join(pages_text[pg:end])
        imgs = [x for i in range(pg, end) for x in page_imgs[i]]
        sections.append((title, text, imgs))
    return _group(sections, path.stem)


def _txt(path: Path) -> list[BookChapter]:
    text = path.read_text("utf-8", errors="replace")
    marks = [(m.start(), m.group(0).strip()) for m in HEADING.finditer(text)]
    if not marks:
        return [BookChapter(path.stem, text, path.stem)]
    sections = [(None, text[: marks[0][0]], [])] if marks[0][0] > 0 else []
    for k, (pos, title) in enumerate(marks):
        end = marks[k + 1][0] if k + 1 < len(marks) else len(text)
        sections.append((title, text[pos:end], []))
    return _group(sections, path.stem)


def _split_long(chapters: list[BookChapter], max_words: int) -> list[BookChapter]:
    """Very long 'chapters' (no structure found) are cut into parts at paragraph breaks."""
    out = []
    for ch in chapters:
        if ch.words <= max_words * 1.5:
            out.append(ch)
            continue
        paras, cur, parts = ch.text.split("\n\n"), [], []
        for para in paras:
            cur.append(para)
            if sum(len(p.split()) for p in cur) >= max_words:
                parts.append("\n\n".join(cur))
                cur = []
        if cur:
            parts.append("\n\n".join(cur))
        per = max(1, len(ch.images) // max(len(parts), 1))
        for i, part in enumerate(parts):
            imgs = ch.images[i * per : (i + 1) * per] if i < len(parts) - 1 else ch.images[i * per :]
            out.append(BookChapter(f"{ch.title} (part {i + 1})", part, ch.volume, imgs))
    return out


# ------------------------------------------------------------------- public


def extract(input_dir: Path, work_dir: Path, split_words: int = 6000, min_words: int = 150, min_px: int = 300) -> list[BookChapter]:
    """All chapters of all volumes in reading order (cached in .recaper/book/)."""
    out: list[BookChapter] = []
    for f in book_files(input_dir):
        key = hashlib.sha1(f"{f.name}|{f.stat().st_size}|{split_words}|{min_words}|{min_px}".encode()).hexdigest()[:10]
        vol_dir = work_dir / "book" / f"{re.sub(r'[^A-Za-z0-9]+', '_', f.stem)[:40]}_{key}"
        index = vol_dir / "chapters.json"
        if index.exists():
            out += [BookChapter(**c) for c in json.loads(index.read_text("utf-8"))]
            continue
        vol_dir.mkdir(parents=True, exist_ok=True)
        ext = f.suffix.lower()
        chs = _epub(f, vol_dir, work_dir, min_px) if ext == ".epub" else _pdf(f, vol_dir, work_dir, min_px) if ext == ".pdf" else _txt(f)
        # drop front/back matter without prose but keep its pictures for the next chapter
        kept: list[BookChapter] = []
        carry: list[str] = []
        for ch in chs:
            if ch.words < min_words:
                carry += ch.images
                continue
            ch.images = carry + ch.images
            carry = []
            kept.append(ch)
        if carry and kept:
            kept[-1].images += carry
        kept = _split_long(kept, split_words)
        index.write_text(json.dumps([asdict(c) for c in kept], ensure_ascii=False), "utf-8")
        out += kept
    return out


def sort_volumes(names: list[str]) -> list[str]:
    return sorted(names, key=natural_key)
