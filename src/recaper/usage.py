"""Per-project usage log: every model call, image generation, voiced line and render of every run.

Appended to <project>/.recaper/usage.jsonl (one JSON object per line, tagged with the run id), so the UI can
show what each run cost in calls, images and quota hits, and estimate the next one. Recording is a no-op
until `start()` is called (the CLI's `run` does), so library code and tests never write a log by accident.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

FILE_NAME = "usage.jsonl"
_lock = threading.Lock()
_state: dict[str, Any] = {"file": None, "run": None}


def start(work_dir: Path, **meta: Any) -> str:
    run = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    _state.update(file=work_dir / FILE_NAME, run=run)
    record("run", **meta)
    return run


def stop() -> None:
    _state.update(file=None, run=None)


def record(kind: str, **fields: Any) -> None:
    path: Path | None = _state["file"]
    if path is None:
        return
    row = {"run": _state["run"], "t": round(time.time(), 2), "kind": kind, **fields}
    line = json.dumps(row, ensure_ascii=False, default=str) + "\n"
    with _lock:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError:
            pass  # usage is best-effort; never break a run over it


def read(work_dir: Path) -> list[dict]:
    path = work_dir / FILE_NAME
    if not path.exists():
        return []
    rows = []
    for line in path.read_text("utf-8", errors="replace").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


# Subscription / local providers: calls count against a plan's quota (or nothing), not a bill.
PLAN_PROVIDERS = ("claude", "antigravity", "gemini-cli", "opencode", "ollama")
# Image generation list prices, USD per image at the size recaper asks for (Gemini: 16:9 at 1K; OpenAI:
# 1536x1024), checked 2026-11 against ai.google.dev/pricing and OpenAI's image-generation guide. Reference images
# are billed as input tokens (~560 per image on Gemini, ~765 on OpenAI at the 1024 px recaper sends).
# `images.price` overrides all of this.
IMAGE_MODELS: dict[str, dict] = {
    "gemini-2.5-flash-image": {"image": 0.039, "input_per_m": 0.30},
    "gemini-3.1-flash-image": {"image": 0.067, "input_per_m": 0.50},
    "gemini-3.1-flash-lite-image": {"image": 0.0336, "input_per_m": 0.25},
    "gemini-3-pro-image": {"image": 0.134, "input_per_m": 2.00},
    "gemini-nano-banana-2.1": {"image": 0.0336, "input_per_m": 1.50},
    "gpt-image-1": {"image": {"low": 0.016, "medium": 0.063, "high": 0.25}, "input_per_m": 10.0},
    "gpt-image-1-mini": {"image": {"low": 0.006, "medium": 0.015, "high": 0.052}, "input_per_m": 2.50},
    "gpt-image-1.5": {"image": {"low": 0.013, "medium": 0.05, "high": 0.2}, "input_per_m": 8.0},
    "gpt-image-2": {"image": {"low": 0.005, "medium": 0.041, "high": 0.165}, "input_per_m": 8.0},
}
DEFAULT_IMAGE_MODEL = {"gemini": "gemini-2.5-flash-image", "openai": "gpt-image-1"}
REF_TOKENS = {"gemini": 560, "openai": 765}


def image_price(spec: str, quality: str = "high", refs: int = 0, override: float = 0.0) -> tuple[float, str]:
    """(USD per generated image incl. its reference images, where the number comes from)."""
    name, _, model = (spec or "none").partition(":")
    name = name.lower()
    if name in ("", "none"):
        return 0.0, "off"
    if override and override > 0:
        return float(override), "your price (images.price)"
    if name == "antigravity":
        return 0.0, "your Google AI plan (no bill)"
    model = model or DEFAULT_IMAGE_MODEL.get(name, "")
    entry = IMAGE_MODELS.get(model) or next((v for k, v in sorted(IMAGE_MODELS.items(), key=lambda kv: -len(kv[0])) if model.startswith(k)), None)
    note = f"list price of {model}"
    if entry is None:  # unknown / newer model: assume the provider's default and say so
        entry = IMAGE_MODELS.get(DEFAULT_IMAGE_MODEL.get(name, ""), {"image": 0.0, "input_per_m": 0.0})
        note = f"{model} not in the price list - assumed like {DEFAULT_IMAGE_MODEL.get(name, '?')}; set images.price"
    img = entry["image"]
    if isinstance(img, dict):
        img = img.get(quality, img["high"])
        note += f" ({quality})"
    return round(img + refs * REF_TOKENS.get(name, 0) * entry["input_per_m"] / 1e6, 5), note


def image_setup_sig(cfg: Any) -> str:
    """What makes cached generated images reusable: same generator, style, reference count and sources."""
    ic = cfg.images
    return "|".join(str(x) for x in (ic.generator, ic.style, ic.refs, ic.web, ic.quality, ic.quality_check, ic.min_score))


def billing(provider_label: str) -> str:
    """'plan' (subscription / local / free quota) or 'api' (billed per token)."""
    name = provider_label.split(":", 1)[0].lower()
    if name == "claude":
        import os

        return "api" if os.environ.get("ANTHROPIC_API_KEY") else "plan"
    if name == "gemini":
        import os

        return "api" if (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")) else "plan"
    return "plan" if name in PLAN_PROVIDERS else "api"


def summarize(rows: list[dict]) -> list[dict]:
    """Group the log by run (newest first)."""
    runs: dict[str, dict] = {}
    for r in rows:
        run = runs.setdefault(r.get("run") or "?", {
            "run": r.get("run"), "started": r["t"], "ended": r["t"], "meta": {}, "llm": {}, "images": {"ok": 0, "failed": 0, "seconds": 0.0, "usd": 0.0},
            "voice": {"lines": 0, "seconds": 0.0, "engine": None}, "upscaled": 0, "renders": [], "outputs": [], "status": None,
        })  # fmt: skip
        run["started"], run["ended"] = min(run["started"], r["t"]), max(run["ended"], r["t"])
        k = r.get("kind")
        if k == "run":
            run["meta"] = {x: y for x, y in r.items() if x not in ("run", "t", "kind")}
        elif k == "llm":
            p = run["llm"].setdefault(r.get("provider", "?"), {
                "role": r.get("role"), "calls": 0, "ok": 0, "quota": 0, "refused": 0, "errors": 0, "images_sent": 0,
                "chars_in": 0, "chars_out": 0, "seconds": 0.0, "billing": billing(r.get("provider", "?")),
            })  # fmt: skip
            p["calls"] += 1
            p[{"ok": "ok", "quota": "quota", "refusal": "refused"}.get(r.get("outcome"), "errors")] += 1
            for f in ("images_sent", "chars_in", "chars_out"):
                p[f] += int(r.get(f) or 0)
            p["seconds"] += float(r.get("seconds") or 0)
        elif k == "image":
            run["images"]["ok" if r.get("outcome") == "ok" else "failed"] += 1
            run["images"]["seconds"] += float(r.get("seconds") or 0)
            run["images"]["provider"] = r.get("provider")
            run["images"]["usd"] = round(run["images"]["usd"] + float(r.get("usd") or 0), 4)
        elif k == "pictures":
            run["pictures"] = {x: y for x, y in r.items() if x not in ("run", "t", "kind")}
        elif k == "voice":
            run["voice"]["lines"] += int(r.get("lines") or 0)
            run["voice"]["seconds"] += float(r.get("seconds") or 0)
            run["voice"]["engine"] = r.get("engine")
        elif k == "upscale":
            run["upscaled"] += int(r.get("panels") or 0)
        elif k == "render":
            run["renders"].append({x: y for x, y in r.items() if x not in ("run", "t", "kind")})
        elif k == "end":
            run["status"] = r.get("status")
            run["error"] = r.get("error")
            run["outputs"] = r.get("outputs") or []
    out = sorted(runs.values(), key=lambda x: -x["started"])
    for r in out:
        r["seconds"] = round(r["ended"] - r["started"], 1)
        r["status"] = r["status"] or "unfinished"
    return out


PACE_FILE = "voice_pace.json"


def save_pace(work_dir: Path, tcfg: Any, words: int, seconds: float) -> None:
    """Remember how fast the last narration really was (words over its length, pauses included)."""
    data = {"engine": tcfg.engine, "voice": tcfg.voice, "speed": tcfg.speed, "words": words, "seconds": round(seconds, 1)}
    try:
        (work_dir / PACE_FILE).write_text(json.dumps(data), "utf-8")
    except OSError:
        pass


def pace(work_dir: Path, cfg: Any) -> float:
    """Narration words per minute: measured on this project's last voicing (scaled to the current speed) when
    it used the same engine, else the nominal pace from the settings."""
    try:
        m = json.loads((work_dir / PACE_FILE).read_text("utf-8"))
        if m.get("engine") == cfg.tts.engine and m.get("seconds", 0) > 30 and m.get("speed"):
            return m["words"] / (m["seconds"] / 60) * cfg.tts.speed / m["speed"]
    except (OSError, ValueError, KeyError, TypeError, ZeroDivisionError):
        pass
    return cfg.spoken_wpm


def estimate(project_dir: Path, cfg: Any, chapters: list[dict], is_book: bool) -> dict:
    """Rough cost of the next full run, from what is already cached. `chapters` = the UI's cheap listing
    ([{title, pages}], in pipeline order). Numbers are upper-bound-ish estimates, not promises."""
    import math

    from .config import WORK_DIR_NAME, parse_chapter_ranges

    work = project_dir / WORK_DIR_NAME
    if is_book:  # volumes split into chapters once extracted; before that ~12 per volume
        extracted = [c for f in sorted((work / "book").glob("*/chapters.json")) for c in json.loads(f.read_text("utf-8"))]
        chapters = [{"title": c.get("title"), "pages": 0} for c in extracted] or [{"title": "?", "pages": 0}] * (12 * len(chapters))
    picked = parse_chapter_ranges(cfg.chapters, len(chapters)) if chapters else []
    reader_calls = images_sent = writer_calls = polish_calls = 0
    unread = unwritten = 0
    panels_known = 0
    for i in picked:
        cid = f"c{i + 1:04d}"
        index = work / "panels" / cid / "panels.json"
        if index.exists():
            n = len(json.loads(index.read_text("utf-8")))
            panels_known += n
        else:
            n = 0 if is_book else int((chapters[i].get("pages") or 20) * 3)
        if not (work / "notes" / f"{cid}.json").exists():
            unread += 1
            reader_calls += math.ceil(n / max(1, cfg.read_batch)) if n else 0
            images_sent += n
            if is_book:
                writer_calls += 1  # prose digest
        if not any((work / "script").glob(f"{cid}_*.json")):
            unwritten += 1
            writer_calls += 1
            polish_calls += 1 if cfg.polish else 0
    has_script = (work / "script.json").exists()
    if unread:
        writer_calls += 1  # bible reconciliation
    if not has_script or unwritten:
        writer_calls += (1 if cfg.hook else 0) + (1 if is_book and unread else 0)  # hook (+ related-works map)
    # word targets use words_per_minute; the voice speed decides how long they take to say
    wpm = pace(work, cfg)
    minutes = (cfg.target_minutes or len(picked) * cfg.minutes_per_chapter) * cfg.words_per_minute / wpm
    if has_script:  # the real length beats the target
        try:
            sc = json.loads((work / "script.json").read_text("utf-8"))
            words = sum(len(b["text"].split()) for c in [{"beats": sc.get("hook", [])}, *sc.get("chapters", [])] for b in c["beats"])
            minutes = words / max(1, wpm)
            n_script = len(sc.get("chapters", []))
            if picked and n_script and len(picked) != n_script:  # a different selection: scale the script's pace
                minutes *= len(picked) / n_script
        except (OSError, ValueError, KeyError):
            pass
    parts = math.ceil(minutes / cfg.episode_minutes) if cfg.episode_minutes and minutes > cfg.episode_minutes * 1.25 else 1
    if parts > 1 and cfg.previously:
        writer_calls += parts - 1
    images = image_estimate(work, cfg, len(picked), is_book)
    refs = references_estimate(work, cfg, picked, is_book)
    reader_calls += images["qc"]["calls"] + refs["reader_calls"]
    images_sent += images["qc"]["images"] + refs["images"]
    writer_calls += refs["picture_pass"]
    up_dir = work / "upscaled"
    upscaled = sum(1 for _ in up_dir.rglob("*.jpg")) if up_dir.exists() else 0
    reader_bill = sorted({billing(p) for p in cfg.reader.split(",") if p.strip()})
    writer_bill = sorted({billing(p) for p in cfg.writer.split(",") if p.strip()})
    return {
        "chapters": len(picked), "unread": unread, "unwritten": unwritten,
        "reader": {"spec": cfg.reader, "calls": reader_calls, "images": images_sent, "billing": reader_bill},
        "writer": {"spec": cfg.writer, "calls": writer_calls + (0 if cfg.polisher else polish_calls), "billing": writer_bill},
        "polisher": {"spec": cfg.polisher, "calls": polish_calls if cfg.polisher else 0,
                     "billing": sorted({billing(p) for p in cfg.polisher.split(",") if p.strip()})},
        "images": images,
        "references": refs,
        "voice": {"engine": cfg.tts.engine, "words": int(minutes * wpm)},
        "video": {"minutes": round(minutes, 1), "parts": parts},
        "upscaler": {"mode": cfg.render.upscaler, "cached": upscaled},
        "panels_known": panels_known,
    }  # fmt: skip


QC_REDO_PRIOR = 0.35  # share of generated images the quality check sends back, until this project has measured it
ASKED_PER_CHAPTER_PRIOR = 3.0  # pictures a book chapter asks to generate, until a run has measured it


def _last(rows: list[dict], kind: str) -> dict | None:
    return next((r for r in reversed(rows) if r.get("kind") == kind), None)


def _written_requests(work: Path) -> tuple[int, int] | None:
    """(beats asking for a generated picture, chapters) in the newest written chapter of each cid - before any
    run has logged its pictures, this project's own scripts are the best measure."""
    newest: dict[str, Path] = {}
    for f in (work / "script").glob("c*_*.json") if (work / "script").exists() else []:
        if "_pics_" in f.name:
            continue
        cid = f.name.split("_", 1)[0]
        if cid not in newest or f.stat().st_mtime > newest[cid].stat().st_mtime:
            newest[cid] = f
    asked = 0
    for f in newest.values():
        try:
            asked += sum(1 for b in json.loads(f.read_text("utf-8")).get("beats", []) if b.get("generate") and not b.get("refs"))
        except (OSError, ValueError):
            continue
    return (asked, len(newest)) if newest else None


def image_estimate(work: Path, cfg: Any, n_chapters: int, is_book: bool) -> dict:
    """Generated images of the next run and what they cost.

    How many: the pictures the writer asks for per chapter (measured on this project's last run, else a
    prior), capped by images.max_generated, minus the ones the last run already made with the same setup (they
    are cached). Cost: list price of the model at the size recaper requests, plus its reference images, times the
    attempts - the quality check redoes rejected images once (its measured redo rate, else 35%). Every
    attempt is billed, the dropped ones too. `usd_max` assumes every image needs its second attempt."""
    ic = cfg.images
    spec = ic.generator or "none"
    on = is_book and spec.split(":", 1)[0].lower() != "none"
    price, source = image_price(spec, ic.quality, ic.refs, ic.price) if on else (0.0, "off")
    out = {"spec": spec, "on": on, "price": price, "price_source": source, "budget": ic.max_generated, "asked": 0, "cached": 0,
           "new": 0, "attempts": 0, "usd": 0.0, "usd_max": 0.0, "basis": "none", "billing": "plan" if price == 0 else "api",
           "qc": {"on": bool(on and ic.quality_check), "redo_rate": 0.0, "calls": 0, "images": 0}}  # fmt: skip
    if not on or not n_chapters:
        return out
    rows = read(work)
    last = _last(rows, "pictures")
    written = _written_requests(work)
    if last and last.get("chapters"):
        per_chapter, out["basis"] = last["asked"] / last["chapters"], "measured on the last run"
    elif written:
        per_chapter, out["basis"] = written[0] / written[1], f"measured on the {written[1]} written chapters"
    else:
        per_chapter, out["basis"] = ASKED_PER_CHAPTER_PRIOR, f"first run: assumes ~{ASKED_PER_CHAPTER_PRIOR:g} per chapter"
    asked = round(per_chapter * n_chapters)
    slots = min(ic.max_generated, asked)
    cached = 0
    if last and last.get("sig") == image_setup_sig(cfg):  # same setup: what the last run made is reused
        cached = min(slots, int(last.get("cached", 0)) + int(last.get("new", 0)))
    new = max(0, slots - cached)
    redo = 0.0
    if out["qc"]["on"]:
        tries = int(last.get("first_tries", 0)) if last else 0
        redo = (int(last.get("redone", 0)) / tries) if tries >= 5 else QC_REDO_PRIOR
    attempts = new * (1 + redo)
    out.update(asked=asked, cached=cached, new=new, attempts=round(attempts), usd=round(attempts * price, 2),
               usd_max=round(new * (2 if out["qc"]["on"] else 1) * price, 2))  # fmt: skip
    if out["qc"]["on"]:
        out["qc"].update(redo_rate=round(redo, 2), calls=round(attempts), images=round(attempts * (ic.refs + 1)))
    return out


def references_estimate(work: Path, cfg: Any, picked: list[int], is_book: bool) -> dict:
    """Online references: describing screenshots not described yet (one reader call per anime episode) and the
    picture pass for written chapters that haven't been matched to the screenshots yet."""
    out = {"on": False, "fetched": False, "episodes": 0, "stills": 0, "designs": 0, "reader_calls": 0, "images": 0, "picture_pass": 0}
    if not is_book or str(cfg.images.web).lower() in ("off", "no", "false", "0", "none"):
        return out
    out["on"] = True
    web = work / "web"
    try:
        state = json.loads((web / "source.json").read_text("utf-8"))
    except (OSError, ValueError):
        return out  # not looked up yet: the UI says so (the count is only known after the lookup)
    out["fetched"] = bool(state.get("fetched"))
    eps = state.get("episodes") or []
    budget = cfg.images.web_max
    for i, e in enumerate(eps, start=1):
        n = min(len(e.get("files") or []), max(0, budget))
        budget -= n
        if not n:
            continue
        out["episodes"] += 1
        out["stills"] += n
        if not (web / ".recaper" / "notes" / f"c{i:04d}.json").exists():
            out["reader_calls"] += 1
            out["images"] += n
    try:
        out["designs"] = len(json.loads((web / "chars.json").read_text("utf-8")))
    except (OSError, ValueError):
        pass
    if out["stills"]:
        out["picture_pass"] = sum(1 for i in picked if not any((work / "script").glob(f"c{i + 1:04d}_pics_*.json")))
    return out
