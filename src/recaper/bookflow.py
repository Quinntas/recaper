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
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

import cv2
import numpy as np

from . import imagegen, progress, prompts, webrefs
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


def _volume(n: dict) -> str:
    m = re.search(r"vol(?:ume)?[\s\-_.]*(\d+)", str(n.get("volume") or ""), re.I)
    return f" [LN volume {m.group(1)}]" if m else ""


def related_library(project: "Project", all_notes: list[dict], writer: Chain, extra: list[Path] | None = None) -> dict[str, list[dict]]:
    """book cid -> candidate images from linked adaptations and online references:
    [{label, src, pid, desc, chars, weight, kind}]."""
    sources = _source_projects(project) + list(extra or [])
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
            book_lines = "\n".join(f"{n['cid']}{_volume(n)}: {n['title']} - {' '.join(n['summary'].split()[:60])}" for n in chunk)
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
                    cands.append({"src": sn["_src"], "pid": sn["labels"][lab], "desc": info["desc"], "chars": info.get("chars") or [],
                                  "weight": info["weight"], "kind": "anime still" if sn.get("kind") == "anime" else "adaptation panel"})  # fmt: skip
        cands.sort(key=lambda c: -c["weight"])
        # R* = linked adaptation panels, A* = anime stills: separate numbering, so adding online references never
        # renames the labels of chapters that are already written
        linked = [c for c in cands if c["kind"] != "anime still"][:cap]
        stills = [c for c in cands if c["kind"] == "anime still"][:cap]
        for j, c in enumerate(linked, start=1):
            c["label"] = f"R{j}"
        for j, c in enumerate(stills, start=1):
            c["label"] = f"A{j}"
        out[n["cid"]] = linked + stills
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
        lines.append(f"{c['label']} ({c.get('kind', 'adaptation panel')}): {c['desc']}")
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
        known=cfg.known_story == "yes",
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


def repick_pictures(notes: dict, related: list[dict], raw: list[dict], writer: Chain, cache_dir: Path) -> list[dict]:
    """Picture pass for an already-written chapter: beats without a real picture (they asked for a generated
    one) get a fitting image from the library - now with the online references - without rewriting the text."""
    needs = [i for i, b in enumerate(raw) if not b["refs"]]
    pool = [c for c in related if c.get("kind") == "anime still"]
    if not needs or not pool:
        return raw
    library = library_text(notes, related)
    key = hashlib.sha1(json.dumps([[b["text"] for b in raw], needs, [c["pid"] for c in related]]).encode()).hexdigest()[:12]
    path = cache_dir / f"{notes['cid']}_pics_{key}.json"
    if path.exists():
        picks = json.loads(path.read_text("utf-8"))
    else:
        lines = [f"{i + 1}.{' [needs picture]' if i in needs else ' (has: ' + ','.join(b['refs']) + ')'} {b['text']}" for i, b in enumerate(raw)]
        try:
            res = writer.complete_json(prompts.PICTURE_SYSTEM, [f"CHAPTER: {notes['title']}\n\nBEATS:\n" + "\n".join(lines) +
                                                                f"\n\nIMAGE LIBRARY (label: what it shows):\n{library}"])  # fmt: skip
        except Exception as e:  # noqa: BLE001 - the beats keep their generate request
            console.print(f"[yellow]{notes['cid']}: picture pass failed ({e})[/]")
            return raw
        valid = set(notes["labels"]) | {c["label"] for c in related}
        picks = {}
        for k, labs in (res.get("picks") or {}).items():
            m = re.search(r"\d+", str(k))
            if m and isinstance(labs, list):
                picks[str(int(m.group()) - 1)] = [x for x in (str(v).strip().upper() for v in labs) if x in valid][:2]
        cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(picks), "utf-8")
    out = []
    for i, b in enumerate(raw):
        labs = picks.get(str(i)) if i in needs else None
        out.append(b | {"refs": labs} if labs else b)
    return out


# --------------------------------------------------------------- pictures


class Gallery:
    """Generated images (panels/gen/) + fallback title cards (panels/card/)."""

    def __init__(self, work: Path, cfg: Config, checker: Chain | None = None):
        self.work, self.cfg = work, cfg
        self.dir = work / "panels" / "gen"
        self.index = self.dir / "panels.json"
        self.items = json.loads(self.index.read_text("utf-8")) if self.index.exists() else []
        self.by_key = {it["key"]: it for it in self.items}
        self.rejected_file = self.dir / "rejected.json"
        self.rejected: dict = json.loads(self.rejected_file.read_text("utf-8")) if self.rejected_file.exists() else {}
        self.checker = checker if cfg.images.quality_check else None
        self.qc_stats = {"passed": 0, "redone": 0, "dropped": 0}
        self.counts = {"cached": 0, "attempts": 0, "first_tries": 0}
        self.budget = cfg.images.max_generated
        ok, why = imagegen.available(cfg.images.generator)
        self.enabled, self.why = ok, why
        self._lock = threading.Lock()  # generate() runs in parallel threads
        self._next = 1 + max((int(it["id"].rsplit("p", 1)[-1]) for it in self.items if it["id"].startswith("gen-p")), default=0)

    def _save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index.write_text(json.dumps(self.items, ensure_ascii=False, indent=1), "utf-8")

    def check(self, image: Path, prompt: str, refs: list[Path], ref_notes: list[str]) -> dict:
        """Vision QC: {"score": 1-10, "problems": [...]}; a failed check passes the image (never blocks)."""
        parts: list = [f"SCENE THE IMAGE MUST SHOW:\n{prompt}\n\nREFERENCE IMAGES:"]
        for i, r in enumerate(refs):
            parts += [f"[reference {i + 1}: {ref_notes[i] if i < len(ref_notes) else 'official art'}]", ImagePart(r)]
        parts += ["[GENERATED IMAGE - judge this one]", ImagePart(image)]
        try:
            res = self.checker.complete_json(prompts.QC_SYSTEM, parts)
            score = float(res.get("score", 0) or 0)
            return {"score": score, "problems": [str(p) for p in (res.get("problems") or [])][:6]}
        except Exception as e:  # noqa: BLE001
            return {"score": 10.0, "problems": [], "error": str(e)[:160]}

    def generate(self, prompt: str, refs: list[Path], cached_only: bool = False, ref_notes: list[str] | None = None,
                 fresh: bool = False) -> str | None:
        """Generated panel id for this prompt + references (cached), or None (the caller keeps a real picture).
        fresh: draw it again even if cached or rejected before (the old picture is replaced on success)."""
        spec = self.cfg.images.generator
        full = f"{self.cfg.images.style}. {prompt}"
        key = imagegen.cache_key(spec, full, refs)
        with self._lock:  # reserve a slot: id + budget
            if not fresh and key in self.by_key and (self.work / self.by_key[key]["file"]).exists():
                self.counts["cached"] += 1
                return self.by_key[key]["id"]
            if (key in self.rejected and not fresh) or cached_only:  # failed the quality check twice before: use a real picture
                return None
            if not self.enabled:
                imagegen.warn_once(f"image generation unavailable: {self.why} - using the nearest real image instead")
                return None
            if self.budget <= 0 and not fresh:
                imagegen.warn_once(f"generated-image budget ({self.cfg.images.max_generated}) used up - reusing real images")
                return None
            n, self._next = self._next, self._next + 1
            self.budget -= 1
        rel = Path("panels") / "gen" / f"p{n:04d}.png"
        ask, qc, tries = full, None, (2 if self.checker else 1)
        for attempt in range(tries):
            try:
                with self._lock:
                    self.counts["attempts"] += 1
                    self.counts["first_tries"] += attempt == 0
                imagegen.generate(spec, ask, refs, self.work / rel, ref_notes=ref_notes, quality=self.cfg.images.quality,
                                  price_override=self.cfg.images.price)
            except imagegen.ImageGenError as e:
                msg = str(e).lower()
                with self._lock:
                    self.budget += 1
                    if any(k in msg for k in ("credit", "quota", "billing", "402", "429", "exhausted", "api key", "permission", "not installed", "sign in")):
                        self.enabled, self.why = False, f"{spec} unavailable ({str(e)[:160]})"  # stop trying for this run
                imagegen.warn_once(f"image generation failed: {str(e)[:200]}")
                return None
            img = _trim_letterbox(imread(self.work / rel))
            imwrite(self.work / rel.with_suffix(".jpg"), img, quality=93)
            (self.work / rel).unlink(missing_ok=True)
            if not self.checker:
                break
            qc = self.check(self.work / rel.with_suffix(".jpg"), prompt, refs, ref_notes or [])
            if qc["score"] >= self.cfg.images.min_score:
                with self._lock:
                    self.qc_stats["passed"] += 1
                break
            console.print(f"  [dim]image {n} scored {qc['score']:.0f}/10: {'; '.join(qc['problems'])[:160]}[/]")
            if attempt + 1 < tries:  # redo once, telling the generator what went wrong
                with self._lock:
                    self.qc_stats["redone"] += 1
                ask = f"{full}\n\nA previous attempt was rejected for: {'; '.join(qc['problems']) or 'poor quality'}. Avoid these problems."
        else:
            (self.work / rel.with_suffix(".jpg")).unlink(missing_ok=True)
            with self._lock:
                self.qc_stats["dropped"] += 1
                self.rejected[key] = {"prompt": prompt, "qc": qc}
                self.dir.mkdir(parents=True, exist_ok=True)
                self.rejected_file.write_text(json.dumps(self.rejected, ensure_ascii=False, indent=1), "utf-8")
            return None
        rel = rel.with_suffix(".jpg")
        item = {"id": f"gen-p{n:04d}", "chapter": "gen", "file": rel.as_posix(), "w": int(img.shape[1]), "h": int(img.shape[0]),
                "page": None, "key": key, "prompt": prompt, "refs": [str(r) for r in refs], **({"qc": qc} if qc else {})}  # fmt: skip
        with self._lock:
            for old in [it for it in self.items if it["key"] == key]:  # a redraw replaces the previous picture
                self.items.remove(old)
                (self.work / old["file"]).unlink(missing_ok=True)
            if self.rejected.pop(key, None) is not None:
                self.rejected_file.write_text(json.dumps(self.rejected, ensure_ascii=False, indent=1), "utf-8")
            self.items.append(item)
            self.by_key[key] = item
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

    def bar(lines: np.ndarray) -> np.ndarray:  # flat and near black or near white
        mean = lines.mean(axis=1)
        return (lines.std(axis=1) < tol) & ((mean < 32) | (mean > 223))

    def span(is_bar: np.ndarray) -> tuple[int, int]:
        a, b = 0, len(is_bar)
        while a < b and is_bar[a]:
            a += 1
        while b > a and is_bar[b - 1]:
            b -= 1
        return a, b

    (r0, r1), (c0, c1) = span(bar(gray)), span(bar(gray.T))
    if r1 - r0 < gray.shape[0] * 0.5 or c1 - c0 < gray.shape[1] * 0.5:
        return img
    return img[r0:r1, c0:c1]


def _panel_path(work: Path, pid: str) -> Path | None:
    cid = pid.split("-")[0]
    idx = work / "panels" / cid / "panels.json"
    if not idx.exists():
        return None
    item = next((p for p in json.loads(idx.read_text("utf-8")) if p["id"] == pid), None)
    return work / item["file"] if item else None


def _colourful(path: Path, _memo: dict = {}) -> bool:  # noqa: B006 - per-process memo
    """Colour art (vs the black-and-white interior illustrations): only colour makes a good style reference."""
    if path not in _memo:
        img = cv2.imread(str(path), cv2.IMREAD_REDUCED_COLOR_4)
        _memo[path] = img is not None and float(cv2.cvtColor(img, cv2.COLOR_BGR2HSV)[..., 1].mean()) > 35
    return _memo[path]


def fill_real_pictures(chapters_out: list[dict], all_notes: list[dict], project: "Project") -> dict:
    """Script stage: give every beat a real picture.

    Empty beats take a picture from their chapter's pool (its illustrations, adaptation panels / anime stills and
    pictures used elsewhere in it) - the one used closest in the story, avoiding back-to-back repeats. A chapter
    with no pictures at all carries the previous chapter's last one; a title card is the last resort.
    Beats that asked for a generated picture keep their "generate" request: the images stage (generate_pictures)
    swaps their picture for a generated one, and this real one stays as its fallback."""
    work, cfg = project.work, project.cfg
    gallery = Gallery(work, cfg)
    notes_by = {n["cid"]: n for n in all_notes}
    stats = {"reused": 0, "cards": 0, "wanted": sum(1 for c in chapters_out for b in c["beats"] if b.get("generate"))}
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
    return stats


class _References:
    """Picks the reference images for one generation: the official design of every character in the scene
    (online references), then colour frames of the same arc (anime stills of the episodes matched to the chapter)
    for the art style; the book's colour illustrations when nothing else exists."""

    def __init__(self, work: Path, cfg: Config, bible: Bible, all_notes: list[dict], chapters: list[dict], arc_stills: dict[str, list[str]]):
        self.cfg, self.bible, self.arc_stills = cfg, bible, arc_stills
        chars_of: dict[str, set[str]] = {}
        for n in all_notes:
            for lab, info in n["panels"].items():
                chars_of[n["labels"][lab]] = {c.lower() for c in info.get("chars") or []}
        pool: list[tuple[Path, set[str]]] = []
        seen: set[str] = set()
        for ch in chapters:
            for b in ch["beats"]:
                for pid in b.get("fallback") or b["panels"]:
                    if pid not in seen and not pid.startswith(("gen-", "card-")) and (p := _panel_path(work, pid)):
                        seen.add(pid)
                        pool.append((p, chars_of.get(pid, set()) | {c.lower() for c in b.get("chars") or []}))
        for n in all_notes:  # unused book illustrations are style references too
            for lab, info in n["panels"].items():
                pid = n["labels"][lab]
                if pid not in seen and info.get("story", True) and (p := _panel_path(work, pid)):
                    seen.add(pid)
                    pool.append((p, {c.lower() for c in info.get("chars") or []}))
        self.colour_pool = [rp for rp in pool if _colourful(rp[0])] or pool
        self.designs = webrefs.character_refs(work)
        self.alias = {a.lower(): name.lower() for name, info in bible.characters.items() for a in [name, *(info.get("aliases") or [])]}
        self.frames = webrefs.stills(work)

    def design(self, name: str) -> Path | None:
        return self.designs.get(self.alias.get(name.lower(), name.lower()))

    def cast(self, b: dict) -> list[str]:
        """The beat's characters + bible names its scene description mentions."""
        names = list(dict.fromkeys(b.get("chars") or []))
        text = b.get("generate") or ""
        for a, name in self.alias.items():
            if len(a) > 2 and re.search(rf"\b{re.escape(a)}\b", text, re.I):
                full = next((n for n in self.bible.characters if n.lower() == name), name)
                if full not in names and not any(self.alias.get(x.lower()) == name for x in names):
                    names.append(full)
        return names[:4]

    def pick(self, cid: str, b: dict) -> tuple[list[Path], list[str], list[str]]:
        """(reference images, what each one is, characters without a design)."""
        who = self.cast(b)
        who_l = {self.alias.get(c.lower(), c.lower()) for c in who} | {c.lower() for c in who}
        refs: list[Path] = []
        notes: list[str] = []
        cap = max(0, self.cfg.images.refs)
        for c in who:
            d = self.design(c)
            if d and d not in refs and len(refs) < max(1, cap - 1):
                refs.append(d)
                notes.append(f"{c}'s official character design (draw {c} exactly like this, at the age the scene says)")
        arc = [self.frames[pid] for pid in self.arc_stills.get(cid, []) if pid in self.frames]
        if arc or self.frames:
            style = sorted(arc or list(self.frames.values()), key=lambda pc: -len(pc[1] & who_l))
            note = "a frame from the anime adaptation - match its art style, colouring and how these characters look at this point of the story"
        else:
            style = sorted(self.colour_pool, key=lambda pc: -len(pc[1] & who_l))
            note = "official art of this work - match its art style"
        for p, _ in style:
            if len(refs) >= cap:
                break
            if p not in refs:
                refs.append(p)
                notes.append(note)
        missing = [c for c in who if not self.design(c) and c in self.bible.characters]
        return refs, notes, missing

    def prompt(self, b: dict, missing: list[str]) -> str:
        # narrative bible notes ("formerly a 34-year-old shut-in") mislead image models: only for characters without a design
        looks = "; ".join(f"{c}: {self.bible.characters[c]['desc'][:200]}" for c in missing)
        return f"Scene: {b['generate']}" + (f" Characters - {looks}" if looks else "")


def _real_fallback(beats: list[dict], i: int) -> list[str]:
    """A real picture for beat i when the script has none recorded (scripts made before the images stage)."""
    for d in range(1, len(beats)):
        for j in (i - d, i + d):
            if 0 <= j < len(beats):
                real = [p for p in (beats[j].get("fallback") or beats[j]["panels"]) if not p.startswith("gen-")]
                if real:
                    return real[:1]
    return []


def generate_pictures(project: "Project", script: dict, checker: Chain | None = None) -> dict:
    """Images stage (books): beats that asked for a generated picture get one, in place of their real fallback.

    The generation budget (images.max_generated) is spread over chapters in proportion to how many pictures each
    asked for, and over each chapter's requests evenly. Results are cached by prompt + references, so re-running
    only makes what is missing (removed or never made). With quality_check on, the reader scores each result:
    bad ones are redone once with the problems listed, then dropped (the beat keeps its real picture)."""
    work, cfg = project.work, project.cfg
    chapters = script.get("chapters") or []
    bible = Bible()
    if (work / "bible.json").exists():
        data = json.loads((work / "bible.json").read_text("utf-8"))
        bible.characters, bible.terms = data.get("characters", {}), data.get("terms", {})
    all_notes = [json.loads(f.read_text("utf-8")) for c in chapters if (f := work / "notes" / f"{c['cid']}.json").exists()]
    ctx_file = work / "pictures_ctx.json"
    arc_stills = json.loads(ctx_file.read_text("utf-8")).get("arc_stills", {}) if ctx_file.exists() else {}
    gallery = Gallery(work, cfg, checker)

    # every request starts from its real picture, so generated ones can be swapped in and out
    want_by_ch: list[list[dict]] = []
    for ch in chapters:
        want = []
        for i, b in enumerate(ch["beats"]):
            if not b.get("generate"):
                continue
            if "fallback" not in b:
                b["fallback"] = [p for p in b["panels"] if not p.startswith("gen-")] or _real_fallback(ch["beats"], i)
            want.append(b)
        want_by_ch.append(want)
    asked = [len(w) for w in want_by_ch]
    total_asked = sum(asked)
    quota = [0] * len(chapters)
    if total_asked and gallery.enabled:
        budget = min(gallery.budget, total_asked)
        quota = [min(a, int(budget * a / total_asked)) for a in asked]
        for i in sorted(range(len(asked)), key=lambda i: -asked[i]):  # hand out the rounding remainder
            if sum(quota) >= budget:
                break
            if quota[i] < asked[i]:
                quota[i] += 1
    picker = _References(work, cfg, bible, all_notes, chapters, arc_stills)
    tasks = []
    for ci, (ch, want) in enumerate(zip(chapters, want_by_ch)):
        step = len(want) / quota[ci] if quota[ci] else 0  # spread the quota over the chapter, not just its first requests
        picks = {int(k * step) for k in range(quota[ci])} if quota[ci] else set()
        for k, b in enumerate(want):
            refs, ref_notes, missing = picker.pick(ch["cid"], b)
            fresh = bool(b.pop("regen", False))  # asked from the Generated tab: draw it again, outside the quota
            tasks.append((b, picker.prompt(b, missing), refs, ref_notes, k not in picks and not fresh, fresh))

    def make(task):
        b, prompt, refs, ref_notes, cached_only, fresh = task
        return b, gallery.generate(prompt, refs, cached_only=cached_only, ref_notes=ref_notes, fresh=fresh)

    stats = {"asked": total_asked, "generated": 0, "fallback": 0}
    console.print(f"[bold]images[/]: {total_asked} beat(s) want a generated picture, budget {cfg.images.max_generated} "
                  f"({cfg.images.generator}{', quality check on' if gallery.checker else ''})")  # fmt: skip
    progress.emit("images", 0, max(1, len(tasks)), "generating pictures")
    # image APIs and the Antigravity agent are slow per image (10-70 s) but take parallel requests
    with ThreadPoolExecutor(max(1, min(4, cfg.jobs))) as pool:
        for done, (b, pid) in enumerate(pool.map(make, tasks), start=1):
            progress.emit("images", done, len(tasks), f"pictures {done}/{len(tasks)}")
            if pid:
                b["panels"] = [pid]
                stats["generated"] += 1
            else:
                b["panels"] = list(b["fallback"]) or b["panels"]
                stats["fallback"] += 1
    live = {it["id"] for it in gallery.items}
    for ch in chapters:  # pictures removed (redrawn, deleted, --force images) leave no beat pointing at them
        for i, b in enumerate(ch["beats"]):
            if any(p.startswith("gen-") and p not in live for p in b["panels"]):
                b["panels"] = list(b.get("fallback") or []) or _real_fallback(ch["beats"], i) or b["panels"]
    stats["generator"] = "on" if gallery.enabled else f"off ({gallery.why})"
    stats["qc"] = gallery.qc_stats if gallery.checker else None
    stats["cached"] = gallery.counts["cached"]
    from . import usage

    # what the cost estimate learns from: requests per chapter, cache hits, attempts and the redo rate
    usage.record("pictures", chapters=len(chapters), asked=total_asked, quota=sum(quota), cached=gallery.counts["cached"],
                 new=stats["generated"] - gallery.counts["cached"], attempts=gallery.counts["attempts"],
                 first_tries=gallery.counts["first_tries"], redone=gallery.qc_stats["redone"], dropped=gallery.qc_stats["dropped"],
                 sig=usage.image_setup_sig(cfg), generator=cfg.images.generator if gallery.enabled else "off")  # fmt: skip
    qc = stats["qc"]
    console.print(f"  pictures: {stats['generated']} generated ({stats['cached']} cached), {stats['fallback']} kept their real picture - "
                  f"generator {stats['generator']}" + (f"; quality check: {qc['passed']} passed, {qc['redone']} redone, {qc['dropped']} dropped" if qc else ""))  # fmt: skip
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
    user = f"{prompts.STYLE_GUIDE}\n\n{prompts.audience(cfg.known_story == 'yes')}\n\nWRITE IN: {prompts.lang_name(cfg.lang)}.\n\nSTORY BIBLE:\n{bible.text()}\n\nSCRIPT SO FAR (by chapter):\n{synopsis[:14000]}\n\nUSED IMAGES (id: the line it illustrates):\n" + "\n".join(used[:150])
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
    web = webrefs.build(project, bible, reader) if project.source == "book" else None
    related = related_library(project, all_notes, writer, extra=[web] if web else None)
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
    # written chapters are keyed on the linked-adaptation panels only: online references arriving later go
    # through the picture pass below instead of rewriting every chapter
    rel_key = hashlib.sha1(json.dumps({n["cid"]: [c["pid"] for c in related.get(n["cid"], []) if c["kind"] != "anime still"]
                                       for n in all_notes}).encode()).hexdigest()[:6]  # fmt: skip
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
    if any(c["kind"] == "anime still" for v in related.values() for c in v):
        needy = sum(1 for raw in raws for b in raw if not b["refs"])
        console.print(f"  picture pass: {needy} beat(s) without a real picture, matching them to the anime stills")
        with ThreadPoolExecutor(max(1, cfg.jobs)) as pool:
            raws = list(pool.map(lambda nr: repick_pictures(nr[0], related.get(nr[0]["cid"], []), nr[1], writer, work / "script"), zip(all_notes, raws)))
    xref = XrefStore(work)
    chapters_out = [{"cid": n["cid"], "title": n["title"], "beats": resolve_pictures(n, related.get(n["cid"], []), raw, xref)}
                    for n, raw in zip(all_notes, raws)]  # fmt: skip
    asked = sum(1 for c in chapters_out for b in c["beats"] if b.get("generate"))
    console.print(f"  wrote {len(chapters_out)} chapters in {time.time() - t0:.0f}s; {asked} beat(s) asked for a generated picture")
    stats = fill_real_pictures(chapters_out, all_notes, project)
    console.print(f"  pictures: {stats['reused']} filled from the chapter's own images, {stats['cards']} title cards; "
                  f"{stats['wanted']} beat(s) want a generated picture (the images stage makes them)")  # fmt: skip
    # the images stage picks style references from the anime episodes matched to each chapter
    web_src = str(web.resolve()) if web else ""
    arc = {cid: [c["pid"] for c in v if c.get("kind") == "anime still" and str(Path(c["src"]).resolve()) == web_src] for cid, v in related.items()}
    (work / "pictures_ctx.json").write_text(json.dumps({"arc_stills": arc}), "utf-8")
    hook = hook_from_beats(chapters_out, bible, writer, cfg, work / "script" / f"hook_book_{key}_{rel_key}_{cfg.hook_words}.json") if cfg.hook else []
    progress.emit("script", len(futs) + 1, len(futs) + 1, "written")
    return {"hook": hook, "chapters": chapters_out}
