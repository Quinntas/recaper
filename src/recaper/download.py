"""Download chapters from a source connector straight into a recaper project folder.

Layout written (exactly what ingest.discover() reads):
  <project>/Ch 0012.5 - Title/001.jpg ...
  <project>/.source.json   where the chapters came from (manga id, language, chapter ids)
  <project>/.cover.jpg     cover for the library view
"""

from __future__ import annotations

import json
import re
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from .sources import ChapterInfo, Connector, MangaInfo

Progress = Callable[[int, int, str], None]  # (done, total, message)


def chapter_folder_name(ch: ChapterInfo) -> str:
    if ch.number:
        try:
            num = float(ch.number)
            whole = f"{int(num):04d}"
            label = whole if num.is_integer() else f"{whole}{ch.number[ch.number.index('.'):]}"
        except ValueError:
            label = re.sub(r"[^\w.-]+", "_", ch.number)
        name = f"Ch {label}"
    else:
        name = f"Oneshot {ch.id[:8]}"
    if ch.title:
        name += " - " + re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "", ch.title).strip()[:60]
    return name.rstrip(". ")


def safe_project_name(title: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "", title).strip().rstrip(".")
    return name[:80] or "manga"


REGISTRY = ".downloads.json"


def downloaded_ids(project_dir: Path) -> set[str]:
    """Chapter ids already fully downloaded into this project."""
    ids: set[str] = set()
    reg = project_dir / REGISTRY
    if reg.exists():
        for cid, rel in json.loads(reg.read_text("utf-8")).items():
            if (project_dir / rel).exists():
                ids.add(cid)
    if project_dir.is_dir():  # older downloads: per-folder marker
        for d in project_dir.iterdir():
            marker = d / ".complete"
            if marker.exists():
                ids.add(json.loads(marker.read_text("utf-8")).get("chapter_id"))
    return ids


def _register(project_dir: Path, chapter_id: str, rel: str) -> None:
    reg = project_dir / REGISTRY
    data = json.loads(reg.read_text("utf-8")) if reg.exists() else {}
    data[chapter_id] = rel
    reg.write_text(json.dumps(data, indent=1), "utf-8")


def download(
    connector: Connector,
    manga: MangaInfo,
    chapters: list[ChapterInfo],
    project_dir: Path,
    quality: str = "data",
    progress: Progress | None = None,
    cancel: threading.Event | None = None,
    image_workers: int = 6,
) -> dict:
    """Download the given chapters; already-complete chapters are skipped. Returns a summary.

    Image chapters become `<project>/Ch 0012 - Title/001.jpg...`; file chapters (cbz/pdf/zip from
    sources like the Internet Archive) become `<project>/Ch 0001 - Title.pdf`. Both are what ingest reads."""
    project_dir.mkdir(parents=True, exist_ok=True)
    _write_meta(connector, manga, chapters, project_dir)

    todo = [c for c in chapters if c.downloadable]
    skipped_external = [c for c in chapters if not c.downloadable]
    unit = {c.id: (c.pages if c.kind == "images" else 1) for c in todo}  # files count as one unit
    done_units, total_units = 0.0, sum(unit.values())
    have = downloaded_ids(project_dir)
    summary = {"downloaded": [], "skipped_existing": [], "skipped_external": [c.id for c in skipped_external], "failed": []}

    def tick(msg: str) -> None:
        if progress:
            progress(done_units, total_units, msg)

    for ch in todo:
        if cancel and cancel.is_set():
            break
        name = chapter_folder_name(ch)
        if ch.id in have:
            summary["skipped_existing"].append(ch.id)
            done_units += unit[ch.id]
            tick(f"{name}: already downloaded")
            continue
        try:
            pages = connector.pages(ch.id, quality)
            if pages and pages[0].kind == "file":
                page = pages[0]
                ext = Path(page.filename).suffix.lower() or ".pdf"
                dest = project_dir / f"{name}{ext}"
                base = done_units

                def file_progress(done: int, total: int) -> None:
                    if total:
                        tick_units = base + done / total
                        if progress:
                            progress(tick_units, total_units, f"{name}: {done / 1e6:.0f}/{total / 1e6:.0f} MB")

                connector.fetch_to_file(page, dest, file_progress)
                _register(project_dir, ch.id, dest.name)
            else:
                folder = project_dir / name
                tmp = project_dir / f".partial-{re.sub(r'[^A-Za-z0-9]+', '', ch.id)[:16]}"
                tmp.mkdir(exist_ok=True)
                lock = threading.Lock()

                def fetch(page) -> None:
                    nonlocal done_units
                    if cancel and cancel.is_set():
                        return
                    dest = tmp / page.filename
                    if not dest.exists():
                        dest.write_bytes(connector.fetch_image(page))
                    with lock:
                        done_units += 1
                        tick(f"{name}: page {page.filename}")

                with ThreadPoolExecutor(image_workers) as pool:
                    list(pool.map(fetch, pages))
                if cancel and cancel.is_set():
                    break
                if folder.exists():
                    shutil.rmtree(folder)
                tmp.rename(folder)
                (folder / ".complete").write_text(json.dumps({"chapter_id": ch.id, "pages": len(pages)}), "utf-8")
                _register(project_dir, ch.id, folder.name)
                done_units -= len(pages)  # replaced by the chapter's nominal unit count below
            done_units += unit[ch.id]
            summary["downloaded"].append(ch.id)
            tick(f"{name}: done")
        except Exception as e:  # noqa: BLE001 - keep going with the other chapters
            summary["failed"].append({"id": ch.id, "chapter": ch.number, "error": str(e)})
            tick(f"{name}: FAILED ({e})")
    return summary


def format_hint(connector: Connector, manga: MangaInfo) -> dict[str, str]:
    """Best guess of page format / reading order from what the source tells us."""
    tags = {t.lower() for t in manga.tags}
    if connector.id == "peppercarrot":
        return {"source": "manga", "reading": "ltr"}  # western colour pages, several panels per page
    if "long strip" in tags or "web comic" in tags and manga.original_language in ("ko", "zh", "zh-hk"):
        return {"source": "manhwa"}
    if manga.original_language == "ja":
        return {"source": "manga", "reading": "rtl"}
    if manga.original_language in ("ko", "zh", "zh-hk"):
        return {"source": "manhwa"}
    if connector.id == "archive":
        return {"source": "manga", "reading": "ltr"}  # scanned books / comic pages
    return {}


def _write_hint(connector: Connector, manga: MangaInfo, project_dir: Path) -> None:
    """New projects get a recaper.toml with the format hint (never overwrites user settings)."""
    from .config import CONFIG_NAME

    path = project_dir / CONFIG_NAME
    hint = format_hint(connector, manga)
    if path.exists() or not hint:
        return
    lines = ["# written by the downloader from the source's metadata - edit freely"]
    lines += [f'{k} = "{v}"' for k, v in hint.items()]
    path.write_text("\n".join(lines) + "\n", "utf-8")


def _write_meta(connector: Connector, manga: MangaInfo, chapters: list[ChapterInfo], project_dir: Path) -> None:
    _write_hint(connector, manga, project_dir)
    meta_path = project_dir / ".source.json"
    meta = json.loads(meta_path.read_text("utf-8")) if meta_path.exists() else {}
    known = {c["id"]: c for c in meta.get("chapters", [])}
    for c in chapters:
        known[c.id] = asdict(c)
    meta.update({"source": connector.id, "manga": asdict(manga), "chapters": list(known.values())})
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), "utf-8")
    cover = project_dir / ".cover.jpg"
    if manga.cover_url and not cover.exists():
        try:
            data, _ = connector.fetch_cover(manga.cover_url)
            cover.write_bytes(data)
        except Exception:  # noqa: BLE001 - cosmetic
            pass
