"""Book / light-novel mode: prose -> narration, pictures from the book's own illustrations, from linked
adaptations (related works in the library), or generated in the work's official style.

Stages (cached under .recaper/ like the manga flow):
  panels  books.extract() -> chapters + illustrations; illustrations become this chapter's panels
  read    illustrations described by the vision reader; prose digested into events by the writer model;
          linked image sources are read (their own pipeline) and matched chapter-to-chapter (related_map.json)
  script  writer picks pictures per beat from the IMAGE LIBRARY or asks for a generated one; generation
          (or, without a generator / over budget, the nearest real image) fills those beats
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

import cv2
import numpy as np

from . import imagegen, progress, prompts
from .books import BookChapter, extract
from .config import Config, parse_chapter_ranges
from .ingest import Chapter
from .llm import Chain, ImagePart, RefusalError
from .log import console
from .panels import Panel, imread, imwrite
from .story import Bible, _clean_text, _norm_label, _read_tiles, reconcile_bible

if TYPE_CHECKING:
    from .pipeline import Project

MAX_PROSE_WORDS = 24000  # per digest call (a long light-novel chapter fits comfortably)


# ------------------------------------------------------------------ panels


def book_panels(project: "Project") -> None:
    cfg, work = project.cfg, project.work
    bc = cfg.book
    chapters = extract(project.input_dir, work, bc.split_words, bc.min_words, bc.min_illustration)
    if not chapters:
        raise SystemExit(f"No readable book text found in {project.input_dir} (EPUB, text PDF or TXT).")
    project.book_chapters = chapters
    project.chapters = [Chapter(i, ch.title, [work / p for p in ch.images]) for i, ch in enumerate(chapters)]
    project.source = "book"

    # chapter identity changed (e.g. a volume inserted before) -> drop that chapter's caches
    manifest_path = work / "manifest.json"
    old = json.loads(manifest_path.read_text("utf-8")) if manifest_path.exists() else {}
    ident = {c.cid: f"{bk.volume}|{bk.title}|{bk.words}" for c, bk in zip(project.chapters, chapters)}
    for cid, sig in ident.items():
        if old.get("chapters", {}).get(cid) not in (None, sig):
            shutil.rmtree(work / "panels" / cid, ignore_errors=True)
            (work / "notes" / f"{cid}.json").unlink(missing_ok=True)
            for f in (work / "script").glob(f"{cid}_*.json"):
                f.unlink()
            (work / "script.json").unlink(missing_ok=True)
    manifest_path.write_text(json.dumps({"source": "book", "chapters": ident}, indent=1), "utf-8")

    idx = parse_chapter_ranges(cfg.chapters, len(project.chapters))
    project.selected = [project.chapters[i] for i in idx]
    vols = len({c.volume for c in chapters})
    console.print(f"[bold]{len(chapters)} chapters[/] in {vols} volume(s), {len(project.selected)} selected - source: [bold]book[/]")
    for i, ch in enumerate(project.selected, start=1):
        bk = chapters[ch.index]
        project.panels[ch.cid] = _illustration_panels(ch, work)
        progress.emit("panels", i, len(project.selected), f"{ch.title}: {bk.words} words, {len(project.panels[ch.cid])} illustrations")
        console.print(f"  {ch.cid} [dim]{ch.title}[/]: {bk.words} words, {len(project.panels[ch.cid])} illustrations")


def _illustration_panels(ch: Chapter, work: Path) -> list[Panel]:
    out_dir = work / "panels" / ch.cid
    index = out_dir / "panels.json"
    sig = json.dumps([str(p) for p in ch.pages])
    if index.exists() and (out_dir / "params.json").exists() and (out_dir / "params.json").read_text("utf-8") == sig:
        return [Panel(**p) for p in json.loads(index.read_text("utf-8"))]
    panels = []
    for i, src in enumerate(ch.pages, start=1):
        img = imread(src)
        if img is None:
            continue
        long_edge = max(img.shape[:2])
        if long_edge > 3200:
            s = 3200 / long_edge
            img = cv2.resize(img, (round(img.shape[1] * s), round(img.shape[0] * s)), interpolation=cv2.INTER_AREA)
        rel = Path("panels") / ch.cid / f"p{i:04d}.jpg"
        imwrite(work / rel, img)
        panels.append(Panel(f"{ch.cid}-p{i:04d}", ch.cid, rel.as_posix(), img.shape[1], img.shape[0], None))
    out_dir.mkdir(parents=True, exist_ok=True)
    index.write_text(json.dumps([asdict(p) for p in panels], indent=1), "utf-8")
    (out_dir / "params.json").write_text(sig, "utf-8")
    return panels


# -------------------------------------------------------------------- read


def _as_digest_dict(val: Any, title: str) -> dict:
    if isinstance(val, dict):
        return val
    if isinstance(val, list):
        if len(val) == 1 and isinstance(val[0], dict) and ("events" in val[0] or "summary" in val[0]):
            return val[0]
        return {"story": True, "title": title, "summary": "", "events": [e for e in val if isinstance(e, dict)], "characters": [], "terms": []}
    return {"story": True, "title": title, "summary": "", "events": [], "characters": [], "terms": []}


def _digest_prose(cid: str, title: str, words: list[str], bible_text: str, writer: Chain) -> dict:
    """Digest chapter prose; if a content filter refuses the full chapter, split into smaller chunks and merge."""
    capped = words[:MAX_PROSE_WORDS]
    header = f"STORY BIBLE:\n{bible_text or '(empty - first chapter)'}\n\nCHAPTER: {title}\n\nTEXT:\n"
    prose = " ".join(capped) + (" [...]" if len(words) > MAX_PROSE_WORDS else "")
    try:
        return _as_digest_dict(writer.complete_json(prompts.BOOK_DIGEST_SYSTEM, [header + prose]), title)
    except RefusalError:
        console.print(f"[yellow]{cid}: chapter text triggered content filter; splitting into segments to skip filtered passage[/]")

    def _split_attempt(chunk: list[str], depth: int) -> list[dict]:
        try:
            return [_as_digest_dict(writer.complete_json(prompts.BOOK_DIGEST_SYSTEM, [header + " ".join(chunk)]), title)]
        except RefusalError:
            if depth >= 2 or len(chunk) < 400:
                console.print(f"[yellow]{cid}: skipping {len(chunk)}-word segment refused by content filter[/]")
                return []
            mid = len(chunk) // 2
            return _split_attempt(chunk[:mid], depth + 1) + _split_attempt(chunk[mid:], depth + 1)

    mid = len(capped) // 2
    parts = (_split_attempt(capped[:mid], 1) + _split_attempt(capped[mid:], 1)) if mid >= 200 else []
    if not parts:
        return {"story": True, "title": title, "summary": f"Events of {title}.", "events": [{"desc": f"Key events in {title}.", "chars": [], "weight": 2}], "characters": [], "terms": []}
    return {
        "story": any(p.get("story", True) is not False for p in parts),
        "title": next((p["title"] for p in parts if p.get("title")), title),
        "summary": " ".join(str(p.get("summary", "")).strip() for p in parts if p.get("summary")).strip(),
        "events": [e for p in parts for e in (p.get("events") or [])],
        "characters": [c for p in parts for c in (p.get("characters") or [])],
        "terms": [t for p in parts for t in (p.get("terms") or [])],
    }


def _read_book_chapter(ch: Chapter, bk: BookChapter, panels: list[Panel], bible_text: str, reader: Chain, writer: Chain, work: Path) -> dict:
    path = work / "notes" / f"{ch.cid}.json"
    if path.exists():
        return json.loads(path.read_text("utf-8"))
    labels = {f"P{i}": p for i, p in enumerate(panels, start=1)}
    panel_notes: dict[str, dict] = {}
    if labels:
        parts: list = [f"STORY BIBLE:\n{bible_text or '(empty)'}\n\nCHAPTER: {bk.title}\nThe illustrations follow."]
        for label, p in labels.items():
            parts.append(f"[{label}]")
            parts.extend(ImagePart(t) for t in _read_tiles(p, work))
        try:
            res = reader.complete_json(prompts.ILLUSTRATION_SYSTEM, parts)
            for e in res.get("panels", []):
                lab = _norm_label(e.get("id"))
                if lab in labels:
                    panel_notes[lab] = {"story": bool(e.get("story", True)), "weight": int(e.get("weight", 3) or 3),
                                        "desc": str(e.get("desc", "")).strip(), "chars": e.get("chars") or []}  # fmt: skip
        except Exception as e:  # noqa: BLE001 - illustrations are optional; prose is what matters
            console.print(f"[yellow]{ch.cid}: could not describe illustrations ({e})[/]")
    for lab in labels:
        panel_notes.setdefault(lab, {"story": True, "weight": 2, "desc": "illustration", "chars": []})

    words = bk.text.split()
    console.print(f"  digesting {ch.cid} ({len(words)} words)")
    res = _digest_prose(ch.cid, bk.title, words, bible_text, writer)
    events = []
    for i, e in enumerate(res.get("events") or [], start=1):
        if e.get("desc"):
            events.append({"id": f"E{i}", "desc": str(e["desc"]).strip(), "chars": e.get("chars") or [], "weight": int(e.get("weight", 2) or 2)})
    notes = {
        "cid": ch.cid, "kind": "book", "story": res.get("story", True) is not False, "title": str(res.get("title") or bk.title).strip(), "source_title": bk.title,
        "volume": bk.volume, "summary": str(res.get("summary", "")).strip(), "events": events,
        "labels": {k: p.id for k, p in labels.items()}, "panels": panel_notes,
        "characters": res.get("characters") or [], "terms": res.get("terms") or [],
    }  # fmt: skip
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(notes, ensure_ascii=False, indent=1), "utf-8")
    return notes


def book_read(project: "Project", reader: Chain, writer: Chain) -> tuple[list[dict], Bible]:
    cfg, work = project.cfg, project.work
    chapters = project.selected
    books = project.book_chapters
    t0 = time.time()
    progress.emit("read", 0, len(chapters), f"reading {chapters[0].title}")
    first = _read_book_chapter(chapters[0], books[chapters[0].index], project.panels[chapters[0].cid], "", reader, writer, work)
    notes_by = {first["cid"]: first}
    seed = Bible()
    seed.merge(first.get("characters"), first.get("terms"))
    progress.emit("read", 1, len(chapters), chapters[0].title)
    with ThreadPoolExecutor(max(1, cfg.jobs)) as pool:
        futs = {pool.submit(_read_book_chapter, ch, books[ch.index], project.panels[ch.cid], seed.text(), reader, writer, work): ch.cid
                for ch in chapters[1:]}  # fmt: skip
        for f in as_completed(futs):
            notes_by[futs[f]] = f.result()
            progress.emit("read", len(notes_by), len(chapters), futs[f])
    all_notes = [notes_by[ch.cid] for ch in chapters]
    bible = reconcile_bible(all_notes, writer, work)
    console.print(f"  read {len(all_notes)} chapters in {time.time() - t0:.0f}s; bible: {len(bible.characters)} characters")
    return all_notes, bible


# ---------------------------------------------------------- related works


def _source_projects(project: "Project") -> list[Path]:
    out = []
    for name in [s.strip() for s in project.cfg.image_sources.split(",") if s.strip()]:
        p = (project.input_dir.parent / name).resolve()
        if p.is_dir() and p != project.input_dir:
            out.append(p)
        else:
            console.print(f"[yellow]image source {name!r} not found next to this project - skipped[/]")
    return out


def _load_source_notes(src: Path) -> list[dict]:
    """Make sure the linked project has been cut + read (its own settings), then load its notes."""
    from .pipeline import Project

    work = src / ".recaper"
    have = sorted((work / "notes").glob("c*.json")) if (work / "notes").exists() else []
    if not have:
        console.print(f"[bold]image source[/] {src.name}: cutting + reading its panels first")
        sub = Project(src, Config.load(src, {}))
        sub.stage_panels()
        sub.stage_script(until_read=True)
        have = sorted((work / "notes").glob("c*.json"))
    return [json.loads(p.read_text("utf-8")) for p in have]


def related_library(project: "Project", all_notes: list[dict], writer: Chain) -> dict[str, list[dict]]:
    """book cid -> candidate images from linked adaptations: [{label, src, pid, desc, chars, weight}]."""
    sources = _source_projects(project)
    if not sources:
        return {}
    src_notes: dict[str, dict] = {}  # key "<src>:<cid>" -> notes
    for src in sources:
        for n in _load_source_notes(src):
            src_notes[f"{src.name}:{n['cid']}"] = n | {"_src": str(src)}
    if not src_notes:
        return {}

    key = hashlib.sha1(json.dumps([[n["cid"], n["summary"][:200]] for n in all_notes] + sorted(src_notes)).encode()).hexdigest()[:12]
    map_path = project.work / f"related_map_{key}.json"
    if map_path.exists():
        mapping = json.loads(map_path.read_text("utf-8"))
    else:
        src_lines = "\n".join(f"{k}: {n['title']} - {' '.join(n['summary'].split()[:45])}" for k, n in src_notes.items())
        mapping = {}
        for i in range(0, len(all_notes), 30):
            chunk = all_notes[i : i + 30]
            book_lines = "\n".join(f"{n['cid']}: {n['title']} - {' '.join(n['summary'].split()[:60])}" for n in chunk)
            console.print(f"  matching book chapters {i + 1}-{i + len(chunk)} to {len(src_notes)} adaptation chapters")
            res = writer.complete_json(prompts.MAP_SYSTEM, [f"BOOK CHAPTERS:\n{book_lines}\n\nADAPTATION CHAPTERS:\n{src_lines}"])
            for cid, keys in (res.get("map") or {}).items():
                mapping[str(cid)] = [k for k in keys or [] if k in src_notes]
        map_path.write_text(json.dumps(mapping, indent=1), "utf-8")

    out: dict[str, list[dict]] = {}
    cap = project.cfg.images.related_per_chapter
    for n in all_notes:
        cands = []
        for k in mapping.get(n["cid"], []):
            sn = src_notes[k]
            for lab, info in sn["panels"].items():
                if info.get("story", True) and info.get("weight", 2) >= 2 and info.get("desc"):
                    cands.append({"src": sn["_src"], "pid": sn["labels"][lab], "desc": info["desc"], "chars": info.get("chars") or [], "weight": info["weight"]})
        cands.sort(key=lambda c: -c["weight"])
        for j, c in enumerate(cands[:cap], start=1):
            c["label"] = f"R{j}"
        out[n["cid"]] = cands[:cap]
    linked = sum(1 for v in out.values() if v)
    console.print(f"  related works: {linked}/{len(all_notes)} chapters have matching adaptation panels")
    return out


class XrefStore:
    """Imports panels from linked projects into this project's panel namespace (panels/xref/)."""

    def __init__(self, work: Path):
        self.dir = work / "panels" / "xref"
        self.index = self.dir / "panels.json"
        self.map_file = self.dir / "map.json"
        self.panels = [Panel(**p) for p in json.loads(self.index.read_text("utf-8"))] if self.index.exists() else []
        self.map = json.loads(self.map_file.read_text("utf-8")) if self.map_file.exists() else {}
        self.work = work

    def get(self, src: str, pid: str) -> str | None:
        key = f"{src}|{pid}"
        if key in self.map:
            return self.map[key]
        src_work = Path(src) / ".recaper"
        cid = pid.split("-")[0]
        idx = src_work / "panels" / cid / "panels.json"
        if not idx.exists():
            return None
        item = next((p for p in json.loads(idx.read_text("utf-8")) if p["id"] == pid), None)
        if not item:
            return None
        n = len(self.panels) + 1
        rel = Path("panels") / "xref" / f"p{n:04d}.jpg"
        self.dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src_work / item["file"], self.work / rel)
        new = Panel(f"xref-p{n:04d}", "xref", rel.as_posix(), item["w"], item["h"], None)
        self.panels.append(new)
        self.map[key] = new.id
        self.save()
        return new.id

    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index.write_text(json.dumps([asdict(p) for p in self.panels], indent=1), "utf-8")
        self.map_file.write_text(json.dumps(self.map, indent=1), "utf-8")


# ------------------------------------------------------------------ writing


def library_text(notes: dict, related: list[dict]) -> str:
    lines = []
    for lab, info in notes["panels"].items():
        if info.get("story", True):
            lines.append(f"{lab} (book illustration): {info['desc']}")
    for c in related:
        lines.append(f"{c['label']} (adaptation panel): {c['desc']}")
    return "\n".join(lines) or "(no images - use \"generate\" for the key moments)"


def events_text(notes: dict) -> str:
    return "\n".join(
        f"{e['id']} [w{e['weight']}]{' (' + ', '.join(e['chars']) + ')' if e.get('chars') else ''}: {e['desc']}" for e in notes.get("events", [])
    )


def write_book_chapter(notes: dict, related: list[dict], position: str, story_so_far: str, prev_context: str,
                       words: int, bible: Bible, writer: Chain, cfg: Config, path: Path) -> list[dict]:  # fmt: skip
    if path.exists():
        return json.loads(path.read_text("utf-8"))["beats"]
    combined = f"EVENTS:\n{events_text(notes)}\n\nIMAGE LIBRARY (label: what it shows):\n{library_text(notes, related)}"
    user = prompts.writer_user(
        language=cfg.lang, target_words=words, bible_text=bible.text(), story_so_far=story_so_far, prev_context=prev_context,
        chapter_title=notes["title"], notes_text=combined, position=position, extra=cfg.extra_instructions,
    )  # fmt: skip
    console.print(f"  writing {notes['cid']} (~{words} words)")
    res = writer.complete_json(prompts.BOOK_WRITER_SYSTEM, [user])
    beats = []
    for b in res.get("beats") or []:
        text = _clean_text(b.get("text", ""))
        if not text:
            continue
        refs = [str(x).strip().upper() for x in (b.get("panels") or [])][:3]
        gen = _clean_text(b.get("generate") or "")
        emphasis = b.get("emphasis") if b.get("emphasis") in ("normal", "hit", "reveal") else "normal"
        beats.append({"text": text, "refs": refs, "generate": gen, "chars": b.get("chars") or [], "emphasis": emphasis})
    if not beats:
        raise RuntimeError(f"writer returned no usable beats for {notes['cid']}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"beats": beats}, ensure_ascii=False, indent=1), "utf-8")
    return beats


def resolve_pictures(notes: dict, related: list[dict], raw: list[dict], xref: XrefStore) -> list[dict]:
    """Labels -> panel ids (importing adaptation panels). Beats keep "generate" when they have no picture."""
    rel = {c["label"]: c for c in related}
    out = []
    for b in raw:
        ids = []
        for lab in b["refs"]:
            if lab in notes["labels"]:
                ids.append(notes["labels"][lab])
            elif lab in rel:
                pid = xref.get(rel[lab]["src"], rel[lab]["pid"])
                if pid:
                    ids.append(pid)
        beat = {"text": b["text"], "panels": list(dict.fromkeys(ids)), "emphasis": b["emphasis"], "chars": b.get("chars") or []}
        if b.get("generate") and not ids:
            beat["generate"] = b["generate"]
        out.append(beat)
    return out


# --------------------------------------------------------------- pictures


class Gallery:
    """Generated images (panels/gen/) + fallback title cards (panels/card/)."""

    def __init__(self, work: Path, cfg: Config):
        self.work, self.cfg = work, cfg
        self.dir = work / "panels" / "gen"
        self.index = self.dir / "panels.json"
        self.items = json.loads(self.index.read_text("utf-8")) if self.index.exists() else []
        self.by_key = {it["key"]: it for it in self.items}
        self.budget = cfg.images.max_generated
        ok, why = imagegen.available(cfg.images.generator)
        self.enabled, self.why = ok, why

    def _save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index.write_text(json.dumps(self.items, ensure_ascii=False, indent=1), "utf-8")

    def generate(self, prompt: str, refs: list[Path], cached_only: bool = False) -> str | None:
        spec = self.cfg.images.generator
        full = f"{self.cfg.images.style}. {prompt}"
        key = imagegen.cache_key(spec, full, refs)
        if key in self.by_key and (self.work / self.by_key[key]["file"]).exists():
            return self.by_key[key]["id"]
        if cached_only:
            return None
        if not self.enabled:
            imagegen.warn_once(f"image generation unavailable: {self.why} - using the nearest real image instead")
            return None
        if self.budget <= 0:
            imagegen.warn_once(f"generated-image budget ({self.cfg.images.max_generated}) used up - reusing real images")
            return None
        n = len(self.items) + 1
        rel = Path("panels") / "gen" / f"p{n:04d}.png"
        try:
            imagegen.generate(spec, full, refs, self.work / rel)
        except imagegen.ImageGenError as e:
            msg = str(e).lower()
            if any(k in msg for k in ("credit", "quota", "billing", "402", "429", "exhausted", "api key", "permission")):
                self.enabled, self.why = False, f"{spec} unavailable ({str(e)[:160]})"  # stop trying for this run
            imagegen.warn_once(f"image generation failed: {str(e)[:200]}")
            return None
        img = _trim_letterbox(imread(self.work / rel))
        imwrite(self.work / rel.with_suffix(".jpg"), img, quality=93)
        (self.work / rel).unlink(missing_ok=True)
        rel = rel.with_suffix(".jpg")
        item = {"id": f"gen-p{n:04d}", "chapter": "gen", "file": rel.as_posix(), "w": int(img.shape[1]), "h": int(img.shape[0]),
                "page": None, "key": key, "prompt": prompt, "refs": [str(r) for r in refs]}  # fmt: skip
        self.items.append(item)
        self.by_key[key] = item
        self.budget -= 1
        self._save()
        return item["id"]

    def title_card(self, title: str) -> str:
        """Last-resort picture: the chapter title on a dark gradient."""
        d = self.work / "panels" / "card"
        idx = d / "panels.json"
        items = json.loads(idx.read_text("utf-8")) if idx.exists() else []
        for it in items:
            if it.get("title") == title:
                return it["id"]
        from PIL import Image, ImageDraw, ImageFont

        W, H = 1600, 900
        img = Image.new("RGB", (W, H))
        px = np.zeros((H, W, 3), np.uint8)
        px[:] = np.linspace(28, 8, H, dtype=np.uint8)[:, None, None]
        img = Image.fromarray(px)
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("georgiab.ttf", 72)
        except OSError:
            font = ImageFont.load_default()
        words, lines, cur = title.split(), [], ""
        for w in words:
            if draw.textlength(f"{cur} {w}".strip(), font=font) > W * 0.8:
                lines.append(cur)
                cur = w
            else:
                cur = f"{cur} {w}".strip()
        lines.append(cur)
        y = H / 2 - len(lines) * 45
        for line in lines:
            draw.text(((W - draw.textlength(line, font=font)) / 2, y), line, fill=(235, 230, 220), font=font)
            y += 90
        n = len(items) + 1
        rel = Path("panels") / "card" / f"p{n:04d}.jpg"
        (self.work / rel).parent.mkdir(parents=True, exist_ok=True)
        img.save(self.work / rel, "JPEG", quality=92)
        items.append({"id": f"card-p{n:04d}", "chapter": "card", "file": rel.as_posix(), "w": W, "h": H, "page": None, "title": title})
        idx.write_text(json.dumps(items, ensure_ascii=False, indent=1), "utf-8")
        return items[-1]["id"]


def _trim_letterbox(img: np.ndarray, tol: float = 10.0) -> np.ndarray:
    """Remove uniform black/white bars some image models add around a 16:9 picture."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    rows = np.flatnonzero((gray.std(axis=1) > tol) | (np.abs(gray.mean(axis=1) - gray[gray.shape[0] // 2].mean()) < 60))
    cols = np.flatnonzero((gray.std(axis=0) > tol) | (np.abs(gray.mean(axis=0) - gray[:, gray.shape[1] // 2].mean()) < 60))
    if rows.size < gray.shape[0] * 0.5 or cols.size < gray.shape[1] * 0.5:
        return img
    return img[rows[0] : rows[-1] + 1, cols[0] : cols[-1] + 1]


def _panel_path(work: Path, pid: str) -> Path | None:
    cid = pid.split("-")[0]
    idx = work / "panels" / cid / "panels.json"
    if not idx.exists():
        return None
    item = next((p for p in json.loads(idx.read_text("utf-8")) if p["id"] == pid), None)
    return work / item["file"] if item else None


def fill_pictures(chapters_out: list[dict], all_notes: list[dict], bible: Bible, project: "Project") -> dict:
    """Give every beat a picture.

    1. beats that asked for one get a generated image (with official-art references); the generation budget
       is spread over chapters in proportion to how many pictures each asked for
    2. other empty beats take a picture from their chapter's pool (its illustrations, adaptation panels and
       pictures used elsewhere in it) - the one used closest in the story, avoiding back-to-back repeats
    3. a chapter with no pictures at all carries the previous chapter's last one; a title card is the last resort
    """
    work, cfg = project.work, project.cfg
    gallery = Gallery(work, cfg)
    notes_by = {n["cid"]: n for n in all_notes}
    chars_of: dict[str, set[str]] = {}
    for n in all_notes:
        for lab, info in n["panels"].items():
            chars_of[n["labels"][lab]] = {c.lower() for c in info.get("chars") or []}
    ref_pool: list[tuple[Path, set[str]]] = []
    seen: set[str] = set()
    for ch in chapters_out:
        for b in ch["beats"]:
            for pid in b["panels"]:
                if pid not in seen and (p := _panel_path(work, pid)):
                    seen.add(pid)
                    ref_pool.append((p, chars_of.get(pid, set()) | {c.lower() for c in b.get("chars") or []}))
    for n in all_notes:  # unused book illustrations are great style references too
        for lab, info in n["panels"].items():
            pid = n["labels"][lab]
            if pid not in seen and info.get("story", True) and (p := _panel_path(work, pid)):
                seen.add(pid)
                ref_pool.append((p, {c.lower() for c in info.get("chars") or []}))

    # 1. generation, budget spread across chapters
    asked = [sum(1 for b in ch["beats"] if not b["panels"] and b.get("generate")) for ch in chapters_out]
    total_asked = sum(asked)
    quota = [0] * len(chapters_out)
    if total_asked and gallery.enabled:
        budget = min(gallery.budget, total_asked)
        quota = [min(a, int(budget * a / total_asked)) for a in asked]
        for i in sorted(range(len(asked)), key=lambda i: -asked[i]):  # hand out the rounding remainder
            if sum(quota) >= budget:
                break
            if quota[i] < asked[i]:
                quota[i] += 1
    stats = {"generated": 0, "reused": 0, "cards": 0}
    done = 0
    for ci, ch in enumerate(chapters_out):
        want = [b for b in ch["beats"] if not b["panels"] and b.get("generate")]
        if not want:
            continue
        # spread this chapter's quota evenly over its requests (not just the first ones)
        step = len(want) / quota[ci] if quota[ci] else 0
        picks = {int(k * step) for k in range(quota[ci])} if quota[ci] else set()
        for k, b in enumerate(want):
            done += 1
            progress.emit("script", None, None, f"pictures {done}/{total_asked}")
            cached_only = k not in picks
            who = {c.lower() for c in b.get("chars") or []}
            refs = sorted(ref_pool, key=lambda pc: -len(pc[1] & who))[: max(0, cfg.images.refs)]
            looks = "; ".join(f"{c}: {bible.characters[c]['desc']}" for c in b.get("chars") or [] if c in bible.characters)
            prompt = f"Scene: {b['generate']}" + (f" Characters - {looks}" if looks else "")
            pid = gallery.generate(prompt, [p for p, _ in refs], cached_only=cached_only)
            if pid:
                b["panels"] = [pid]
                stats["generated"] += 1

    # 2./3. fill the rest from the chapter's own pool
    carry: str | None = None
    for ch in chapters_out:
        beats = ch["beats"]
        positions: dict[str, list[int]] = {}
        for i, b in enumerate(beats):
            for pid in b["panels"]:
                positions.setdefault(pid, []).append(i)
        n = notes_by.get(ch["cid"])
        for lab, info in (n["panels"].items() if n else []):
            if info.get("story", True):
                positions.setdefault(n["labels"][lab], [])  # unused illustration: fair game anywhere
        pool = list(positions)
        prev: str | None = carry
        for i, b in enumerate(beats):
            if b["panels"]:
                prev = b["panels"][-1]
                continue
            if pool:
                def dist(pid: str) -> float:
                    pos = positions[pid]
                    d = min(abs(p - i) for p in pos) if pos else len(beats) / 2
                    return d + (100 if pid == prev and len(pool) > 1 else 0)  # avoid back-to-back repeats

                pid = min(pool, key=dist)
                positions[pid].append(i)
                stats["reused"] += 1
            elif carry:
                pid = carry
                stats["reused"] += 1
            else:
                pid = gallery.title_card(ch["title"])
                stats["cards"] += 1
                pool = [pid]
                positions[pid] = [i]
            b["panels"] = [pid]
            prev = pid
        last = next((b["panels"][-1] for b in reversed(beats) if b["panels"]), None)
        carry = last or carry
    stats["generator"] = "on" if gallery.enabled else f"off ({gallery.why})"
    return stats


def hook_from_beats(chapters_out: list[dict], bible: Bible, writer: Chain, cfg: Config, path: Path) -> list[dict]:
    valid = {pid for c in chapters_out for b in c["beats"] for pid in b["panels"] if not pid.startswith("card-")}
    if path.exists():  # reuse only if every picture still exists in the current script
        cached = json.loads(path.read_text("utf-8"))["beats"]
        if cached and all(pid in valid for b in cached for pid in b["panels"]):
            return cached
    synopsis = "\n".join(f"{c['title']}: {' '.join(b['text'] for b in c['beats'])[:600]}" for c in chapters_out)
    used = []
    for c in chapters_out:
        for b in c["beats"]:
            for pid in b["panels"][:1]:
                if pid in valid:
                    used.append(f"{pid}: {b['text'][:140]}")
    user = f"{prompts.STYLE_GUIDE}\n\nWRITE IN: {prompts.lang_name(cfg.lang)}.\n\nSTORY BIBLE:\n{bible.text()}\n\nSCRIPT SO FAR (by chapter):\n{synopsis[:14000]}\n\nUSED IMAGES (id: the line it illustrates):\n" + "\n".join(used[:150])
    res = writer.complete_json(prompts.BOOK_HOOK_SYSTEM.replace("{WORDS}", str(cfg.hook_words)), [user])
    beats, total = [], 0
    for b in res.get("beats") or []:
        text = _clean_text(b.get("text", ""))
        ids = list(dict.fromkeys(p for p in (b.get("panels") or []) if p in valid))[:2]
        if not text or not ids:
            continue
        n = len(text.split())
        if beats and total + n > cfg.hook_words * 1.2:
            break
        beats.append({"text": text, "panels": ids, "emphasis": b.get("emphasis") if b.get("emphasis") in ("normal", "hit", "reveal") else "hit"})
        total += n
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"beats": beats}, ensure_ascii=False, indent=1), "utf-8")
    return beats


def events_weight(notes: dict) -> int:
    return sum(e["weight"] for e in notes.get("events", [])) or 1


def previous_events(all_notes: list[dict], i: int, k: int = 3) -> str:
    if i == 0:
        return ""
    return " / ".join(e["desc"] for e in all_notes[i - 1].get("events", [])[-k:])


def book_script(project: "Project", reader: Chain, writer: Chain, key: str, until_read: bool) -> dict[str, Any] | None:
    """Whole book flow up to the assembled script (the caller writes script.json)."""
    from .story import positions, story_before

    cfg, work = project.cfg, project.work
    every_note, bible = book_read(project, reader, writer)
    all_notes = [n for n in every_note if n.get("story", True)] or every_note
    skipped = [n["title"] for n in every_note if n not in all_notes]
    if skipped:
        console.print(f"  skipping non-story sections: {', '.join(skipped)}")
    related = related_library(project, all_notes, writer)
    if until_read:
        return None

    t0 = time.time()
    console.print(f"[bold]writing[/] ({cfg.lang}) with {writer.describe()} ({cfg.jobs} parallel)")
    if cfg.target_minutes:
        total = max(150, int(cfg.target_minutes * cfg.words_per_minute) - (cfg.hook_words if cfg.hook else 0))
        weights = [events_weight(n) for n in all_notes]
        targets = [max(60, int(total * w / sum(weights))) for w in weights]
    else:
        targets = [max(60, int(cfg.minutes_per_chapter * cfg.words_per_minute))] * len(all_notes)
    pos = positions(len(all_notes))
    rel_key = hashlib.sha1(json.dumps({n["cid"]: [c["pid"] for c in related.get(n["cid"], [])] for n in all_notes}).encode()).hexdigest()[:6]
    with ThreadPoolExecutor(max(1, cfg.jobs)) as pool:
        futs = [
            pool.submit(write_book_chapter, n, related.get(n["cid"], []), position, story_before(all_notes, i), previous_events(all_notes, i),
                        words, bible, writer, cfg, work / "script" / f"{n['cid']}_{position}_{words}_{cfg.lang}_{key}_{rel_key}.json")  # fmt: skip
            for i, (n, words, position) in enumerate(zip(all_notes, targets, pos))
        ]
        for k, f in enumerate(as_completed(futs), start=1):
            f.result()
            progress.emit("script", k, len(futs) + 1, "written")
        raws = [f.result() for f in futs]
    xref = XrefStore(work)
    chapters_out = [{"cid": n["cid"], "title": n["title"], "beats": resolve_pictures(n, related.get(n["cid"], []), raw, xref)}
                    for n, raw in zip(all_notes, raws)]  # fmt: skip
    asked = sum(1 for c in chapters_out for b in c["beats"] if b.get("generate"))
    console.print(f"  wrote {len(chapters_out)} chapters in {time.time() - t0:.0f}s; {asked} beat(s) asked for a generated picture")
    stats = fill_pictures(chapters_out, all_notes, bible, project)
    console.print(f"  pictures: {stats['generated']} generated, {stats['reused']} reused, {stats['cards']} title cards - generator {stats['generator']}")
    hook = hook_from_beats(chapters_out, bible, writer, cfg, work / "script" / f"hook_book_{key}_{rel_key}_{cfg.hook_words}.json") if cfg.hook else []
    progress.emit("script", len(futs) + 1, len(futs) + 1, "written")
    return {"hook": hook, "chapters": chapters_out}
