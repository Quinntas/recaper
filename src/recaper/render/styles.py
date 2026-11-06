"""Visual styles.

motion   - ref #1 (Actiontoon): panels are cards on a paper background; new panels slide in with
           a slight overshoot, shared panels glide to their new spot, old ones slide out; slow
           camera push, punch-in + shake on "hit" beats; tall panels scroll inside their card.
blurfill - ref #3 (Tobs): one panel at a time centred over a blurred, darkened copy of itself,
           Ken Burns zoom alternating in/out, tall panels pan top->bottom, quick cross-dissolves.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np

from .compose import (
    affine,
    blit,
    blit_shadow,
    blurred_cover,
    clamp01,
    ease_in_out_cubic,
    ease_in_out_sine,
    ease_out_back,
    lerp,
    paper_background,
    resize_to,
    shadow_sprite,
    warp_frame,
)


@dataclass
class Look:
    """Every visual option (mirrors the look fields of RenderConfig)."""

    background: str = "paper"
    layout: str = "cascade"
    reveal: str = "progressive"
    card_border: bool = True
    shadow: float = 1.0
    camera: float = 1.0
    hit_effect: float = 1.0
    transition_speed: float = 1.0
    fill: float = 0.92
    max_upscale: float = 2.4
    bg_blur: float = 6.0
    bg_dim: float = 0.5

    @classmethod
    def from_dict(cls, d: dict | None) -> "Look":
        names = cls.__dataclass_fields__
        return cls(**{k: v for k, v in (d or {}).items() if k in names})


BG_PRESETS = {"paper": None, "white": (248, 248, 248), "black": (10, 10, 12), "dark": (34, 30, 28)}  # BGR


def parse_color(spec: str) -> tuple[int, int, int] | None:
    """'#rrggbb' -> BGR tuple."""
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", spec.strip())
    if not m:
        return None
    v = m.group(1)
    r, g, b = int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)
    return (b, g, r)


def make_background(spec: str, w: int, h: int) -> np.ndarray | None:
    """Static background for the motion style; None means per-shot blurred panel ('blur')."""
    spec = (spec or "paper").strip().lower()
    if spec == "blur":
        return None
    if spec == "paper":
        return paper_background(w, h)
    color = BG_PRESETS.get(spec) or parse_color(spec)
    if color is None:
        raise ValueError(f"unknown background {spec!r}: use paper, white, black, dark, blur or #rrggbb")
    base = np.empty((h, w, 3), np.float32)
    base[:] = color
    # soft vignette (lighter centre on dark colours, darker edges on light ones) so it isn't flat
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    d = np.sqrt(((xx - w / 2) / (w / 2)) ** 2 + ((yy - h / 2) / (h / 2)) ** 2)
    if sum(color) / 3 < 90:
        base += (14 * np.clip(1 - d, 0, 1) ** 1.5)[..., None]
    else:
        base *= (1 - 0.08 * np.clip(d - 0.35, 0, None) ** 1.5)[..., None]
    return np.clip(base, 0, 255).astype(np.uint8)


def is_dark(img: np.ndarray) -> bool:
    return float(img[:: max(1, img.shape[0] // 40), :: max(1, img.shape[1] // 40)].mean()) < 110


@dataclass
class PanelInfo:
    path: str
    w: int
    h: int


@dataclass
class Shot:
    start: float
    end: float
    panels: list[str]
    emphasis: str = "normal"
    index: int = 0  # global shot index (used to alternate motion)

    @property
    def dur(self) -> float:
        return self.end - self.start


@dataclass
class Rect:
    x: float
    y: float
    w: float
    h: float  # visible height
    content_h: float  # full height of the scaled panel (> h when it scrolls)

    def lerp(self, other: "Rect", t: float) -> "Rect":
        return Rect(*(lerp(a, b, t) for a, b in zip(self.astuple(), other.astuple())))

    def astuple(self):
        return (self.x, self.y, self.w, self.h, self.content_h)


def _read(path: str) -> np.ndarray:
    img = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError(f"can't read panel {path}")
    return img


def camera(emphasis: str, t: float, d: float, push: float = 1.0, hit: float = 1.0) -> tuple[float, float, float]:
    """(scale, dx, dy) for the whole frame. push/hit scale the slow push and the punch+shake."""
    p = ease_in_out_sine(t / max(d, 1e-3))
    if emphasis == "hit" and hit > 0:
        punch = 0.07 * hit * math.exp(-t / 0.16)
        decay = hit * math.exp(-t / 0.12)
        return 1 + 0.025 * push * p + punch, 10 * decay * math.sin(t * 70), 7 * decay * math.cos(t * 55)
    if emphasis == "reveal":
        return 1 + 0.07 * push * p, 0.0, 0.0
    return 1 + 0.03 * push * p, 0.0, 0.0


class _Base:
    def __init__(self, width: int, height: int, panels: dict[str, PanelInfo], look: Look | None = None):
        self.W, self.H = width, height
        self.panels = panels
        self.look = look or Look()
        self.border_color = (38, 38, 38)
        self.speed = max(0.2, self.look.transition_speed)
        self._full = lru_cache(maxsize=24)(lambda pid: _read(self.panels[pid].path))
        self._scaled = lru_cache(maxsize=48)(self._make_scaled)
        self._shadow = lru_cache(maxsize=48)(lambda w, h: shadow_sprite(w, h, radius=max(8, int(self.H * 0.018))))

    def _make_scaled(self, pid: str, w: int, h: int, border: bool) -> np.ndarray:
        img = resize_to(self._full(pid), w, h)
        if border:
            cv2.rectangle(img, (0, 0), (img.shape[1] - 1, img.shape[0] - 1), self.border_color, 2)
        return img

    def card_gpu(self, gpu, canvas, pid: str, base: Rect, rect: Rect, alpha: float, cam: tuple[float, float, float],
                 scroll: float, border: bool, shadow: float = 0.0) -> None:
        """draw_card on the GPU canvas (same geometry, shadow and anti-aliased edges)."""
        bw, bch = int(round(base.w)), int(round(base.content_h))
        img = self._scaled(pid, bw, bch, border)
        f = rect.w / max(base.w, 1)
        s, dx, dy = cam
        cx, cy = self.W / 2, self.H / 2
        X = cx + (rect.x - cx) * s + dx
        Y = cy + (rect.y - cy) * s + dy
        sc = f * s
        tex = gpu.tex(img)
        view_h = int(round(base.h))
        if bch > view_h + 1:  # scrolling card: crop rows on the device (the texture is uploaded once)
            off = (bch - view_h) * clamp01(scroll)
            oi = int(off)
            tex = tex[:, :, oi : oi + view_h + 1]
            Y -= (off - oi) * sc
        shadow *= self.look.shadow
        if shadow > 0:
            sprite, pad = self._shadow(tex.shape[3], min(tex.shape[2], view_h))
            off_x, off_y = self.H * 0.008, self.H * 0.012
            gpu.shadow(canvas, gpu.tex(sprite), sc, X + (off_x - pad) * sc, Y + (off_y - pad) * sc, shadow * alpha)
        gpu.blit(canvas, tex, sc, X, Y, alpha)

    def draw_card(
        self,
        canvas: np.ndarray,
        pid: str,
        base: Rect,
        rect: Rect,
        alpha: float,
        cam: tuple[float, float, float],
        scroll: float,
        border: bool,
        shadow: float,
        image: bool = True,
    ) -> None:
        """Draw panel `pid` laid out at `base` size, currently at `rect`, through camera `cam`."""
        bw, bch = int(round(base.w)), int(round(base.content_h))
        img = self._scaled(pid, bw, bch, border)
        f = rect.w / max(base.w, 1)  # transition scale relative to base
        s, dx, dy = cam
        cx, cy = self.W / 2, self.H / 2
        X = cx + (rect.x - cx) * s + dx
        Y = cy + (rect.y - cy) * s + dy
        sc = f * s
        view_h = int(round(base.h))
        if bch > view_h + 1:  # scrolling card
            off = (bch - view_h) * clamp01(scroll)
            oi = int(off)
            img = img[oi : oi + view_h + 1]
            Y -= (off - oi) * sc
        shadow *= self.look.shadow
        if shadow > 0:
            sprite, pad = self._shadow(img.shape[1], min(img.shape[0], view_h))
            off_x, off_y = self.H * 0.008, self.H * 0.012
            blit_shadow(canvas, sprite, affine(sc, X + (off_x - pad) * sc, Y + (off_y - pad) * sc), shadow * alpha)
        if image:
            blit(canvas, img, affine(sc, X, Y), alpha)


# --------------------------------------------------------------------- motion


class MotionStyle(_Base):
    name = "motion"

    def __init__(self, width: int, height: int, panels: dict[str, PanelInfo], look: Look | None = None):
        super().__init__(width, height, panels, look)
        lk = self.look
        self.bg = make_background(lk.background, width, height)  # None = per-shot blurred panel
        if self.bg is None or is_dark(self.bg):
            self.border_color = (215, 215, 215)  # light hairline reads better on dark/blurred backgrounds
        fill = min(max(lk.fill, 0.5), 1.0)
        self.mx, self.my, self.gap = width * (1 - fill) / 2, height * ((1 - fill) / 2 + 0.01), width * 0.019
        self.T_IN, self.T_MOVE, self.T_OUT, self.STAGGER = (x / self.speed for x in (0.5, 0.45, 0.35, 0.12))
        # Multi-panel beats cascade (each panel big, newest on top) like the reference motion-comic
        # recaps; side-by-side rows shrink panels and leave half-empty frames during progressive reveal.
        self.cascade = lk.layout != "row"
        self._blur_bg = lru_cache(maxsize=6)(
            lambda pid: blurred_cover(self._full(pid), self.W, self.H, overscan=1.0, blur=lk.bg_blur, dim=lk.bg_dim)
        )
        self._layout = lru_cache(maxsize=64)(self._compute_layout)
        self._settled = lru_cache(maxsize=4)(self._settled_layer)

    def layout(self, pids: list[str]) -> dict[str, Rect]:
        return self._layout(tuple(pids))

    def _compute_layout(self, pids: tuple[str, ...]) -> dict[str, Rect]:
        aw, ah = self.W - 2 * self.mx, self.H - 2 * self.my
        infos = [self.panels[p] for p in pids]
        ars = [i.w / max(i.h, 1) for i in infos]
        if len(pids) == 1:
            ar, info = ars[0], infos[0]
            dw = min(aw, ah * ar, info.w * self.look.max_upscale)
            if ar < 0.8:  # tall: zoom to a wider card and scroll down it (fitting height = thin column)
                dw = min(max(dw, aw * 0.55), info.w * self.look.max_upscale)
            content_h = dw / ar
            vh = min(content_h, ah)
            return {pids[0]: Rect((self.W - dw) / 2, (self.H - vh) / 2, dw, vh, content_h)}

        k = len(pids)
        # row: equal heights; column: equal widths. Pick the arrangement with more panel area.
        row_h = min(ah, (aw - self.gap * (k - 1)) / sum(ars))
        row_area = sum(row_h * row_h * a for a in ars)
        col_w = min(aw, (ah - self.gap * (k - 1)) / sum(1 / a for a in ars))
        col_area = sum(col_w * col_w / a for a in ars)
        out: dict[str, Rect] = {}
        if self.cascade or max(row_area, col_area) / k < 0.40 * aw * ah:
            # Side by side would make every panel small. Cascade instead (like the reference
            # motion-comic recaps): each panel near full size, overlapping the previous one with an
            # offset, newest on top - shown progressively as the narration reaches it.
            for j, (pid, info, a) in enumerate(zip(pids, infos, ars)):
                dw = min(aw * 0.86, ah * 0.94 * a, info.w * self.look.max_upscale)
                if a < 0.8:  # tall: wider + scrolling rather than a thin column
                    dw = min(max(dw, aw * 0.5), info.w * self.look.max_upscale)
                dh = dw / a
                vh = min(dh, ah * 0.92)
                off = j - (k - 1) / 2
                cx = self.W / 2 + off * self.W * 0.11
                cy = self.H / 2 + off * self.H * 0.035
                out[pid] = Rect(cx - dw / 2, cy - vh / 2, dw, vh, dh)
            return out
        if row_area >= col_area:
            total_w = sum(row_h * a for a in ars) + self.gap * (k - 1)
            x = (self.W - total_w) / 2
            y = (self.H - row_h) / 2
            for pid, a in zip(pids, ars):
                out[pid] = Rect(x, y, row_h * a, row_h, row_h)
                x += row_h * a + self.gap
        else:
            total_h = sum(col_w / a for a in ars) + self.gap * (k - 1)
            y = (self.H - total_h) / 2
            x = (self.W - col_w) / 2
            for pid, a in zip(pids, ars):
                out[pid] = Rect(x, y, col_w, col_w / a, col_w / a)
                y += col_w / a + self.gap
        return out

    def background(self, pids) -> np.ndarray:
        if self.bg is not None:
            return self.bg
        return self._blur_bg(pids[0])

    def _settled_layer(self, pids: tuple[str, ...]) -> np.ndarray:
        """Background + shadows + non-scrolling cards at rest (camera at identity)."""
        canvas = self.background(pids).copy()
        ident = (1.0, 0.0, 0.0)
        for pid, r in self.layout(list(pids)).items():
            scrolling = r.content_h > r.h + 1
            # scrolling cards: shadow only here, their content is drawn per frame
            self.draw_card(canvas, pid, r, r, 1.0, ident, 0.0, border=self.look.card_border, shadow=0.35, image=not scrolling)
        return canvas

    def _entries(self, cur: dict, prv: dict, dur: float) -> tuple[dict[str, float], float]:
        """When each new card starts entering, and when the shot settles.

        New cards of a beat are revealed progressively across the beat (like the narration moves
        through them) instead of all at once - the reference recaps change something on screen
        every few seconds."""
        new = [p for p in cur if p not in prv]
        leaving = any(p not in cur for p in prv)
        delay = self.T_OUT * 0.45 if leaving else 0.0
        progressive = self.look.reveal != "together"
        spacing = (max(self.STAGGER, 0.8 * dur / max(len(new), 1)) if progressive else self.STAGGER) if len(new) > 1 else 0.0
        starts = {pid: delay + j * spacing for j, pid in enumerate(new)}
        settle = max(self.T_MOVE if prv else 0.0, self.T_OUT if leaving else 0.0)
        if new:
            settle = max(settle, max(starts.values()) + self.T_IN)
        return starts, settle

    def frame(self, shot: Shot, prev: Shot | None, t: float) -> np.ndarray:
        cur = self.layout(shot.panels)
        prv = self.layout(prev.panels) if prev else {}
        cam = camera(shot.emphasis, t, shot.dur, self.look.camera, self.look.hit_effect)
        scroll = ease_in_out_sine(clamp01((t - 0.3) / max(shot.dur - 0.5, 0.5)))
        starts, settle = self._entries(cur, prv, shot.dur)

        if t >= settle:
            # steady state: one cached composition, camera applied as a single whole-frame warp
            # (frame_gpu does the same on the GPU)
            frame = warp_frame(self._settled(tuple(shot.panels)), *cam)
            for pid, r in cur.items():
                if r.content_h > r.h + 1:
                    self.draw_card(frame, pid, r, r, 1.0, cam, scroll, border=self.look.card_border, shadow=0.0)
            return frame

        return self._transition(shot, prev, t, cur, prv, cam, scroll, starts)

    def frame_gpu(self, gpu, shot: Shot, prev: Shot | None, t: float):
        """Every motion frame on the GPU: settled = warp of the cached layer, transitions = cards composed on device."""
        cur = self.layout(shot.panels)
        prv = self.layout(prev.panels) if prev else {}
        starts, settle = self._entries(cur, prv, shot.dur)
        cam = camera(shot.emphasis, t, shot.dur, self.look.camera, self.look.hit_effect)
        scroll = ease_in_out_sine(clamp01((t - 0.3) / max(shot.dur - 0.5, 0.5)))
        if t < settle:
            return self._transition_gpu(gpu, shot, prev, t, cur, prv, cam, scroll, starts)
        frame = gpu.warp(self._settled(tuple(shot.panels)), *cam)
        for pid, r in cur.items():
            if r.content_h > r.h + 1:
                self.card_gpu(gpu, frame, pid, r, r, 1.0, cam, scroll, border=self.look.card_border)
        return frame

    def _transition_gpu(self, gpu, shot: Shot, prev: Shot | None, t: float, cur: dict, prv: dict, cam, scroll: float, starts: dict):
        """_transition on the GPU (keep the two in step)."""
        slide = self.W * 0.38
        canvas = gpu.tex(self.background(shot.panels)).clone()
        if self.bg is None and prev is not None and prev.panels[0] != shot.panels[0]:
            canvas = gpu.blend(canvas, gpu.tex(self.background(prev.panels)), clamp01(t / self.T_OUT))
        border = self.look.card_border
        for pid, r in prv.items():
            if pid in cur:
                continue
            p = ease_in_out_cubic(t / self.T_OUT)
            if p >= 1:
                continue
            k = 1 - 0.08 * p
            moved = Rect(r.x + r.w * (1 - k) / 2 - self.W * 0.03 * p, r.y + r.h * (1 - k) / 2, r.w * k, r.h * k, r.content_h * k)
            self.card_gpu(gpu, canvas, pid, r, moved, 1 - p, cam, 1.0, border, shadow=0.35 * (1 - p))
        for pid, r in cur.items():
            if pid in prv:
                q = ease_in_out_cubic(t / self.T_MOVE)
                self.card_gpu(gpu, canvas, pid, r, prv[pid].lerp(r, q), 1.0, cam, scroll, border, shadow=0.35)
                continue
            q = (t - starts[pid]) / self.T_IN
            if q <= 0:
                continue
            e = ease_out_back(q)
            moved = Rect(r.x + slide * (1 - e), r.y, r.w, r.h, r.content_h)
            self.card_gpu(gpu, canvas, pid, r, moved, clamp01(q * 2.5), cam, scroll, border, shadow=0.35)
        return canvas

    def _transition(self, shot: Shot, prev: Shot | None, t: float, cur: dict, prv: dict, cam, scroll: float, starts: dict) -> np.ndarray:
        slide = self.W * 0.38
        canvas = self.background(shot.panels).copy()
        if self.bg is None and prev is not None and prev.panels[0] != shot.panels[0]:
            q = clamp01(t / self.T_OUT)  # blurred backgrounds cross-fade between beats
            canvas = cv2.addWeighted(canvas, q, self.background(prev.panels), 1 - q, 0)

        for pid, r in prv.items():  # leaving: quick shrink + fade in place (sliding would cross the new card)
            if pid in cur:
                continue
            p = ease_in_out_cubic(t / self.T_OUT)
            if p >= 1:
                continue
            k = 1 - 0.08 * p
            moved = Rect(r.x + r.w * (1 - k) / 2 - self.W * 0.03 * p, r.y + r.h * (1 - k) / 2, r.w * k, r.h * k, r.content_h * k)
            self.draw_card(canvas, pid, r, moved, 1 - p, cam, 1.0, border=self.look.card_border, shadow=0.35 * (1 - p))

        for pid, r in cur.items():
            if pid in prv:
                q = ease_in_out_cubic(t / self.T_MOVE)
                self.draw_card(canvas, pid, r, prv[pid].lerp(r, q), 1.0, cam, scroll, border=self.look.card_border, shadow=0.35)
                continue
            q = (t - starts[pid]) / self.T_IN
            if q <= 0:
                continue
            e = ease_out_back(q)
            moved = Rect(r.x + slide * (1 - e), r.y, r.w, r.h, r.content_h)
            self.draw_card(canvas, pid, r, moved, clamp01(q * 2.5), cam, scroll, border=self.look.card_border, shadow=0.35)
        return canvas


# ------------------------------------------------------------------- blurfill


class BlurFillStyle(_Base):
    name = "blurfill"

    def __init__(self, width: int, height: int, panels: dict[str, PanelInfo], look: Look | None = None):
        super().__init__(width, height, panels, look)
        lk = self.look
        self.XFADE = 0.2 / self.speed
        self._bg = lru_cache(maxsize=8)(lambda pid: blurred_cover(self._full(pid), self.W, self.H, blur=lk.bg_blur, dim=lk.bg_dim))
        self._rect = lru_cache(maxsize=64)(self._compute_rect)
        self._layers = lru_cache(maxsize=4)(self._layer)

    def _compute_rect(self, pid: str) -> Rect:
        info = self.panels[pid]
        ar = info.w / max(info.h, 1)
        fill = min(max(self.look.fill, 0.5), 1.0)
        aw, ah = self.W * fill, self.H * fill
        dw = min(aw, ah * ar, info.w * self.look.max_upscale)
        if ar < 0.45:  # tall manhwa panel: wider column that pans
            dw = min(max(dw, self.W * 0.36), info.w * self.look.max_upscale)
            content_h = dw / ar
            vh = min(content_h, self.H * 0.94)
        else:
            content_h = vh = dw / ar
        return Rect((self.W - dw) / 2, (self.H - vh) / 2, dw, vh, content_h)

    def _layer(self, pid: str) -> np.ndarray:
        """Blurred background + shadow (+ the card itself unless it scrolls), camera at identity."""
        bg = self._bg(pid)
        oy, ox = (bg.shape[0] - self.H) // 2, (bg.shape[1] - self.W) // 2
        canvas = bg[oy : oy + self.H, ox : ox + self.W].copy()
        r = self._rect(pid)
        scrolling = r.content_h > r.h + 1
        self.draw_card(canvas, pid, r, r, 1.0, (1.0, 0.0, 0.0), 0.0, border=False, shadow=0.55, image=not scrolling)
        return canvas

    def _cam(self, shot: Shot, t: float) -> tuple[float, float, float]:
        e = ease_in_out_sine(clamp01(t / max(shot.dur, 1e-3)))
        if shot.emphasis == "hit" and self.look.hit_effect > 0:
            return camera("hit", t, shot.dur, self.look.camera, self.look.hit_effect)
        zoom = (0.06 if shot.emphasis == "reveal" else 0.045) * self.look.camera
        s = 1 + zoom * e if shot.index % 2 == 0 else 1 + zoom * (1 - e)
        return (s, 0.0, 0.0)

    def _render_gpu(self, gpu, shot: Shot, t: float):
        pid = shot.panels[0]
        cam = self._cam(shot, t)
        frame = gpu.warp(self._layers(pid), *cam)
        r = self._rect(pid)
        if r.content_h > r.h + 1:
            scroll = ease_in_out_sine(clamp01((t - 0.25) / max(shot.dur - 0.4, 0.4)))
            self.card_gpu(gpu, frame, pid, r, r, 1.0, cam, scroll, border=False)
        return frame

    def frame_gpu(self, gpu, shot: Shot, prev: Shot | None, t: float):
        img = self._render_gpu(gpu, shot, t)
        if prev is not None and t < self.XFADE and prev.panels != shot.panels:
            img = gpu.blend(img, self._render_gpu(gpu, prev, prev.dur), t / self.XFADE)
        return img

    def _render(self, shot: Shot, t: float) -> np.ndarray:
        pid = shot.panels[0]
        cam = self._cam(shot, t)
        # whole composition moves as one camera: a single warp per frame
        frame = warp_frame(self._layers(pid), *cam)
        r = self._rect(pid)
        if r.content_h > r.h + 1:  # tall panel scrolls inside its frame
            scroll = ease_in_out_sine(clamp01((t - 0.25) / max(shot.dur - 0.4, 0.4)))
            self.draw_card(frame, pid, r, r, 1.0, cam, scroll, border=False, shadow=0.0)
        return frame

    def frame(self, shot: Shot, prev: Shot | None, t: float) -> np.ndarray:
        img = self._render(shot, t)
        if prev is not None and t < self.XFADE and prev.panels != shot.panels:
            a = t / self.XFADE
            old = self._render(prev, prev.dur)
            img = cv2.addWeighted(img, a, old, 1 - a, 0)
        return img


def make_style(name: str, width: int, height: int, panels: dict[str, PanelInfo], look: dict | Look | None = None):
    lk = look if isinstance(look, Look) else Look.from_dict(look)
    if name == "motion":
        return MotionStyle(width, height, panels, lk)
    if name == "blurfill":
        return BlurFillStyle(width, height, panels, lk)
    raise ValueError(f"unknown style {name!r} (motion | blurfill)")


def build_shots(timed_beats: list[dict], style: str, min_shot: float = 1.1) -> list[Shot]:
    """timed_beats: [{start, end, panels, emphasis}] -> shots for the given style."""
    shots: list[Shot] = []
    for b in timed_beats:
        if style == "motion" or len(b["panels"]) == 1:
            shots.append(Shot(b["start"], b["end"], list(b["panels"]), b["emphasis"]))
            continue
        d = b["end"] - b["start"]
        pids = b["panels"][: max(1, int(d // min_shot))]
        step = d / len(pids)
        for i, pid in enumerate(pids):
            emph = b["emphasis"] if i == 0 else ("normal" if b["emphasis"] == "hit" else b["emphasis"])
            shots.append(Shot(b["start"] + i * step, b["start"] + (i + 1) * step, [pid], emph))
    for i, s in enumerate(shots):
        s.index = i
    return shots


def panel_infos(meta: dict[str, dict], work_dir: Path) -> dict[str, PanelInfo]:
    return {pid: PanelInfo(str(work_dir / m["file"]), m["w"], m["h"]) for pid, m in meta.items()}
