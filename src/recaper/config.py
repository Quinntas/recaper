"""Run configuration: defaults < global settings (~/.recaper.toml) < recaper.toml in the cwd
< recaper.toml in the input folder < CLI flags."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

CONFIG_NAME = "recaper.toml"
WORK_DIR_NAME = ".recaper"


def global_config_path() -> Path:
    """General settings shared by every project (UI: Settings page). RECAPER_SETTINGS overrides the location."""
    env = os.environ.get("RECAPER_SETTINGS")
    return Path(env).expanduser() if env else Path.home() / ".recaper.toml"


def base_config_files(input_dir: Path | None) -> list[Path]:
    """Config files applied before the project's own recaper.toml, lowest priority first."""
    out: list[Path] = []
    own = (input_dir / CONFIG_NAME).resolve() if input_dir else None
    for candidate in (global_config_path(), Path.cwd() / CONFIG_NAME):
        if candidate.is_file() and candidate.resolve() != own and candidate.resolve() not in [o.resolve() for o in out]:
            out.append(candidate)
    return out

# Shortcuts accepted anywhere a provider spec is expected.
PROVIDER_ALIASES = {
    "deepseek-free": "opencode:opencode/deepseek-v4.1-flash-free",
    "claude": "claude:sonnet",
    "claude-opus": "claude:opus",
    "gemini": "gemini",
}


@dataclass
class TTSConfig:
    engine: str = "kokoro"
    voice: str | None = None  # engine-specific; None = per-language default
    speed: float = 1.0  # Kokoro at 1.0 ~ 170 wpm, an easy listening pace (1.2 ~ 200 wpm)
    args: dict[str, Any] = field(default_factory=dict)  # extra engine constructor kwargs
    beat_gap: float = 0.25  # silence between beats (s)
    chapter_gap: float = 1.2  # extra silence at chapter boundaries (s)
    workers: int = 0  # parallel TTS processes; 0 = auto (3 for local GPU engines, 1 for APIs)


@dataclass
class RenderConfig:
    style: str = "auto"  # auto | motion | blurfill
    width: int = 1920
    height: int = 1080
    fps: int = 30
    encoder: str = "auto"  # auto | nvenc | x264
    quality: int = 21  # CQ / CRF
    workers: int = 0  # 0 = auto
    loudness: float = -16.0  # integrated LUFS target for the final mix
    # --- look & motion (all styles) ---
    background: str = "paper"  # motion style: paper | black | white | dark | blur | #rrggbb
    layout: str = "clean"  # motion style, beats with several panels: clean (max 2, no overlap) | cascade | row
    reveal: str = "progressive"  # motion style: progressive (panels appear as narration reaches them) | together
    card_border: bool = True  # thin border around panel cards (motion style)
    shadow: float = 1.0  # drop-shadow strength multiplier (0 = none)
    camera: float = 1.0  # Ken Burns / slow push intensity multiplier (0 = static)
    hit_effect: float = 1.0  # punch-in + shake on "hit" beats multiplier (0 = off)
    transition_speed: float = 1.0  # card slide / cross-dissolve speed multiplier (>1 = snappier)
    fill: float = 0.92  # share of the frame panels may occupy
    max_upscale: float = 2.4  # max panel enlargement (higher = bigger but softer low-res art)
    upscaler: str = "auto"  # auto | off | fast | quality - Real-ESRGAN for panels shown enlarged (GPU)
    bg_blur: float = 6.0  # blurfill / blur background: blur strength
    bg_dim: float = 0.5  # blurfill / blur background: brightness (0-1)
    chunk_seconds: float = 40.0  # render work-unit length (parallelism / cache granularity); 40 s measured fastest


@dataclass
class BookConfig:
    split_words: int = 6000  # chapters longer than this (no structure found) are split into parts
    min_words: int = 150  # shorter sections (title page, copyright...) are dropped; their images kept
    min_illustration: int = 300  # embedded images smaller than this (px, short side) are ignored


@dataclass
class ImagesConfig:
    generator: str = "none"  # none | gemini[:model] | openai[:model] | antigravity[:model] - fills beats no real image fits
    style: str = "official light-novel illustration style, clean anime line art, soft colours, cinematic composition"
    max_generated: int = 40  # cap per video (cost control); beyond it the nearest real image is reused
    refs: int = 3  # reference images (official art) sent with each generation for style/character consistency
    related_per_chapter: int = 80  # max related-work panels offered to the writer per chapter


@dataclass
class PanelsConfig:
    gutter: float = 0.025  # manhwa: min gutter height between panels, as a fraction of strip width
    min_height: float = 0.12  # manhwa: fragments shorter than this (fraction of width) merge with neighbours
    max_ratio: float = 4.5  # manhwa: panels taller than width x this are split
    white: int = 215  # manga: brightness above which a pixel counts as page background/gutter


@dataclass
class Config:
    lang: str = "en"
    source: str = "auto"  # auto | manga | manhwa | book (light novels / prose: EPUB, text PDF, TXT)
    reading: str = "auto"  # auto | rtl | ltr (manga panel order)
    reader: str = "claude:sonnet"  # vision model(s) that read panels; comma = fallback chain
    writer: str = "claude:sonnet"  # model(s) that write the narration script
    polisher: str = ""  # model(s) for the polish pass; empty = the writer (a stronger editor helps local writers)
    minutes_per_chapter: float = 2.5
    target_minutes: float | None = None  # overrides minutes_per_chapter when set
    words_per_minute: int = 200  # script words per target minute (the Kokoro pace at tts.speed 1.2)
    chapters: str | None = None  # e.g. "1-20,25"
    title: str | None = None
    read_batch: int = int(os.environ.get("RECAPER_READ_BATCH", "36"))  # max panel images per reader call
    jobs: int = 6  # parallel model calls (chapters read / written concurrently)
    extra_instructions: str = ""  # appended to the writer prompt (tone, nicknames, etc.)
    hook: bool = True  # open with a cold-open hook (the short intro summary)
    known_story: str = "auto"  # viewers already know the story: auto (when the chapters start mid-series) | yes | no
    hook_words: int = 110  # hook length target (hard cap +20%)
    outro: str = ""  # optional closing line voiced at the very end (e.g. a call to action)
    polish: bool = True  # one editing pass per chapter: removes repeated tics, varies openings, checks names
    episode_minutes: float = 0  # split long recaps into parts of about this length (0 = one video)
    previously: bool = True  # parts 2+ open with a short 'previously on' recap
    image_sources: str = ""  # comma-separated library projects (e.g. the manga adaptation) whose panels illustrate this one
    panels: PanelsConfig = field(default_factory=PanelsConfig)
    book: BookConfig = field(default_factory=BookConfig)
    images: ImagesConfig = field(default_factory=ImagesConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    render: RenderConfig = field(default_factory=RenderConfig)

    # ---- loading -------------------------------------------------------
    @property
    def spoken_wpm(self) -> float:
        """Narration words per minute at the chosen voice speed (words_per_minute is the pace at speed 1.2)."""
        return self.words_per_minute * max(self.tts.speed, 0.1) / 1.2

    @classmethod
    def load(cls, input_dir: Path, overrides: dict[str, Any] | None = None) -> "Config":
        cfg = cls()
        for candidate in [*base_config_files(input_dir), input_dir / CONFIG_NAME]:
            if candidate.is_file():
                with candidate.open("rb") as fh:
                    try:
                        _apply(cfg, tomllib.load(fh))
                    except (ValueError, tomllib.TOMLDecodeError) as e:
                        raise ValueError(f"{candidate}: {e}") from e
        if overrides:
            _apply(cfg, {k: v for k, v in overrides.items() if v is not None})
        cfg.reader = _expand_aliases(cfg.reader)
        cfg.writer = _expand_aliases(cfg.writer)
        cfg.polisher = _expand_aliases(cfg.polisher) if cfg.polisher.strip() else ""
        return cfg


def _expand_aliases(spec: str) -> str:
    return ",".join(PROVIDER_ALIASES.get(s.strip(), s.strip()) for s in spec.split(",") if s.strip())


def _apply(obj: Any, data: dict[str, Any]) -> None:
    names = {f.name for f in fields(obj)}
    for key, value in data.items():
        key = key.replace("-", "_")
        if "." in key:  # dotted CLI override, e.g. "tts.voice"
            head, rest = key.split(".", 1)
            _apply(getattr(obj, head), {rest: value})
            continue
        if key not in names:
            raise ValueError(f"Unknown config key: {key!r}")
        current = getattr(obj, key)
        if hasattr(current, "__dataclass_fields__") and isinstance(value, dict):
            _apply(current, value)
        else:
            setattr(obj, key, value)


def parse_chapter_ranges(spec: str | None, count: int) -> list[int]:
    """'1-3,7' -> [0,1,2,6] (1-based input, 0-based output)."""
    if not spec:
        return list(range(count))
    picked: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            lo = int(a) if a else 1
            hi = int(b) if b else count
            picked.update(range(lo - 1, min(hi, count)))
        else:
            picked.add(int(part) - 1)
    return sorted(i for i in picked if 0 <= i < count)
