"""Synthetic manga / manhwa generator for tests and demos (original content, no copyrighted art).

Pages contain bordered panels separated by white gutters, simple figures, and speech bubbles
carrying an actual plot, so the reader model can extract a story and panel detection is testable.
"""

from __future__ import annotations

import random
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

STORY = [
    # chapter 1
    [
        [("Kai Ren, 29. Office clerk. Last train missed again.", "kai_desk"), ("Another night of overtime...", "kai_tired")],
        [("On the empty platform, a crack of blue light splits the air.", "portal"), ("What... is that?!", "kai_shock"),
         ("A sword floats out of the light.", "sword")],
        [("TAKE ME, it whispers. YOU HAVE BEEN CHOSEN.", "sword"), ("Kai grabs the hilt. His veins burn with blue fire.", "kai_power"),
         ("System: [Skill acquired - Blade of the Last Train]", "system")],
    ],
    # chapter 2
    [
        [("Next morning. Kai's boss, Director Haru, slams a pile of files down.", "boss"), ("Finish these by noon, Ren!", "boss")],
        [("But the office walls start to bleed shadow.", "portal"), ("A dungeon break - inside the office!", "kai_shock"),
         ("A goblin with a necktie lunges at Director Haru.", "monster")],
        [("Kai draws the blade. One slash.", "kai_power"), ("The goblin turns to ash. Haru stares, speechless.", "boss"),
         ("Who ARE you, Ren?", "boss")],
    ],
    # chapter 3
    [
        [("A woman in a white guild coat appears: Mira Sol, S-rank hunter.", "mira"), ("That sword belongs to the Abyss Guild.", "mira")],
        [("Hand it over, or the guild will hunt you.", "mira"), ("Kai grips the hilt tighter.", "kai_tired")],
        [("No. It chose me.", "kai_power"), ("Mira smiles. Then you'd better survive the Gate Exam.", "mira"),
         ("Behind her, a gate the size of a skyscraper opens.", "portal")],
    ],
]

COLORS = {"kai": (60, 90, 200), "boss": (170, 60, 40), "mira": (230, 230, 240), "sword": (80, 200, 255), "monster": (60, 150, 60)}


def _font(size: int):
    for name in ("arialbd.ttf", "Arial Bold.ttf", "DejaVuSans-Bold.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _figure(d: ImageDraw.ImageDraw, box, kind: str, color: bool, rng: random.Random):
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2 + (y1 - y0) // 8
    h = (y1 - y0) * 0.55
    ink = (20, 20, 20)
    who = kind.split("_")[0]
    fill = COLORS.get(who, (200, 200, 200)) if color else (255, 255, 255)
    if kind == "portal":
        for r in range(int(h * 0.6), 4, -14):
            d.ellipse([cx - r * 0.7, cy - r, cx + r * 0.7, cy + r], outline=(80, 160, 255) if color else ink, width=4)
        return
    if kind in ("sword", "system"):
        d.polygon([(cx, cy - h * 0.6), (cx + 14, cy + h * 0.2), (cx - 14, cy + h * 0.2)], fill=fill, outline=ink)
        d.rectangle([cx - 40, cy + h * 0.2, cx + 40, cy + h * 0.2 + 12], fill=ink)
        if kind == "system":
            d.rectangle([x0 + 30, y0 + 30, x1 - 30, y0 + 90], outline=(0, 120, 255) if color else ink, width=3)
        return
    if kind == "monster":
        d.ellipse([cx - h * 0.35, cy - h * 0.5, cx + h * 0.35, cy + h * 0.2], fill=fill, outline=ink, width=3)
        d.polygon([(cx - h * 0.3, cy - h * 0.45), (cx - h * 0.45, cy - h * 0.8), (cx - h * 0.15, cy - h * 0.5)], fill=fill, outline=ink)
        d.line([cx, cy - h * 0.1, cx, cy + h * 0.15], fill=(200, 0, 0) if color else ink, width=8)  # necktie
        return
    # person: head + body, with pose variation
    r = h * 0.18
    d.ellipse([cx - r, cy - h * 0.55 - r, cx + r, cy - h * 0.55 + r], fill=(250, 220, 190) if color else (255, 255, 255), outline=ink, width=3)
    d.rectangle([cx - r * 0.9, cy - h * 0.55 + r, cx + r * 0.9, cy + h * 0.15], fill=fill, outline=ink, width=3)
    arm = h * (0.45 if kind.endswith("power") else 0.25)
    d.line([cx + r * 0.9, cy - h * 0.3, cx + r * 0.9 + arm, cy - h * 0.3 - (arm if kind.endswith("power") else -10)], fill=ink, width=5)
    if kind.endswith("power"):
        d.line([cx + r * 0.9 + arm, cy - h * 0.3 - arm, cx + r * 0.9 + arm + 40, cy - h * 0.3 - arm - 120], fill=(80, 200, 255) if color else ink, width=10)
    if kind.endswith("shock"):
        for k in range(6):
            a = rng.uniform(-1, 1)
            d.line([cx + a * 200, y0 + 20, cx + a * 60, cy - h * 0.6], fill=ink, width=2)


def _bubble(d: ImageDraw.ImageDraw, box, text: str, size: int):
    x0, y0, x1, y1 = box
    font = _font(size)
    lines = textwrap.wrap(text.upper(), width=max(10, int((x1 - x0) / (size * 0.62))))[:5]
    th = len(lines) * (size + 6)
    bw = min(x1 - x0 - 30, max(d.textlength(line, font=font) for line in lines) + 40)
    bx0, by0 = x0 + 18, y0 + 18
    d.rounded_rectangle([bx0, by0, bx0 + bw, by0 + th + 24], radius=22, fill=(255, 255, 255), outline=(20, 20, 20), width=3)
    for i, line in enumerate(lines):
        d.text((bx0 + 20, by0 + 12 + i * (size + 6)), line, fill=(15, 15, 15), font=font)


def make_manga(dest: Path, chapters: int = 3, seed: int = 1) -> Path:
    """B&W pages (900x1300) with 2-3 panels each, white gutters."""
    rng = random.Random(seed)
    for ci, pages in enumerate(STORY[:chapters], start=1):
        folder = dest / f"Chapter {ci}"
        folder.mkdir(parents=True, exist_ok=True)
        for pi, panels in enumerate(pages, start=1):
            W, H, m, g = 900, 1300, 40, 28
            img = Image.new("RGB", (W, H), "white")
            d = ImageDraw.Draw(img)
            n = len(panels)
            # layout: first row full width, remaining split in a row (right-to-left reading order)
            if n == 2:
                boxes = [(m, m, W - m, H // 2 - g // 2), (m, H // 2 + g // 2, W - m, H - m)]
            else:
                top = (m, m, W - m, int(H * 0.45))
                mid_y = int(H * 0.45) + g
                right = (W // 2 + g // 2, mid_y, W - m, H - m)  # read first (RTL)
                left = (m, mid_y, W // 2 - g // 2, H - m)
                boxes = [top, right, left]
            for (text, kind), box in zip(panels, boxes):
                d.rectangle(box, outline=(10, 10, 10), width=5)
                # screentone-ish hatching for texture
                for y in range(box[1] + 8, box[3] - 8, 18):
                    d.line([box[0] + 8, y, box[0] + 60, y], fill=(170, 170, 170), width=1)
                _figure(d, box, kind, color=False, rng=rng)
                _bubble(d, box, text, size=26)
            img.save(folder / f"{pi:03d}.png")
    return dest


def make_manhwa(dest: Path, chapters: int = 3, seed: int = 2) -> Path:
    """Colour vertical strips (800 wide), panels separated by tall gutters, cut into chunks at arbitrary heights."""
    rng = random.Random(seed)
    for ci, pages in enumerate(STORY[:chapters], start=1):
        folder = dest / f"Episode {ci}"
        folder.mkdir(parents=True, exist_ok=True)
        panels = [p for page in pages for p in page]
        W = 800
        heights = [rng.choice([700, 900, 1100, 2600]) for _ in panels]
        gutter = 160
        H = sum(heights) + gutter * (len(panels) + 1)
        strip = Image.new("RGB", (W, H), (255, 255, 255))
        d = ImageDraw.Draw(strip)
        y = gutter
        for (text, kind), ph in zip(panels, heights):
            box = (0, y, W, y + ph)
            top = tuple(rng.randint(30, 90) for _ in range(3))
            for yy in range(box[1], box[3], 4):  # vertical gradient sky
                t = (yy - box[1]) / ph
                col = tuple(int(c + (220 - c) * t) for c in top)
                d.rectangle([0, yy, W, yy + 4], fill=col)
            _figure(d, box, kind, color=True, rng=rng)
            _bubble(d, box, text, size=28)
            y += ph + gutter
        # cut into ~1600px chunks regardless of panel boundaries (like real webtoon uploads)
        cut, k = 0, 1
        while cut < H:
            nxt = min(H, cut + 1600)
            strip.crop((0, cut, W, nxt)).save(folder / f"{k:03d}.jpg", quality=92)
            cut, k = nxt, k + 1
    return dest


if __name__ == "__main__":
    import sys

    root = Path(sys.argv[1] if len(sys.argv) > 1 else "samples")
    make_manga(root / "manga")
    make_manhwa(root / "manhwa")
    print(f"wrote samples under {root.resolve()}")
