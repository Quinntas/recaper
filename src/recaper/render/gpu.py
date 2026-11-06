"""GPU (CUDA, via torch) compositing for the common frame types, so render workers mostly just copy bytes.

A 1080p frame on the CPU costs ~6 ms alone but ~19 ms when 8 workers + 8 encoders share the CPU (memory
bandwidth), which capped rendering at ~430 fps while NVENC takes ~1000. Settled frames (the camera warp of a
cached layer, scrolling tall panels drawn on top, beat cross-fades) are built here and converted to I420 on the
device: one 3 MB download per frame. Transitions with moving cards stay on the CPU path.

Matches the CPU path within ~1-2 levels (bilinear sampling + BT.601 limited-range I420 like cv2's BGR2YUV_I420).
RECAPER_GPU_WARP=0 disables it.
"""

from __future__ import annotations

import os
from collections import OrderedDict

import numpy as np

_state: dict = {}


def get() -> "Gpu | None":
    if "gpu" not in _state:
        _state["gpu"] = None
        if os.environ.get("RECAPER_GPU_WARP", "1") != "0":
            try:
                import torch

                if torch.cuda.is_available():
                    _state["gpu"] = Gpu(torch)
            except Exception:  # noqa: BLE001 - no torch / broken CUDA: CPU path
                pass
    return _state["gpu"]


def disable() -> None:
    _state["gpu"] = None


class Gpu:
    def __init__(self, torch):
        self.torch = torch
        self.F = torch.nn.functional
        self.dev = torch.device("cuda")
        self._tex: OrderedDict = OrderedDict()  # id(array) -> (array, tensor 1x3xHxW half)
        self._axes: dict = {}
        t = torch.tensor
        # BGR -> BT.601 limited range, same as cv2.COLOR_BGR2YUV_I420
        self._ky = t([0.0978, 0.50414, 0.25702], device=self.dev).view(1, 3, 1, 1)
        self._ku = t([0.43916, -0.29119, -0.14797], device=self.dev).view(1, 3, 1, 1)
        self._kv = t([-0.07085, -0.36772, 0.43888], device=self.dev).view(1, 3, 1, 1)

    # ---------------------------------------------------------------- textures
    def tex(self, arr: np.ndarray):
        """Upload once per cached array (we keep a reference, so its id can't be recycled while cached)."""
        ent = self._tex.get(id(arr))
        if ent is None or ent[0] is not arr:
            t = self.torch.from_numpy(np.ascontiguousarray(arr)).to(self.dev)
            t = (t[None, None] if t.ndim == 2 else t.permute(2, 0, 1)[None]).half()  # 1-channel alpha sprites too
            self._tex[id(arr)] = ent = (arr, t)
            while len(self._tex) > 12:
                self._tex.popitem(last=False)
        else:
            self._tex.move_to_end(id(arr))
        return ent[1]

    def _axis(self, n: int):
        if n not in self._axes:
            self._axes[n] = self.torch.arange(n, device=self.dev, dtype=self.torch.float32)
        return self._axes[n]

    # ---------------------------------------------------------------- ops
    def warp(self, layer: np.ndarray, scale: float, dx: float, dy: float):
        """Camera move of a whole layer (zoom about the centre + offset), like compose.warp_frame."""
        src = self.tex(layer)
        h, w = layer.shape[:2]
        if abs(scale - 1) < 1e-6 and abs(dx) < 1e-3 and abs(dy) < 1e-3:
            return src.clone()
        cx, cy = w / 2, h / 2
        return self._sample(src, w, h, scale, cx - cx * scale + dx, cy - cy * scale + dy, 0, 0, w, h, "reflection")

    def _sample(self, src, sw: int, sh: int, s: float, tx: float, ty: float, x0: int, y0: int, x1: int, y1: int, pad: str):
        """Sample `src` (size sw x sh) for destination pixels [x0,x1) x [y0,y1) under dst = s*src + (tx, ty)."""
        ax = self._axis(max(x1, 1))[x0:x1]
        ay = self._axis(max(y1, 1))[y0:y1]
        gx = (2 * ((ax - tx) / s) + 1) / sw - 1
        gy = (2 * ((ay - ty) / s) + 1) / sh - 1
        grid = self.torch.stack((gx[None, :].expand(len(ay), len(ax)), gy[:, None].expand(len(ay), len(ax))), dim=-1)[None]
        return self.F.grid_sample(src, grid.to(src.dtype), mode="bilinear", padding_mode=pad, align_corners=False)

    def blit(self, canvas, src, s: float, X: float, Y: float, alpha: float = 1.0) -> None:
        """Draw `src` (1x3xhxw) scaled by `s` with its top-left at (X, Y), anti-aliased edges (like compose.blit)."""
        if alpha <= 0.003:
            return
        H, W = canvas.shape[2:]
        sh, sw = src.shape[2:]
        x0, y0 = max(int(np.floor(X)), 0), max(int(np.floor(Y)), 0)
        x1, y1 = min(int(np.ceil(X + sw * s)), W), min(int(np.ceil(Y + sh * s)), H)
        if x1 <= x0 or y1 <= y0:
            return
        img = self._sample(src, sw, sh, s, X, Y, x0, y0, x1, y1, "zeros")
        # coverage = bilinear sample of an all-ones image with zero padding; separable for an axis-aligned
        # scale, so two 1-D ramps instead of sampling a 4th channel (same anti-aliased edges as compose.blit)
        u = (self._axis(x1)[x0:x1] - X) / s
        v = (self._axis(y1)[y0:y1] - Y) / s
        wx = self.torch.minimum(u + 1, sw - u).clamp_(0, 1)
        wy = self.torch.minimum(v + 1, sh - v).clamp_(0, 1)
        m = (wy[:, None] * wx[None, :] * alpha).to(img.dtype)[None, None]
        region = canvas[:, :, y0:y1, x0:x1]
        canvas[:, :, y0:y1, x0:x1] = img * m + region * (1 - m)

    def shadow(self, canvas, sprite, s: float, X: float, Y: float, strength: float) -> None:
        """Darken the canvas under an alpha sprite (1x1xhxw, 0-1) scaled by `s` at (X, Y), like compose.blit_shadow."""
        if strength <= 0.003:
            return
        H, W = canvas.shape[2:]
        sh, sw = sprite.shape[2:]
        x0, y0 = max(int(np.floor(X)), 0), max(int(np.floor(Y)), 0)
        x1, y1 = min(int(np.ceil(X + sw * s)), W), min(int(np.ceil(Y + sh * s)), H)
        if x1 <= x0 or y1 <= y0:
            return
        a = self._sample(sprite, sw, sh, s, X, Y, x0, y0, x1, y1, "zeros")
        canvas[:, :, y0:y1, x0:x1] = canvas[:, :, y0:y1, x0:x1] * (1 - a * strength)

    @staticmethod
    def blend(a, b, w: float):
        return a * w + b * (1 - w)

    def to_i420(self, frame) -> np.ndarray:
        """1x3xHxW BGR (0-255, half/float) -> flat I420 uint8 bytes on the host."""
        f = frame.float()
        y = (f * self._ky).sum(1, keepdim=True) + 16
        small = self.F.avg_pool2d(f, 2)
        u = (small * self._ku).sum(1, keepdim=True) + 128
        v = (small * self._kv).sum(1, keepdim=True) + 128
        flat = self.torch.cat((y.flatten(), u.flatten(), v.flatten()))
        return flat.round_().clamp_(0, 255).to(self.torch.uint8).cpu().numpy()
