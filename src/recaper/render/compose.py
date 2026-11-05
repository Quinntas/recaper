"""Low-level compositing: sub-pixel affine blits with alpha, soft shadows, easing."""

from __future__ import annotations

import math
from functools import lru_cache

import cv2
import numpy as np

# ------------------------------------------------------------------ easing


def clamp01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def ease_out_cubic(t: float) -> float:
    t = clamp01(t)
    return 1 - (1 - t) ** 3


def ease_in_out_cubic(t: float) -> float:
    t = clamp01(t)
    return 4 * t**3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


def ease_out_back(t: float, s: float = 1.4) -> float:
    t = clamp01(t)
    c3 = s + 1
    return 1 + c3 * (t - 1) ** 3 + s * (t - 1) ** 2


def ease_in_out_sine(t: float) -> float:
    t = clamp01(t)
    return -(math.cos(math.pi * t) - 1) / 2


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


# ------------------------------------------------------------------ blits


@lru_cache(maxsize=64)
def _ones(h: int, w: int) -> np.ndarray:
    return np.full((h, w), 255, np.uint8)


def blit(canvas: np.ndarray, src: np.ndarray, M: np.ndarray, alpha: float = 1.0) -> None:
    """Draw `src` (H,W,3 uint8) onto `canvas` with affine `M` (2x3, src->canvas), anti-aliased edges."""
    if alpha <= 0.003:
        return
    h, w = src.shape[:2]
    corners = np.array([[0, 0, 1], [w, 0, 1], [0, h, 1], [w, h, 1]], np.float32) @ M.T
    x0 = max(int(math.floor(corners[:, 0].min())), 0)
    y0 = max(int(math.floor(corners[:, 1].min())), 0)
    x1 = min(int(math.ceil(corners[:, 0].max())), canvas.shape[1])
    y1 = min(int(math.ceil(corners[:, 1].max())), canvas.shape[0])
    if x1 <= x0 or y1 <= y0:
        return
    Ml = M.astype(np.float64).copy()
    Ml[0, 2] -= x0
    Ml[1, 2] -= y0
    size = (x1 - x0, y1 - y0)
    warped = cv2.warpAffine(src, Ml, size, flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    mask = cv2.warpAffine(_ones(h, w), Ml, size, flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    region = canvas[y0:y1, x0:x1]
    if alpha >= 0.997 and abs(M[0, 1]) < 1e-9 and abs(M[1, 0]) < 1e-9:
        # Opaque, axis-aligned (always the case here): memcpy the fully covered interior and
        # alpha-blend only the 1-2px anti-aliased edge strips. Float blends over whole cards are
        # what made rendering memory-bandwidth bound.
        ix0 = min(max(int(math.ceil(corners[:, 0].min())) - x0, 0), size[0])
        iy0 = min(max(int(math.ceil(corners[:, 1].min())) - y0, 0), size[1])
        ix1 = max(min(int(math.floor(corners[:, 0].max())) - x0, size[0]), ix0)
        iy1 = max(min(int(math.floor(corners[:, 1].max())) - y0, size[1]), iy0)
        region[iy0:iy1, ix0:ix1] = warped[iy0:iy1, ix0:ix1]
        for ys, xs in (
            (slice(0, iy0), slice(None)),
            (slice(iy1, None), slice(None)),
            (slice(iy0, iy1), slice(0, ix0)),
            (slice(iy0, iy1), slice(ix1, None)),
        ):
            m = mask[ys, xs]
            if m.size:
                w = cv2.multiply(m, 1 / 255.0, dtype=cv2.CV_32F)
                region[ys, xs] = cv2.blendLinear(warped[ys, xs], region[ys, xs], w, cv2.subtract(1.0, w))
        return
    wa = cv2.multiply(mask, alpha / 255.0, dtype=cv2.CV_32F)  # C++ paths only: numpy masking is ~10x slower
    canvas[y0:y1, x0:x1] = cv2.blendLinear(warped, region, wa, cv2.subtract(1.0, wa))


def warp_frame(img: np.ndarray, scale: float, dx: float, dy: float) -> np.ndarray:
    """Whole-frame camera move (zoom about the centre + offset) - one warp instead of re-compositing."""
    h, w = img.shape[:2]
    if abs(scale - 1) < 1e-6 and abs(dx) < 1e-3 and abs(dy) < 1e-3:
        return img.copy()
    cx, cy = w / 2, h / 2
    M = np.array([[scale, 0, cx - cx * scale + dx], [0, scale, cy - cy * scale + dy]], np.float64)
    return cv2.warpAffine(img, M, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)


def shadow_sprite(w: int, h: int, radius: int) -> tuple[np.ndarray, int]:
    """Blurred rectangle alpha (float32 0..1) padded by `pad` on each side."""
    pad = radius * 2
    a = np.zeros((h + 2 * pad, w + 2 * pad), np.float32)
    a[pad : pad + h, pad : pad + w] = 1.0
    k = radius * 2 + 1
    a = cv2.GaussianBlur(a, (k, k), radius / 2)
    return a, pad


def blit_shadow(canvas: np.ndarray, sprite: np.ndarray, M: np.ndarray, strength: float) -> None:
    """Darken canvas under an alpha sprite placed with affine M."""
    if strength <= 0.003:
        return
    h, w = sprite.shape[:2]
    corners = np.array([[0, 0, 1], [w, 0, 1], [0, h, 1], [w, h, 1]], np.float32) @ M.T
    x0 = max(int(math.floor(corners[:, 0].min())), 0)
    y0 = max(int(math.floor(corners[:, 1].min())), 0)
    x1 = min(int(math.ceil(corners[:, 0].max())), canvas.shape[1])
    y1 = min(int(math.ceil(corners[:, 1].max())), canvas.shape[0])
    if x1 <= x0 or y1 <= y0:
        return
    Ml = M.astype(np.float64).copy()
    Ml[0, 2] -= x0
    Ml[1, 2] -= y0
    a = cv2.warpAffine(sprite, Ml, (x1 - x0, y1 - y0), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    dark = cv2.multiply(a, float(strength))
    region = canvas[y0:y1, x0:x1]
    canvas[y0:y1, x0:x1] = cv2.blendLinear(_zeros(*region.shape), region, dark, cv2.subtract(1.0, dark))


@lru_cache(maxsize=16)
def _zeros(h: int, w: int, c: int) -> np.ndarray:
    return np.zeros((h, w, c), np.uint8)


def affine(scale: float, tx: float, ty: float) -> np.ndarray:
    return np.array([[scale, 0, tx], [0, scale, ty]], np.float64)


def resize_to(img: np.ndarray, w: int, h: int) -> np.ndarray:
    w, h = max(1, int(round(w))), max(1, int(round(h)))
    interp = cv2.INTER_AREA if w < img.shape[1] else cv2.INTER_CUBIC
    return cv2.resize(img, (w, h), interpolation=interp)


def paper_background(w: int, h: int, seed: int = 7) -> np.ndarray:
    """Warm off-white paper with faint grain and a soft vignette (motion-comic style)."""
    rng = np.random.default_rng(seed)
    base = np.empty((h, w, 3), np.float32)
    base[:] = (236, 240, 243)  # BGR, slightly warm
    grain = cv2.GaussianBlur(rng.normal(0, 1, (h // 2, w // 2)).astype(np.float32), (0, 0), 0.8)
    grain = cv2.resize(grain, (w, h)) * 3.0
    base += grain[..., None]
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    d = np.sqrt(((xx - w / 2) / (w / 2)) ** 2 + ((yy - h / 2) / (h / 2)) ** 2)
    base *= (1 - 0.10 * np.clip(d - 0.35, 0, None) ** 1.5)[..., None]
    return np.clip(base, 0, 255).astype(np.uint8)


def blurred_cover(img: np.ndarray, w: int, h: int, overscan: float = 1.12, blur: float = 6.0, dim: float = 0.5) -> np.ndarray:
    """Panel scaled to cover the frame, heavily blurred and darkened (blur-fill style)."""
    W, H = int(w * overscan), int(h * overscan)
    s = max(W / img.shape[1], H / img.shape[0])
    small_w, small_h = max(8, int(img.shape[1] * s / 8)), max(8, int(img.shape[0] * s / 8))
    small = cv2.resize(img, (small_w, small_h), interpolation=cv2.INTER_AREA)
    small = cv2.GaussianBlur(small, (0, 0), blur) if blur > 0 else small
    big = cv2.resize(small, (int(img.shape[1] * s), int(img.shape[0] * s)), interpolation=cv2.INTER_LINEAR)
    y0 = (big.shape[0] - H) // 2
    x0 = (big.shape[1] - W) // 2
    big = big[y0 : y0 + H, x0 : x0 + W].astype(np.float32)
    gray = big.mean(axis=2, keepdims=True)
    big = (big * 0.7 + gray * 0.3) * max(0.0, min(dim, 1.0))
    return np.clip(big, 0, 255).astype(np.uint8)
