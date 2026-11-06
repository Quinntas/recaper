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
# Approximate list prices for image generation (USD per image), used for estimates only.
IMAGE_PRICES = {"gemini": 0.039, "openai": 0.063, "antigravity": 0.0}


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
            "run": r.get("run"), "started": r["t"], "ended": r["t"], "meta": {}, "llm": {}, "images": {"ok": 0, "failed": 0, "seconds": 0.0},
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
    reader_calls = images_sent = writer_calls = 0
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
            writer_calls += 1 + (1 if cfg.polish else 0)
    has_script = (work / "script.json").exists()
    if unread:
        writer_calls += 1  # bible reconciliation
    if not has_script or unwritten:
        writer_calls += (1 if cfg.hook else 0) + (1 if is_book and unread else 0)  # hook (+ related-works map)
    minutes = cfg.target_minutes or len(picked) * cfg.minutes_per_chapter
    if has_script:  # the real length beats the target
        try:
            sc = json.loads((work / "script.json").read_text("utf-8"))
            words = sum(len(b["text"].split()) for c in [{"beats": sc.get("hook", [])}, *sc.get("chapters", [])] for b in c["beats"])
            minutes = words / max(1, cfg.words_per_minute)
        except (OSError, ValueError, KeyError):
            pass
    parts = math.ceil(minutes / cfg.episode_minutes) if cfg.episode_minutes and minutes > cfg.episode_minutes * 1.25 else 1
    if parts > 1 and cfg.previously:
        writer_calls += parts - 1
    gen = 0
    spec = (cfg.images.generator or "none").split(":", 1)[0]
    if is_book and spec != "none":
        done = work / "panels" / "gen" / "panels.json"
        made = len(json.loads(done.read_text("utf-8"))) if done.exists() else 0
        gen = max(0, cfg.images.max_generated - made) if not has_script else 0
    up_dir = work / "upscaled"
    upscaled = sum(1 for _ in up_dir.rglob("*.jpg")) if up_dir.exists() else 0
    reader_bill = sorted({billing(p) for p in cfg.reader.split(",") if p.strip()})
    writer_bill = sorted({billing(p) for p in cfg.writer.split(",") if p.strip()})
    image_cost = round(gen * IMAGE_PRICES.get(spec, 0.0), 2)
    return {
        "chapters": len(picked), "unread": unread, "unwritten": unwritten,
        "reader": {"spec": cfg.reader, "calls": reader_calls, "images": images_sent, "billing": reader_bill},
        "writer": {"spec": cfg.writer, "calls": writer_calls, "billing": writer_bill},
        "images": {"spec": cfg.images.generator, "max": gen, "usd": image_cost},
        "voice": {"engine": cfg.tts.engine, "words": int(minutes * cfg.words_per_minute)},
        "video": {"minutes": round(minutes, 1), "parts": parts},
        "upscaler": {"mode": cfg.render.upscaler, "cached": upscaled},
        "panels_known": panels_known,
    }  # fmt: skip
