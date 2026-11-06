"""Read panels -> chapter notes + story bible; then write the narration script.

Everything is cached under the work dir so runs are resumable:
  notes/<cid>.json        what the reader saw, per panel, plus the running summary
  script/<cid>.json       beats written for a chapter
  script/hook.json        the cold open
  script.json             the assembled, user-editable script (never overwritten unless forced)
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

import cv2

from . import prompts
from .config import Config
from .ingest import Chapter
from .llm import Chain, ImagePart
from .log import console
from .panels import Panel, imread, imwrite

# ------------------------------------------------------------------- bible


class Bible:
    def __init__(self) -> None:
        self.characters: dict[str, dict[str, Any]] = {}
        self.terms: dict[str, str] = {}

    def _find(self, name: str) -> str | None:
        key = name.strip().lower()
        for canon, entry in self.characters.items():
            if canon.lower() == key or key in (a.lower() for a in entry.get("aliases", [])):
                return canon
        return None

    def merge(self, characters: list[dict] | None, terms: list[dict] | None) -> None:
        for c in characters or []:
            name = str(c.get("name", "")).strip()
            if not name:
                continue
            canon = self._find(name) or name
            entry = self.characters.setdefault(canon, {"aliases": [], "desc": ""})
            for alias in c.get("aliases") or []:
                if alias and alias != canon and alias not in entry["aliases"]:
                    entry["aliases"].append(alias)
            if c.get("desc"):
                entry["desc"] = str(c["desc"]).strip()
        for t in terms or []:
            if t.get("term"):
                self.terms[str(t["term"]).strip()] = str(t.get("meaning", "")).strip()

    def text(self) -> str:
        lines = []
        for name, e in self.characters.items():
            alias = f" (aka {', '.join(e['aliases'])})" if e["aliases"] else ""
            lines.append(f"- {name}{alias}: {e['desc']}")
        if self.terms:
            lines.append("Terms:")
            lines.extend(f"- {k}: {v}" for k, v in self.terms.items())
        return "\n".join(lines)


# ------------------------------------------------------------------ reading


def _read_tiles(panel: Panel, work_dir: Path, max_ratio: float = 2.0) -> list[Path]:
    """Tall panels are split into overlapping tiles so text stays legible for the reader."""
    src = work_dir / panel.file
    ratio = panel.h / max(panel.w, 1)
    if ratio <= max_ratio + 0.3:
        return [src]
    n = math.ceil(ratio / max_ratio)
    tiles = []
    img = None
    step = panel.h / n
    overlap = int(panel.w * 0.08)
    for k in range(n):
        dest = work_dir / "read_tiles" / f"{panel.id}_{k}.jpg"
        if not dest.exists():
            if img is None:
                img = imread(src)
            y0 = max(int(k * step) - overlap, 0)
            y1 = min(int((k + 1) * step) + overlap, panel.h)
            imwrite(dest, img[y0:y1], quality=90)
        tiles.append(dest)
    return tiles


def _label_map(panels: list[Panel]) -> dict[str, Panel]:
    return {f"P{i}": p for i, p in enumerate(panels, start=1)}


def read_chapter(
    chapter: Chapter,
    panels: list[Panel],
    bible_text: str,
    reader: Chain,
    cfg: Config,
    work_dir: Path,
) -> dict[str, Any]:
    """Read one chapter. Pure w.r.t. shared state, so chapters can be read in parallel."""
    path = work_dir / "notes" / f"{chapter.cid}.json"
    if path.exists():
        return json.loads(path.read_text("utf-8"))

    labels = _label_map(panels)
    items = list(labels.items())
    batches = [items[i : i + cfg.read_batch] for i in range(0, len(items), cfg.read_batch)]
    panel_notes: dict[str, dict] = {}
    summaries: list[str] = []
    titles: list[str] = []
    new_chars: list[dict] = []
    new_terms: list[dict] = []
    local = Bible()  # characters met so far in THIS chapter, carried between its batches

    for bi, batch in enumerate(batches, start=1):
        note = f"Batch {bi}/{len(batches)}: labels {batch[0][0]}-{batch[-1][0]}."
        if summaries:
            note += " Earlier in this chapter: " + " ".join(summaries)
        known = "\n".join(x for x in (bible_text, local.text()) if x)
        parts: list = [prompts.reader_user(known, "", chapter.title, note)]
        for label, panel in batch:
            parts.append(f"[{label}]")
            parts.extend(ImagePart(t) for t in _read_tiles(panel, work_dir))
        console.print(f"  reading {chapter.cid} batch {bi}/{len(batches)} ({len(batch)} panels)")
        res = reader.complete_json(prompts.READER_SYSTEM, parts)
        for entry in res.get("panels", []):
            label = _norm_label(entry.get("id"))
            if label in labels:
                panel_notes[label] = {
                    "story": bool(entry.get("story", True)),
                    "weight": int(entry.get("weight", 2) or 2),
                    "desc": str(entry.get("desc", "")).strip(),
                    "chars": entry.get("chars") or [],
                }
        if res.get("summary"):
            summaries.append(str(res["summary"]).strip())
        if res.get("title"):
            titles.append(str(res["title"]).strip())
        new_chars += res.get("characters") or []
        new_terms += res.get("terms") or []
        local.merge(res.get("characters"), res.get("terms"))

    for label in labels:  # anything the model skipped
        panel_notes.setdefault(label, {"story": True, "weight": 1, "desc": "", "chars": []})

    notes = {
        "cid": chapter.cid,
        "title": titles[0] if titles else chapter.title,
        "source_title": chapter.title,
        "summary": " ".join(summaries),
        "labels": {k: p.id for k, p in labels.items()},
        "panels": panel_notes,
        "characters": new_chars,
        "terms": new_terms,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(notes, ensure_ascii=False, indent=1), "utf-8")
    return notes


def reconcile_bible(all_notes: list[dict], writer: Chain, work_dir: Path) -> Bible:
    """Merge the per-chapter character lists (read in parallel) into one canonical bible."""
    path = work_dir / "bible.json"
    key = [n["cid"] for n in all_notes]
    if path.exists():
        data = json.loads(path.read_text("utf-8"))
        if data.get("chapters") == key:
            bible = Bible()
            bible.characters, bible.terms = data["characters"], data["terms"]
            return bible
    naive = Bible()
    for n in all_notes:
        naive.merge(n.get("characters"), n.get("terms"))
    bible = naive
    if len(all_notes) > 1 and naive.characters:
        listing = "\n\n".join(
            f"{n['cid']} ({n['title']}):\n" + "\n".join(f"- {c.get('name')}: {c.get('desc', '')}" for c in n.get("characters") or [])
            for n in all_notes
        )
        try:
            res = writer.complete_json(prompts.BIBLE_SYSTEM, [listing])
            merged = Bible()
            merged.merge(res.get("characters"), res.get("terms"))
            for term, meaning in naive.terms.items():
                merged.terms.setdefault(term, meaning)
            if merged.characters:
                bible = merged
        except Exception as e:  # noqa: BLE001 - fall back to the mechanical merge
            console.print(f"[yellow]bible reconciliation failed ({e}); using merged lists[/]")
    path.write_text(
        json.dumps({"chapters": key, "characters": bible.characters, "terms": bible.terms}, ensure_ascii=False, indent=1), "utf-8"
    )
    return bible


def story_before(all_notes: list[dict], index: int, max_words: int = 650) -> str:
    """Summaries of earlier chapters, oldest dropped first (no model call needed)."""
    parts = [f"{n['title']}: {n['summary']}" for n in all_notes[:index]]
    words: list[str] = []
    for part in reversed(parts):
        w = part.split()
        if len(words) + len(w) > max_words:
            break
        words = w + words
    return " ".join(words)


def previous_context(all_notes: list[dict], index: int, k: int = 3) -> str:
    """What the reader saw at the end of the previous chapter, for seamless narration."""
    if index == 0:
        return ""
    prev = [x["desc"] for x in all_notes[index - 1]["panels"].values() if x["story"] and x["desc"]]
    return " / ".join(prev[-k:])


def _norm_label(raw: Any) -> str:
    s = str(raw or "").strip().upper()
    if ":" in s:
        s = s.split(":", 1)[1]
    m = re.search(r"(\d+)", s)
    return f"P{int(m.group(1))}" if m else s


# ------------------------------------------------------------------ writing


def _notes_text(notes: dict) -> str:
    lines = []
    for label, n in notes["panels"].items():
        if not n["story"]:
            continue
        chars = f" ({', '.join(n['chars'])})" if n.get("chars") else ""
        lines.append(f"{label} [w{n['weight']}]{chars}: {n['desc'] or '(no description)'}")
    return "\n".join(lines)


_CLEAN = re.compile(r"[*_#`~\[\]{}<>|]")


def _clean_text(text: str) -> str:
    text = _CLEAN.sub("", str(text))
    return re.sub(r"\s+", " ", text).strip()


def _resolve_beats(raw_beats: list, resolve) -> list[dict]:
    beats: list[dict] = []
    for b in raw_beats or []:
        text = _clean_text(b.get("text", ""))
        if not text:
            continue
        ids = [pid for lab in (b.get("panels") or []) if (pid := resolve(lab))]
        ids = list(dict.fromkeys(ids))[:3]
        if not ids and beats:
            ids = beats[-1]["panels"][-1:]
        emphasis = b.get("emphasis") if b.get("emphasis") in ("normal", "hit", "reveal") else "normal"
        beats.append({"text": text, "panels": ids, "emphasis": emphasis})
    # first beat without panels borrows the next beat's first panel
    for i, b in enumerate(beats):
        if not b["panels"]:
            nxt = next((x["panels"][:1] for x in beats[i + 1 :] if x["panels"]), [])
            b["panels"] = nxt
    return [b for b in beats if b["panels"]]


def write_chapter(
    chapter_notes: dict,
    position: str,
    story_so_far: str,
    prev_context: str,
    target_words: int,
    bible: Bible,
    writer: Chain,
    cfg: Config,
    path: Path,
) -> list[dict]:
    """Write one chapter's beats. Independent of other chapters' scripts -> parallelisable."""
    if path.exists():
        return json.loads(path.read_text("utf-8"))["beats"]
    user = prompts.writer_user(
        language=cfg.lang,
        target_words=target_words,
        bible_text=bible.text(),
        story_so_far=story_so_far,
        prev_context=prev_context,
        chapter_title=chapter_notes["title"],
        notes_text=_notes_text(chapter_notes),
        position=position,
        extra=cfg.extra_instructions,
    )
    console.print(f"  writing {chapter_notes['cid']} (~{target_words} words)")
    res = writer.complete_json(prompts.WRITER_SYSTEM, [user])
    labels = chapter_notes["labels"]
    beats = _resolve_beats(res.get("beats", []), lambda lab: labels.get(_norm_label(lab)))
    if not beats:
        raise RuntimeError(f"writer returned no usable beats for {chapter_notes['cid']}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"beats": beats}, ensure_ascii=False, indent=1), "utf-8")
    return beats


def write_hook(all_notes: list[dict], bible: Bible, writer: Chain, cfg: Config, path: Path) -> list[dict]:
    if path.exists():
        return json.loads(path.read_text("utf-8"))["beats"]
    synopsis = "\n".join(f"{n['title']}: {n['summary']}" for n in all_notes)
    highlights = []
    for n in all_notes:
        strong = [(lab, x) for lab, x in n["panels"].items() if x["story"] and x["weight"] >= 3]
        for lab, x in strong[:8]:
            highlights.append(f"{n['cid']}:{lab}: {x['desc']}")
    if not highlights:  # nothing rated 3: fall back to the first described panels
        for n in all_notes[:3]:
            for lab, x in list(n["panels"].items())[:6]:
                if x["story"]:
                    highlights.append(f"{n['cid']}:{lab}: {x['desc']}")
    user = prompts.hook_user(cfg.lang, bible.text(), synopsis[:12000], "\n".join(highlights[:80]), cfg.extra_instructions)
    console.print("  writing cold-open hook")
    res = writer.complete_json(prompts.HOOK_SYSTEM.replace("{WORDS}", str(cfg.hook_words)), [user])
    by_cid = {n["cid"]: n["labels"] for n in all_notes}

    def resolve(lab: str) -> str | None:
        lab = str(lab)
        if ":" in lab:
            cid, rest = lab.split(":", 1)
            return by_cid.get(cid.strip(), {}).get(_norm_label(rest))
        return all_notes[0]["labels"].get(_norm_label(lab)) if all_notes else None

    beats = _cap_words(_resolve_beats(res.get("beats", []), resolve), max_words=int(cfg.hook_words * 1.2))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"beats": beats}, ensure_ascii=False, indent=1), "utf-8")
    return beats


def _cap_words(beats: list[dict], max_words: int) -> list[dict]:
    """Keep whole beats up to the word budget (models overshoot hook lengths)."""
    out, total = [], 0
    for b in beats:
        n = len(b["text"].split())
        if out and total + n > max_words:
            break
        out.append(b)
        total += n
    return out


def chapter_word_targets(all_notes: list[dict], cfg: Config, hook_words: int = 90) -> list[int]:
    if not cfg.target_minutes:
        return [max(60, int(cfg.minutes_per_chapter * cfg.words_per_minute))] * len(all_notes)
    total = max(150, int(cfg.target_minutes * cfg.words_per_minute) - hook_words)
    weights = [sum(x["weight"] for x in n["panels"].values() if x["story"]) or 1 for n in all_notes]
    s = sum(weights)
    return [max(60, int(total * w / s)) for w in weights]


def positions(n: int) -> list[str]:
    if n == 1:
        return ["only"]
    return ["first"] + ["middle"] * (n - 2) + ["last"]


# ------------------------------------------------------------------ polish pass

_PRESENT_SPEECH = re.compile(
    r"\b(?:he|she|they|it|[A-Z][a-z]+)\s+(tells|asks|says|explains|replies|answers|shouts|whispers|thinks|realizes|realises|wonders|admits|insists|warns|promises|mutters|yells|declares)\b"
)
_TURNS = re.compile(r"^(but|however|yet|unfortunately|little did|then again|instead|still|meanwhile|suddenly|just as|only)\b", re.I)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?…])\s+", text) if s.strip()]


def style_checks(beats: list[dict], lang: str) -> list[str]:
    """Cheap, deterministic findings handed to the polish model (English-specific checks only for English)."""
    out: list[str] = []
    sents = [s for b in beats for s in _sentences(b["text"])]
    if not sents:
        return out
    long = [s for s in sents if len(s.split()) > 35]
    if long:
        out.append(f"{len(long)} sentence(s) over 35 words, e.g. \"{long[0][:90]}…\"")
    questions = sum(s.endswith("?") for s in sents)
    if questions > 1:
        out.append(f"{questions} rhetorical questions (max 1, only as the final cliffhanger)")
    if not lang.lower().startswith("en"):
        return out
    present = sorted({m.group(0) for b in beats for m in _PRESENT_SPEECH.finditer(b["text"])})
    if present:
        out.append("speech/thought reported in the present: " + ", ".join(f'"{p}"' for p in present[:8]))
    openers: dict[str, int] = {}
    for s in sents:
        w = re.sub(r"[^\w']", "", s.split()[0]).lower() if s.split() else ""
        if w and w not in ("the", "a", "an"):
            openers[w] = openers.get(w, 0) + 1
    rep = [f'"{w}" x{n}' for w, n in sorted(openers.items(), key=lambda x: -x[1]) if n >= max(3, len(sents) // 6)]
    if rep:
        out.append("sentences repeatedly open with " + ", ".join(rep[:5]))
    turns = sum(bool(_TURNS.match(s)) for s in sents)
    if len(sents) >= 9 and turns < len(sents) / 6:
        out.append(f"only {turns} turn(s) in {len(sents)} sentences (aim for about one every three)")
    words: dict[str, int] = {}
    for b in beats:
        for phrase in re.findall(r"\b(?:\w+\s){2}\w+\b", b["text"].lower()):
            words[phrase] = words.get(phrase, 0) + 1
    tics = [p for p, n in words.items() if n >= 3 and not re.match(r"^(of the|in the|to the|and the|on the|at the)", p)]
    if tics:
        out.append("repeated phrases: " + ", ".join(f'"{t}"' for t in tics[:5]))
    return out


def polish_chapter(beats: list[dict], title: str, bible: Bible, writer: Chain, cfg: Config, cache_dir: Path) -> list[dict]:
    """Line-edit one chapter's beat texts (panels, emphasis and count untouched). Cached by content."""
    if len(beats) < 2:
        return beats
    blob = json.dumps([[b["text"] for b in beats], cfg.lang, cfg.extra_instructions, bible.text(), prompts.POLISH_SYSTEM], ensure_ascii=False)
    path = cache_dir / f"polish_{hashlib.sha1(blob.encode()).hexdigest()[:16]}.json"
    if path.exists():
        texts = json.loads(path.read_text("utf-8"))["texts"]
    else:
        checks = style_checks(beats, cfg.lang)
        numbered = "\n".join(f"{i}. {b['text']}" for i, b in enumerate(beats, start=1))
        user = prompts.polish_user(cfg.lang, bible.text(), title, numbered, "\n".join(f"- {c}" for c in checks), cfg.extra_instructions)
        try:
            res = writer.complete_json(prompts.POLISH_SYSTEM, [user])
        except Exception as e:  # noqa: BLE001 - polishing is optional; keep the draft
            console.print(f"[yellow]polish skipped for {title}: {e}[/]")
            return beats
        texts = res.get("texts") if isinstance(res, dict) else None
        if not isinstance(texts, list) or len(texts) != len(beats):
            console.print(f"[yellow]polish for {title} returned {len(texts) if isinstance(texts, list) else 'no'} texts for {len(beats)} beats; keeping the draft[/]")
            return beats
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"texts": texts, "checks": style_checks(beats, cfg.lang)}, ensure_ascii=False, indent=1), "utf-8")
    out = []
    for b, t in zip(beats, texts):
        t = _clean_text(str(t or ""))
        n0, n1 = len(b["text"].split()), len(t.split())
        ok = t and 0.6 * n0 - 3 <= n1 <= 1.5 * n0 + 5  # a rewrite that lost or invented content is ignored
        out.append({**b, "text": t} if ok else b)
    return out
