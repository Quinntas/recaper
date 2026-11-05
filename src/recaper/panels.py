"""Panel extraction.

* manhwa: chapter images are stitched into one long strip and cut at the gutters
  (runs of near-uniform rows). Over-tall cuts are split; tiny fragments are merged.
* manga: each page is segmented by flood-filling the page background from the border;
  remaining blobs are panels. Reading order uses a recursive XY-cut (right-to-left
  for Japanese manga by default).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import numpy as np

from .config import PanelsConfig
from .ingest import Chapter


@dataclass
class Panel:
    id: str  # globally unique, e.g. c0003-p017
    chapter: str  # chapter cid
    file: str  # path relative to the work dir
    w: int
    h: int
    page: int | None = None


# --------------------------------------------------------------------------- io


def imread(path: Path) -> np.ndarray:
    data = np.fromfile(str(path), dtype=np.uint8)  # unicode-safe on Windows
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        from PIL import Image  # webp/avif/gif fallbacks

        with Image.open(path) as im:
            img = cv2.cvtColor(np.asarray(im.convert("RGB")), cv2.COLOR_RGB2BGR)
    return img


def imwrite(path: Path, img: np.ndarray, quality: int = 93) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError(f"could not encode {path}")
    buf.tofile(str(path))


# ---------------------------------------------------------------- manhwa strips


def _blank_rows(gray: np.ndarray, tol: float) -> np.ndarray:
    w = gray.shape[1]
    core = gray[:, int(w * 0.03) : max(int(w * 0.97), int(w * 0.03) + 1)]
    return core.std(axis=1) < tol


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """[start, end) runs of True."""
    padded = np.concatenate([[False], mask, [False]])
    diff = np.diff(padded.astype(np.int8))
    starts = np.flatnonzero(diff == 1)
    ends = np.flatnonzero(diff == -1)
    return list(zip(starts.tolist(), ends.tolist()))


def _ink_fraction(img: np.ndarray) -> float:
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    border = np.concatenate([gray[0], gray[-1], gray[:, 0], gray[:, -1]])
    bg = np.median(border)
    return float((np.abs(gray.astype(np.int16) - bg) > 28).mean())


def _trim(img: np.ndarray, tol: float = 5.0, pad: int = 6) -> np.ndarray:
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    rows = np.flatnonzero(gray.std(axis=1) >= tol)
    cols = np.flatnonzero(gray.std(axis=0) >= tol)
    if rows.size == 0 or cols.size == 0:
        return img
    y0, y1 = max(rows[0] - pad, 0), min(rows[-1] + pad + 1, img.shape[0])
    x0, x1 = max(cols[0] - pad, 0), min(cols[-1] + pad + 1, img.shape[1])
    return img[y0:y1, x0:x1]


def slice_manhwa(pages: list[Path], gutter: float = 0.025, min_height: float = 0.12, max_ratio: float = 4.5) -> list[np.ndarray]:
    imgs = [imread(p) for p in pages]
    imgs = [i for i in imgs if i is not None and i.size]
    if not imgs:
        return []
    width = int(np.median([i.shape[1] for i in imgs]))
    imgs = [
        i if i.shape[1] == width else cv2.resize(i, (width, round(i.shape[0] * width / i.shape[1])), interpolation=cv2.INTER_AREA)
        for i in imgs
    ]
    strip = np.vstack(imgs)
    gray = cv2.cvtColor(strip, cv2.COLOR_BGR2GRAY).astype(np.float32)

    blank = _blank_rows(gray, tol=6.0)
    min_gap = max(8, int(width * gutter))
    row_mean = strip.reshape(strip.shape[0], -1, 3).mean(axis=1)  # per-row colour
    # A gutter is a stretch of uniform rows that also keeps ONE colour. Blank runs can include a
    # panel's flat edge or a gradient sky, so split each run into constant-colour sub-runs.
    candidates = []
    for a, b in _runs(blank):
        if b - a < min_gap:
            continue
        s = a
        for i in range(a + 1, b + 1):
            if i == b or np.abs(row_mean[i] - row_mean[s]).max() > 3.0:
                if i - s >= min_gap:
                    candidates.append((s, i, row_mean[s:i].mean(axis=0)))
                s = i
    # Gutters use the "paper" colour: white, black, or the chapter's most common gutter colour.
    # Flat bands of any other colour are empty space inside a panel unless they're very long.
    if candidates:
        keys = [tuple((c // 16).astype(int)) for _, _, c in candidates]
        weights: dict[tuple, int] = {}
        for k, (a, b, _) in zip(keys, candidates):
            weights[k] = weights.get(k, 0) + (b - a)
        paper = np.array(max(weights, key=weights.get)) * 16 + 8
    gaps = []
    for a, b, color in candidates:
        is_paper = color.min() > 232 or color.max() < 24 or np.abs(color - paper).max() < 16
        if is_paper or b - a >= width * 0.5:
            gaps.append((a, b))

    # content segments between gaps
    segs: list[list[int]] = []
    cursor = 0
    for a, b in gaps:
        if a > cursor:
            segs.append([cursor, a])
        cursor = b
    if cursor < strip.shape[0]:
        segs.append([cursor, strip.shape[0]])

    # merge fragments that are too small to stand alone into their nearest neighbour
    min_h = int(width * min_height)
    merged: list[list[int]] = []
    for seg in segs:
        if merged and (seg[1] - seg[0] < min_h or merged[-1][1] - merged[-1][0] < min_h) and seg[0] - merged[-1][1] < width * 0.25:
            merged[-1][1] = seg[1]
        else:
            merged.append(seg)

    # split over-tall segments at their quietest row
    max_h = int(width * max_ratio)
    final: list[tuple[int, int]] = []
    stack = [tuple(s) for s in reversed(merged)]
    row_std = gray.std(axis=1)
    while stack:
        a, b = stack.pop()
        if b - a <= max_h:
            final.append((a, b))
            continue
        lo, hi = a + int((b - a) * 0.35), a + int((b - a) * 0.65)
        cut = lo + int(np.argmin(row_std[lo:hi]))
        stack.extend([(cut, b), (a, cut)])

    out = []
    for a, b in final:
        crop = _trim(strip[a:b])
        if crop.shape[0] < 24 or crop.shape[1] < 24 or _ink_fraction(crop) < 0.01:
            continue
        out.append(crop)
    return out


# ------------------------------------------------------------------ manga pages


def _xy_cut(boxes: list[tuple[int, int, int, int]], rtl: bool, tol: int) -> list[tuple[int, int, int, int]]:
    if len(boxes) <= 1:
        return boxes

    def split(axis: int) -> list[list] | None:
        # axis 1 = horizontal cut (by y), axis 0 = vertical cut (by x)
        start, size = (1, 3) if axis == 1 else (0, 2)
        order = sorted(boxes, key=lambda b: b[start])
        groups, current = [], [order[0]]
        reach = order[0][start] + order[0][size]
        for b in order[1:]:
            if b[start] >= reach - tol:
                groups.append(current)
                current = [b]
            else:
                current.append(b)
            reach = max(reach, b[start] + b[size])
        groups.append(current)
        return groups if len(groups) > 1 else None

    rows = split(1)
    if rows:
        return [b for g in rows for b in _xy_cut(g, rtl, tol)]
    cols = split(0)
    if cols:
        if rtl:
            cols.reverse()
        return [b for g in cols for b in _xy_cut(g, rtl, tol)]
    return sorted(boxes, key=lambda b: (b[1], -b[0] if rtl else b[0]))


def detect_manga_panels(page: np.ndarray, rtl: bool = True, white_level: int = 215) -> list[np.ndarray]:
    h, w = page.shape[:2]
    gray = cv2.cvtColor(page, cv2.COLOR_BGR2GRAY)
    border = np.concatenate([gray[0], gray[-1], gray[:, 0], gray[:, -1]])
    if np.median(border) < 80:  # black gutters
        gray = 255 - gray
    white = (gray > white_level).astype(np.uint8)

    n, labels = cv2.connectedComponents(white, connectivity=4)
    edge_labels = set(np.unique(np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]]))) - {0}
    background = np.isin(labels, list(edge_labels))
    content = (~background).astype(np.uint8)
    content = cv2.morphologyEx(content, cv2.MORPH_OPEN, np.ones((7, 7), np.uint8))

    n2, _, stats, _ = cv2.connectedComponentsWithStats(content, connectivity=8)
    page_area = h * w
    boxes = []
    for i in range(1, n2):
        x, y, bw, bh, _area = stats[i]
        if bw * bh < page_area * 0.025 or bw < w * 0.08 or bh < h * 0.05:
            continue
        boxes.append((int(x), int(y), int(bw), int(bh)))

    # drop boxes nested inside others
    def inside(a, b):
        ax, ay, aw, ah = a
        bx, by, bw_, bh_ = b
        ix = max(0, min(ax + aw, bx + bw_) - max(ax, bx))
        iy = max(0, min(ay + ah, by + bh_) - max(ay, by))
        return ix * iy > 0.85 * aw * ah

    boxes = [b for b in boxes if not any(o is not b and inside(b, o) and o[2] * o[3] > b[2] * b[3] for o in boxes)]

    if not boxes or (len(boxes) == 1 and boxes[0][2] * boxes[0][3] > page_area * 0.8):
        return [_trim(page)]

    ordered = _xy_cut(boxes, rtl=rtl, tol=int(min(h, w) * 0.02))
    pad = 4
    return [page[max(y - pad, 0) : min(y + bh + pad, h), max(x - pad, 0) : min(x + bw + pad, w)] for x, y, bw, bh in ordered]


# ---------------------------------------------------------------------- driver


def _signature(source: str, rtl: bool, params) -> str:
    return json.dumps([source, rtl, asdict(params)], sort_keys=True)


def panels_stale(chapter: Chapter, source: str, rtl: bool, work_dir: Path, params) -> bool:
    """True if cached panels exist but were cut with different settings (they will be re-cut)."""
    out_dir = work_dir / "panels" / chapter.cid
    if not (out_dir / "panels.json").exists():
        return False
    sig_file = out_dir / "params.json"
    old = sig_file.read_text("utf-8") if sig_file.exists() else _signature(source, rtl, PanelsConfig())
    return old != _signature(source, rtl, params)


def extract_chapter(chapter: Chapter, source: str, rtl: bool, work_dir: Path, params=None) -> list[Panel]:
    """Cut a chapter into panels (cached). `params` = PanelsConfig; changing it re-slices the chapter."""
    params = params or PanelsConfig()
    signature = _signature(source, rtl, params)
    out_dir = work_dir / "panels" / chapter.cid
    index_file = out_dir / "panels.json"
    sig_file = out_dir / "params.json"
    if index_file.exists():
        old_sig = sig_file.read_text("utf-8") if sig_file.exists() else _signature(source, rtl, PanelsConfig())
        if old_sig == signature:
            return [Panel(**p) for p in json.loads(index_file.read_text("utf-8"))]
        for old in out_dir.glob("p*.jpg"):  # slicing settings changed: redo this chapter
            old.unlink()

    crops: list[tuple[np.ndarray, int | None]] = []
    if source == "manhwa":
        crops = [(c, None) for c in slice_manhwa(chapter.pages, params.gutter, params.min_height, params.max_ratio)]
    else:
        for pi, page_path in enumerate(chapter.pages):
            page = imread(page_path)
            if page is None:
                continue
            if page.shape[1] > page.shape[0] * 1.15:  # two-page spread: keep whole, it reads as one beat
                crops.append((_trim(page), pi))
                continue
            crops.extend((c, pi) for c in detect_manga_panels(page, rtl=rtl, white_level=params.white))

    panels: list[Panel] = []
    for i, (img, page_idx) in enumerate(crops, start=1):
        long_edge = max(img.shape[:2])
        if long_edge > 3200 and source != "manhwa":
            s = 3200 / long_edge
            img = cv2.resize(img, (round(img.shape[1] * s), round(img.shape[0] * s)), interpolation=cv2.INTER_AREA)
        rel = Path("panels") / chapter.cid / f"p{i:04d}.jpg"
        imwrite(work_dir / rel, img)
        panels.append(Panel(f"{chapter.cid}-p{i:04d}", chapter.cid, rel.as_posix(), img.shape[1], img.shape[0], page_idx))

    out_dir.mkdir(parents=True, exist_ok=True)
    index_file.write_text(json.dumps([asdict(p) for p in panels], indent=1), "utf-8")
    sig_file.write_text(signature, "utf-8")
    return panels


def contact_sheet(panels: list[Panel], work_dir: Path, dest: Path, thumb_h: int = 260, per_row: int = 8) -> None:
    """Debug view: every panel of a chapter in reading order with its label."""
    thumbs = []
    for p in panels:
        img = imread(work_dir / p.file)
        s = thumb_h / img.shape[0]
        t = cv2.resize(img, (max(1, round(img.shape[1] * s)), thumb_h), interpolation=cv2.INTER_AREA)
        t = t[:, : thumb_h * 2] if t.shape[1] > thumb_h * 2 else t
        canvas = np.full((thumb_h + 28, thumb_h * 2 if t.shape[1] > thumb_h else thumb_h, 3), 245, np.uint8)
        canvas[28 : 28 + t.shape[0], : t.shape[1]] = t[:, : canvas.shape[1]]
        cv2.putText(canvas, p.id.split("-")[-1], (4, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (20, 20, 200), 2)
        thumbs.append(canvas)
    if not thumbs:
        return
    rows, row, row_w = [], [], 0
    max_w = per_row * thumb_h
    for t in thumbs:
        if row and row_w + t.shape[1] > max_w:
            rows.append(row)
            row, row_w = [], 0
        row.append(t)
        row_w += t.shape[1] + 6
    rows.append(row)
    lines = []
    for r in rows:
        line = np.full((thumb_h + 28, max_w + 6 * per_row, 3), 255, np.uint8)
        x = 0
        for t in r:
            line[:, x : x + t.shape[1]] = t
            x += t.shape[1] + 6
        lines.append(line)
    imwrite(dest, np.vstack(lines), quality=85)
