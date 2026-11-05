"""Discover chapters in the input folder.

Accepted layouts (can be mixed):
  my-series/Chapter 1/*.jpg     one sub-folder of images per chapter
  my-series/ch001.cbz           one archive (cbz/zip) per chapter
  my-series/vol1.pdf            one PDF per chapter / volume
  my-series/*.jpg               loose images = a single chapter
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from .config import WORK_DIR_NAME

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".avif"}
ARCHIVE_EXTS = {".cbz", ".zip"}
PDF_EXTS = {".pdf"}


@dataclass
class Chapter:
    index: int  # 0-based position in reading order
    title: str
    pages: list[Path]

    @property
    def cid(self) -> str:
        return f"c{self.index + 1:04d}"


def natural_key(text: str) -> list:
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+(?:\.\d+)?)", text) if t]


def _chapter_sort_key(name: str) -> tuple:
    # Prefer an explicit "chapter/ch/ep N" number; fall back to natural ordering.
    m = re.search(r"(?:chapter|chap|ch|ep|episode|#)[\s._-]*(\d+(?:\.\d+)?)", name, re.I)
    if m:
        return (0, float(m.group(1)), natural_key(name))
    return (1, 0.0, natural_key(name))


def _images_in(folder: Path) -> list[Path]:
    return sorted(
        (p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS),
        key=lambda p: natural_key(p.name),
    )


def discover(input_dir: Path, work_dir: Path) -> list[Chapter]:
    entries: list[tuple[str, list[Path]]] = []
    for child in input_dir.iterdir():
        if child.name.startswith(".") or child.name == WORK_DIR_NAME:
            continue
        if child.is_dir():
            imgs = _images_in(child)
            if imgs:
                entries.append((child.name, imgs))
            else:  # allow one level deeper (e.g. Volume 1/Chapter 3/)
                for sub in child.iterdir():
                    if sub.is_dir() and (sub_imgs := _images_in(sub)):
                        entries.append((f"{child.name}/{sub.name}", sub_imgs))
        elif child.suffix.lower() in ARCHIVE_EXTS:
            entries.append((child.stem, _extract_archive(child, work_dir / "pages" / _slug(child.stem))))
        elif child.suffix.lower() in PDF_EXTS:
            entries.append((child.stem, _render_pdf(child, work_dir / "pages" / _slug(child.stem))))

    loose = _images_in(input_dir)
    if loose and not entries:
        entries.append((input_dir.name, loose))

    entries.sort(key=lambda e: _chapter_sort_key(e[0]))
    return [Chapter(i, title, pages) for i, (title, pages) in enumerate(entries) if pages]


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_") or "chapter"


def _extract_archive(path: Path, dest: Path) -> list[Path]:
    if not dest.exists() or not any(dest.iterdir()):
        dest.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path) as zf:
            names = sorted(
                (n for n in zf.namelist() if Path(n).suffix.lower() in IMAGE_EXTS and not n.startswith("__MACOSX")),
                key=natural_key,
            )
            for i, name in enumerate(names):
                # flatten + renumber to avoid path traversal and keep order
                target = dest / f"{i:04d}{Path(name).suffix.lower()}"
                target.write_bytes(zf.read(name))
    return _images_in(dest)


def _render_pdf(path: Path, dest: Path, dpi: int = 160) -> list[Path]:
    if not dest.exists() or not any(dest.iterdir()):
        import pymupdf

        dest.mkdir(parents=True, exist_ok=True)
        with pymupdf.open(path) as doc:
            for i, page in enumerate(doc):
                images = page.get_images(full=True)
                if len(images) == 1:
                    # scanned / webtoon PDFs: one image per page -> take it at native resolution
                    info = doc.extract_image(images[0][0])
                    if info and info.get("image") and info.get("width", 0) >= 300:
                        ext = info.get("ext", "png")
                        if ext in ("jpg", "jpeg", "png", "webp", "bmp"):
                            (dest / f"{i:04d}.{ext}").write_bytes(info["image"])
                            continue
                page.get_pixmap(dpi=dpi).save(dest / f"{i:04d}.png")
    return _images_in(dest)


def detect_source(chapters: list[Chapter], sample: int = 24) -> str:
    """Guess 'manhwa' (long colour strips) vs 'manga' (B&W pages)."""
    pages = [p for ch in chapters[:4] for p in ch.pages][:sample]
    if not pages:
        return "manga"
    ratios, sats, widths = [], [], []
    for p in pages:
        with Image.open(p) as im:
            widths.append(im.width)
            ratios.append(im.height / max(im.width, 1))
            small = im.convert("RGB").resize((64, max(1, int(64 * im.height / im.width))))
            hsv = np.asarray(small.convert("HSV"), dtype=np.float32)
            inked = hsv[..., 2] < 235  # ignore white gutters / margins
            sats.append(float(hsv[..., 1][inked].mean()) if inked.any() else 0.0)
    ratio = float(np.median(ratios))
    sat = float(np.median(sats))
    same_width = (max(widths) - min(widths)) <= 0.02 * max(widths)
    if ratio > 2.2 or (same_width and ratio >= 1.8):  # webtoon chunks
        return "manhwa"
    return "manhwa" if sat > 45 else "manga"
