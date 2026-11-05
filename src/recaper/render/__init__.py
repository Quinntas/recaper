"""Render shots to video: chunks rendered in parallel worker processes, piped to ffmpeg, then muxed."""

from __future__ import annotations

import hashlib
import json
import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path

from ..config import RenderConfig
from .. import progress
from ..log import console
from .encode import mux_final, nvenc_available, open_raw_writer, video_codec_args
from .styles import Look, PanelInfo, Shot, build_shots, make_style, panel_infos

__all__ = ["render_video", "build_shots"]

_RENDER_CODE = hashlib.sha1(
    b"".join((Path(__file__).parent / f).read_bytes() for f in ("styles.py", "compose.py", "encode.py", "__init__.py"))
).hexdigest()


def _render_chunk(job: dict) -> str:
    out = Path(job["out"])
    if out.exists() and out.stat().st_size > 0:
        return str(out)
    import cv2

    cv2.setNumThreads(3)  # measured best: 8 workers x 3 threads (rendering is memory-bandwidth bound)
    panels = {k: PanelInfo(**v) for k, v in job["panels"].items()}
    style = make_style(job["style"], job["width"], job["height"], panels, job.get("look"))
    shots = [Shot(**s) for s in job["shots"]]
    prev = Shot(**job["prev"]) if job["prev"] else None
    fps = job["fps"]
    tmp = out.with_suffix(".part.mp4")
    proc = open_raw_writer(tmp, job["width"], job["height"], fps, job["codec"])
    try:
        f0, f1 = job["f0"], job["f1"]
        i = 0
        for f in range(f0, f1):
            t = f / fps
            while i < len(shots) - 1 and t >= shots[i].end:
                i += 1
            shot = shots[i]
            before = shots[i - 1] if i > 0 else prev
            frame = style.frame(shot, before, max(0.0, t - shot.start))
            proc.stdin.write(memoryview(cv2.cvtColor(frame, cv2.COLOR_BGR2YUV_I420)))
        proc.stdin.close()
        err = proc.stderr.read().decode(errors="replace")
        if proc.wait() != 0:
            raise RuntimeError(f"ffmpeg failed on {out.name}: {err[-800:]}")
    except BaseException:
        proc.kill()
        raise
    tmp.replace(out)
    return str(out)


def render_video(
    timed_beats: list[dict],
    panel_meta: dict[str, dict],
    style: str,
    rcfg: RenderConfig,
    narration: Path,
    chapters_meta: Path | None,
    out: Path,
    work_dir: Path,
    chunk_seconds: float | None = None,
) -> None:
    chunk_seconds = chunk_seconds or rcfg.chunk_seconds
    look = {k: getattr(rcfg, k) for k in Look.__dataclass_fields__}
    shots = build_shots(timed_beats, style)
    if not shots:
        raise RuntimeError("nothing to render")
    used = {p for s in shots for p in s.panels}
    infos = {pid: asdict(i) for pid, i in panel_infos({p: panel_meta[p] for p in used}, work_dir).items()}
    fps = rcfg.fps
    codec = video_codec_args(rcfg.encoder, rcfg.quality, fps)
    enc_name = "NVENC" if "h264_nvenc" in codec else "x264"

    # chunk at shot boundaries; chunk identity hashes its content so edits only re-render what changed
    chunks: list[list[Shot]] = []
    cur: list[Shot] = []
    for s in shots:
        cur.append(s)
        if s.end - cur[0].start >= chunk_seconds:
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)

    render_dir = work_dir / "render"
    render_dir.mkdir(parents=True, exist_ok=True)
    jobs = []
    prev: Shot | None = None
    for ch in chunks:
        f0, f1 = round(ch[0].start * fps), round(ch[-1].end * fps)
        payload = {
            "style": style, "look": look, "width": rcfg.width, "height": rcfg.height, "fps": fps, "codec": codec,
            "shots": [asdict(s) for s in ch], "prev": asdict(prev) if prev else None, "f0": f0, "f1": f1,
            "panels": {p: infos[p] for s in ch + ([prev] if prev else []) for p in s.panels},
        }  # fmt: skip
        # key includes the renderer's own source so code/style changes never reuse stale chunks
        key = hashlib.sha1((_RENDER_CODE + json.dumps(payload, sort_keys=True)).encode()).hexdigest()[:16]
        payload["out"] = str(render_dir / f"chunk_{len(jobs):04d}_{key}.mp4")
        jobs.append(payload)
        prev = ch[-1]

    # consumer NVENC allows ~8-12 concurrent sessions; above that ffmpeg fails to open the encoder
    workers = rcfg.workers or max(1, min((os.cpu_count() or 4) // 2, 8 if "h264_nvenc" in codec else 12))
    total = shots[-1].end
    console.print(f"rendering {len(shots)} shots / {total / 60:.1f} min as {len(jobs)} chunks, {workers} workers, {enc_name}, style={style}")
    t0 = time.time()
    done = 0
    paths: dict[int, str] = {}
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_render_chunk, j): i for i, j in enumerate(jobs)}
        for fut in as_completed(futs):
            paths[futs[fut]] = fut.result()
            done += 1
            el = time.time() - t0
            console.print(f"  chunk {done}/{len(jobs)}  ({el:.0f}s elapsed, ~{el / done * (len(jobs) - done):.0f}s left)")
            progress.emit("render", done, len(jobs), f"~{el / done * (len(jobs) - done):.0f}s left")

    # drop stale chunks from previous renders
    keep = set(paths.values())
    for old in render_dir.glob("chunk_*.mp4"):
        if str(old) not in keep:
            old.unlink(missing_ok=True)

    console.print("muxing narration + loudness normalisation")
    progress.emit("render", len(jobs), len(jobs), "muxing audio")
    mux_final([Path(paths[i]) for i in range(len(jobs))], narration, out, rcfg.loudness, chapters_meta, work_dir)


def encoder_info() -> str:
    return "h264_nvenc" if nvenc_available() else "libx264"
