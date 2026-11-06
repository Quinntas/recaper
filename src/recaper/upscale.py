"""Real-ESRGAN panel upscaling (anime / manga models) on the GPU, so small panels stay sharp when enlarged.

Only panels the renderer would enlarge are upscaled, once, to the size they are shown at (cached in
.recaper/upscaled/). Models are downloaded on first use to ~/.recaper/models/ and loaded with spandrel
(`uv sync --extra kokoro` or `--extra upscale`).

  fast     realesr-animevideov3 (compact, ~0.1 s per panel)
  quality  RealESRGAN_x4plus_anime_6B (sharper line art, several times slower)
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
import urllib.request
from pathlib import Path

import cv2
import numpy as np

from . import progress
from .log import console

MODEL_DIR = Path(os.environ.get("RECAPER_MODELS", Path.home() / ".recaper" / "models"))
MODELS = {
    "fast": ("realesr-animevideov3.pth",
             "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesr-animevideov3.pth",
             "b8a8376811077954d82ca3fcf476f1ac3da3e8a68a4f4d71363008000a18b75d"),
    "quality": ("RealESRGAN_x4plus_anime_6B.pth",
                "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.2.4/RealESRGAN_x4plus_anime_6B.pth",
                "f872d837d3c90ed2e05227bed711af5671a6fd1c9f7d7e91c911a61f155e99da"),
}  # fmt: skip
MIN_GAIN = 1.25  # panels enlarged less than this are left alone
MAX_SCALE = 4.0
HEADROOM = 1.2  # camera push-in / hit zoom on top of "fit the frame"
TILE = 1024


def resolve_mode(mode: str) -> tuple[str | None, str]:
    """-> (fast|quality|None, reason when None)."""
    mode = (mode or "auto").lower()
    if mode in ("off", "none", ""):
        return None, "upscaling is off"
    if mode not in ("auto", *MODELS):
        return None, f"unknown upscaler {mode!r} (auto | off | fast | quality)"
    try:
        import spandrel  # noqa: F401
        import torch
    except ImportError:
        return None, "install the upscaler: uv sync --extra kokoro (or --extra upscale)"
    if not torch.cuda.is_available():
        if mode == "auto":
            return None, "no CUDA GPU (set render.upscaler = fast to upscale on the CPU anyway - slow)"
    return ("fast" if mode == "auto" else mode), ""


def target_scale(w: int, h: int, frame_w: int, frame_h: int) -> float:
    """How much the renderer may enlarge this panel (fit-to-frame + camera headroom); 1 = no upscale needed."""
    need = min(frame_w / max(w, 1), frame_h / max(h, 1)) * HEADROOM
    return 1.0 if need < MIN_GAIN else round(min(need, MAX_SCALE), 2)


def _weights(mode: str) -> Path:
    name, url, sha = MODELS[mode]
    path = MODEL_DIR / name
    if path.exists():
        return path
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    console.print(f"downloading upscaler model {name}…")
    tmp = path.with_suffix(".part")
    urllib.request.urlretrieve(url, tmp)  # noqa: S310 - fixed https URL
    digest = hashlib.sha256(tmp.read_bytes()).hexdigest()
    if digest != sha:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"{name}: checksum mismatch")
    tmp.replace(path)
    return path


_lock = threading.Lock()


class _Model:
    def __init__(self, mode: str):
        import spandrel
        import torch

        self.torch = torch
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        desc = spandrel.ModelLoader(device=self.device).load_from_file(str(_weights(mode)))
        self.scale = desc.scale
        self.half = self.device == "cuda" and desc.supports_half
        model = desc.model.eval()
        self.model = model.half() if self.half else model

    def run(self, bgr: np.ndarray, size: tuple[int, int] | None = None) -> np.ndarray:
        """BGR uint8 -> BGR uint8 at `size` (w, h; default = model scale). Tiled so big panels fit in VRAM."""
        torch = self.torch
        h, w = bgr.shape[:2]
        s = self.scale
        if size and h <= TILE and w <= TILE:  # one tile: resize on the device, transfer only the final image
            with torch.inference_mode():
                t = torch.from_numpy(np.ascontiguousarray(bgr)).to(self.device).flip(-1).permute(2, 0, 1)[None]
                t = (t.half() if self.half else t.float()) / 255.0
                y_ = self.model(t).float()
                y_ = torch.nn.functional.interpolate(y_, size=(size[1], size[0]), mode="bicubic", antialias=True, align_corners=False)
                y_ = y_[0].clamp_(0, 1).mul_(255.0).round_()
                return y_.to(torch.uint8).permute(1, 2, 0).flip(-1).contiguous().cpu().numpy()
        out = self._tiled(bgr, h, w, s)
        if size and (out.shape[1], out.shape[0]) != size:
            out = cv2.resize(out, size, interpolation=cv2.INTER_AREA if out.shape[1] > size[0] else cv2.INTER_CUBIC)
        return out

    def _tiled(self, bgr: np.ndarray, h: int, w: int, s: int) -> np.ndarray:
        torch = self.torch
        out = np.zeros((h * s, w * s, 3), np.uint8)
        pad = 16
        with torch.inference_mode():
            for y in range(0, h, TILE):
                for x in range(0, w, TILE):
                    y0, x0 = max(y - pad, 0), max(x - pad, 0)
                    y1, x1 = min(y + TILE + pad, h), min(x + TILE + pad, w)
                    # uint8 in and out; all float work stays on the device (the CPU side was 3/4 of the time)
                    t = torch.from_numpy(np.ascontiguousarray(bgr[y0:y1, x0:x1])).to(self.device)
                    t = t.flip(-1).permute(2, 0, 1)[None]
                    t = (t.half() if self.half else t.float()) / 255.0
                    y_ = self.model(t)[0].clamp_(0, 1).mul_(255.0).round_()
                    res = y_.to(torch.uint8).permute(1, 2, 0).flip(-1).contiguous().cpu().numpy()
                    ty, tx = (y - y0) * s, (x - x0) * s
                    th, tw = (min(y + TILE, h) - y) * s, (min(x + TILE, w) - x) * s
                    out[y * s : y * s + th, x * s : x * s + tw] = res[ty : ty + th, tx : tx + tw]
        return out

    def close(self) -> None:
        del self.model
        if self.device == "cuda":
            self.torch.cuda.empty_cache()


def upscale_panels(meta: dict[str, dict], work_dir: Path, frame_w: int, frame_h: int, mode: str) -> dict[str, dict]:
    """Return a copy of `meta` ({pid: {file, w, h, ...}}, files relative to work_dir) where panels the renderer
    would enlarge point at upscaled copies. Failures fall back to the original panel."""
    resolved, why = resolve_mode(mode)
    todo = {pid: m for pid, m in meta.items() if target_scale(m["w"], m["h"], frame_w, frame_h) > 1}
    if not todo:
        return meta
    if not resolved:
        if mode not in ("off", "none", ""):
            console.print(f"[dim]upscaler skipped: {why}[/]")
        return meta
    out_dir = work_dir / "upscaled" / resolved
    out_dir.mkdir(parents=True, exist_ok=True)
    result = dict(meta)
    jobs = []
    for pid, m in todo.items():
        src = work_dir / m["file"]
        if not src.exists():
            continue
        s = target_scale(m["w"], m["h"], frame_w, frame_h)
        w2, h2 = round(m["w"] * s), round(m["h"] * s)
        st = src.stat()
        key = hashlib.sha1(f"{m['file']}|{st.st_size}|{int(st.st_mtime)}|{w2}x{h2}".encode()).hexdigest()[:12]
        dest = out_dir / f"{pid}_{key}.jpg"
        result[pid] = {**m, "file": dest.relative_to(work_dir).as_posix(), "w": w2, "h": h2}
        if not dest.exists():
            jobs.append((pid, src, dest, w2, h2))
    if jobs:
        t0 = time.time()
        console.print(f"upscaling {len(jobs)} panels ({resolved}, {len(todo) - len(jobs)} cached)")
        with _lock:
            try:
                model = _Model(resolved)
            except Exception as e:  # noqa: BLE001 - never block a render on the upscaler
                console.print(f"[yellow]upscaler unavailable ({e}); using the original panels[/]")
                return meta
            try:
                for i, (pid, src, dest, w2, h2) in enumerate(jobs, start=1):
                    try:
                        img = cv2.imread(str(src), cv2.IMREAD_COLOR)
                        big = model.run(img, (w2, h2))
                        tmp = dest.with_suffix(".part.jpg")
                        cv2.imwrite(str(tmp), big, [cv2.IMWRITE_JPEG_QUALITY, 94])
                        tmp.replace(dest)
                    except Exception as e:  # noqa: BLE001
                        console.print(f"[yellow]upscale failed for {pid}: {e}[/]")
                        result[pid] = meta[pid]
                    progress.emit("render", 0, 1, f"upscaling panels {i}/{len(jobs)}")
            finally:
                model.close()
        console.print(f"  upscaled in {time.time() - t0:.0f}s")
        from . import usage

        usage.record("upscale", mode=resolved, panels=len(jobs), seconds=round(time.time() - t0, 1))
    return result
