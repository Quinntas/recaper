"""Options schema for the UI, generated from the Config dataclasses so every option is exposed,
plus reading/writing a project's recaper.toml (only non-default values are written)."""

from __future__ import annotations

import json
import sys
from dataclasses import MISSING, fields, is_dataclass
from pathlib import Path
from typing import Any

from ..config import CONFIG_NAME, Config, RenderConfig, TTSConfig, base_config_files, global_config_path
from ..tts import DEFAULT_VOICES, ENGINE_CLASSES

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

PROVIDERS = [
    "claude:sonnet",
    "claude:opus",
    "claude:haiku",
    "gemini",
    "gemini-api:gemini-flash-latest",
    "gemini-cli",
    "openai:gpt-5-mini",
    "ollama:qwen2.5vl:7b",
    "deepseek-free",
    "deepseek-free,claude:sonnet",
    "claude:sonnet,gemini",
]
KOKORO_VOICES = [
    "am_michael", "am_fenrir", "am_puck", "am_adam", "am_echo", "am_eric", "am_liam", "am_onyx",
    "af_heart", "af_bella", "af_nicole", "af_sarah", "af_sky", "bm_george", "bm_fable", "bm_lewis",
    "bf_emma", "bf_isabella", "pm_alex", "pm_santa", "pf_dora", "em_alex", "ef_dora", "ff_siwis",
    "im_nicola", "if_sara", "hm_omega", "jm_kumo", "zm_yunjian",
]  # fmt: skip
LANGS = ["en", "pt", "es", "fr", "it", "de", "ja", "hi", "zh"]

GROUPS = [
    ("story", "Story & length"),
    ("models", "AI models"),
    ("voice", "Voice"),
    ("video", "Video"),
    ("look", "Look & motion"),
    ("panels", "Panel slicing"),
    ("book", "Books & images"),
    ("advanced", "Advanced"),
]
BACKGROUNDS = ["paper", "black", "white", "dark", "blur"]

# key -> (group, label, help, kind, choices, suggestions)
META: dict[str, tuple] = {
    "title": ("story", "Video title", "Used for the file name and embedded metadata. Default: folder name.", "text", None, None),
    "lang": ("story", "Narration language", "Script and voice language.", "select", LANGS, None),
    "minutes_per_chapter": ("story", "Minutes per chapter", "Narration length per chapter (top refs ≈ 2-3).", "number", None, None),
    "target_minutes": ("story", "Total length target (min)", "Overrides minutes per chapter when set; length is spread by chapter weight.", "number", None, None),
    "chapters": ("story", "Chapter selection", "e.g. 1-20 or 1-5,8. Empty = all.", "text", None, None),
    "extra_instructions": ("story", "Extra directions for the writer", "Tone, nicknames, things to emphasise or skip.", "textarea", None, None),
    "hook": ("story", "Cold-open hook", "Start the video with a 20-35 s hook built from the most striking moments.", "bool", None, None),
    "hook_words": ("story", "Hook length (words)", "Target length of the hook (hard cap +20%).", "int", None, None),
    "outro": ("story", "Closing line", "Optional last line, voiced over the final panel (e.g. a call to action).", "text", None, None),
    "source": ("story", "Source type", "auto detects light novels / books (EPUB, text PDF, TXT), long colour strips (manhwa) and pages (manga).", "select", ["auto", "manga", "manhwa", "book"], None),
    "reading": ("story", "Manga reading order", "Panel order on manga pages. auto = right-to-left for manga.", "select", ["auto", "rtl", "ltr"], None),
    "reader": ("models", "Reader (vision)", "Model(s) that look at the panels. Comma = fallback chain. Must support images.", "provider", None, PROVIDERS),
    "writer": ("models", "Writer", "Model(s) that write the narration. Text-only models (deepseek-free) are fine here.", "provider", None, PROVIDERS),
    "jobs": ("models", "Parallel model calls", "Chapters read/written at the same time.", "int", None, None),
    "read_batch": ("models", "Panels per reader call", "Max images per vision request.", "int", None, None),
    "words_per_minute": ("models", "Narration pace (wpm)", "Used to turn minutes into word targets. ~200 at voice speed 1.2.", "int", None, None),
    "tts.engine": ("voice", "TTS engine", "RealtimeTTS engine. Local GPU: kokoro, coqui. Cloud: edge, openai, elevenlabs, azure...", "select", sorted(ENGINE_CLASSES), None),
    "tts.voice": ("voice", "Voice", "Engine-specific voice name. Empty = default for the language.", "text", None, KOKORO_VOICES + sorted({v for d in DEFAULT_VOICES.values() for v in d.values()})),
    "tts.speed": ("voice", "Speed", "1.2 ≈ 200 wpm (top recap channels are 200-240).", "number", None, None),
    "tts.beat_gap": ("voice", "Pause between beats (s)", "", "number", None, None),
    "tts.chapter_gap": ("voice", "Pause between chapters (s)", "", "number", None, None),
    "tts.workers": ("voice", "Parallel voice workers", "0 = auto (3 for local GPU engines, 1 for cloud APIs).", "int", None, None),
    "tts.args": ("voice", "Engine arguments (JSON)", 'Extra constructor kwargs, e.g. {"api_key": "..."} or {"model": "tts-1-hd"}.', "json", None, None),
    "render.style": ("video", "Visual style", "motion = panel cards on paper, revealed with the narration (manga default). blurfill = panel over blurred copy (manhwa default).", "select", ["auto", "motion", "blurfill"], None),
    "render.width": ("video", "Width", "", "int", None, None),
    "render.height": ("video", "Height", "", "int", None, None),
    "render.fps": ("video", "FPS", "", "int", None, None),
    "render.quality": ("video", "Quality (CQ/CRF)", "Lower = better & bigger. 18-23 is typical.", "int", None, None),
    "render.loudness": ("video", "Loudness (LUFS)", "Integrated loudness of the final mix. YouTube normalises to -14.", "number", None, None),
    "render.background": ("look", "Background (motion style)", "paper, black, white, dark, blur (blurred current panel) or any #rrggbb colour.", "color", BACKGROUNDS, None),
    "render.layout": ("look", "Multi-panel layout (motion)", "cascade = each panel big, overlapping, newest on top. row = side by side.", "select", ["cascade", "row"], None),
    "render.reveal": ("look", "Panel reveal (motion)", "progressive = panels appear as the narration reaches them. together = all at the start of the beat.", "select", ["progressive", "together"], None),
    "render.card_border": ("look", "Card border (motion)", "Thin outline around each panel card.", "bool", None, None),
    "render.shadow": ("look", "Shadow strength", "Drop-shadow multiplier. 0 = none.", "number", None, None),
    "render.camera": ("look", "Camera motion", "Ken Burns / slow push intensity. 0 = static, 2 = double.", "number", None, None),
    "render.hit_effect": ("look", "Hit effect", "Punch-in + shake on impact beats. 0 = off.", "number", None, None),
    "render.transition_speed": ("look", "Animation speed", "Card slides and cross-dissolves. >1 = snappier, <1 = slower.", "number", None, None),
    "render.fill": ("look", "Frame fill", "Share of the frame panels may use (0.5-1.0).", "number", None, None),
    "render.max_upscale": ("look", "Max upscale", "How much small panels may be enlarged. Higher = bigger but softer.", "number", None, None),
    "render.bg_blur": ("look", "Background blur", "Blur strength for blurfill and the 'blur' background.", "number", None, None),
    "render.bg_dim": ("look", "Background brightness", "0 = black, 1 = full brightness (blurred backgrounds).", "number", None, None),
    "panels.gutter": ("panels", "Manhwa gutter size", "Min blank gap between panels, as a fraction of strip width. Lower = more, smaller cuts. Changing re-cuts and re-reads the chapter.", "number", None, None),
    "panels.min_height": ("panels", "Manhwa min panel height", "Fragments shorter than this (fraction of width) merge into neighbours.", "number", None, None),
    "panels.max_ratio": ("panels", "Manhwa max panel ratio", "Panels taller than width x this are split.", "number", None, None),
    "panels.white": ("panels", "Manga background level", "Brightness (0-255) above which a pixel counts as page background. Lower if gutters are grey.", "int", None, None),
    "image_sources": ("book", "Related works (image sources)", "Library projects - e.g. the manga adaptation - whose panels illustrate this one. Comma-separated. Use the Images tab to find and link them.", "projects", None, None),
    "images.generator": ("book", "Image generator", "Fills beats no real image fits, styled on the official art. gemini needs GEMINI_API_KEY, openai needs OPENAI_API_KEY. none = reuse the nearest real image.", "provider", None, ["none", "gemini", "gemini:gemini-2.5-flash-image", "openai", "openai:gpt-image-1"]),
    "images.style": ("book", "Generated image style", "Style words added to every generation prompt.", "textarea", None, None),
    "images.max_generated": ("book", "Max generated images", "Per video (cost control). Beyond it the nearest real image is reused.", "int", None, None),
    "images.refs": ("book", "Reference images per generation", "Official art sent with each request so characters and style match.", "int", None, None),
    "images.related_per_chapter": ("book", "Related panels per chapter", "Max adaptation panels offered to the writer per book chapter.", "int", None, None),
    "book.split_words": ("book", "Book: split long chapters (words)", "Chapters longer than this are split when the book has no usable structure.", "int", None, None),
    "book.min_words": ("book", "Book: skip sections under (words)", "Title pages, copyright, contents... are dropped (their images are kept).", "int", None, None),
    "book.min_illustration": ("book", "Book: min illustration size (px)", "Embedded images smaller than this are ignored (icons, drop caps).", "int", None, None),
    "render.chunk_seconds": ("advanced", "Render chunk length (s)", "Work-unit size for parallel rendering and the render cache.", "number", None, None),
    "render.encoder": ("advanced", "Encoder", "auto uses NVENC when available.", "select", ["auto", "nvenc", "x264"], None),
    "render.workers": ("advanced", "Render workers", "0 = auto.", "int", None, None),
}


def _default(f) -> Any:
    if f.default is not MISSING:
        return f.default
    if f.default_factory is not MISSING:  # type: ignore[misc]
        return f.default_factory()  # type: ignore[misc]
    return None


def _flat_fields(cls=Config, prefix: str = ""):
    for f in fields(cls):
        default = _default(f)
        if is_dataclass(default):
            yield from _flat_fields(type(default), prefix + f.name + ".")
        else:
            yield prefix + f.name, default


def schema() -> dict:
    opts = []
    for key, default in _flat_fields():
        group, label, help_, kind, choices, suggestions = META.get(key, ("advanced", key, "", "text", None, None))
        opts.append(
            {"key": key, "group": group, "label": label, "help": help_, "kind": kind, "default": default,
             "choices": choices, "suggestions": suggestions}
        )  # fmt: skip
    missing = [k for k, _ in _flat_fields() if k not in META]
    return {"groups": [{"id": g, "label": label} for g, label in GROUPS], "options": opts, "unlabelled": missing}


# ---------------------------------------------------------------- toml io


# Per-series settings: not offered on the global Settings page.
PROJECT_ONLY = {"title", "chapters", "image_sources"}


def read_values(project_dir: Path) -> dict[str, Any]:
    """Flat dotted values set in the project's recaper.toml."""
    return read_file(project_dir / CONFIG_NAME)


def read_global() -> dict[str, Any]:
    return read_file(global_config_path())


def inherited(project_dir: Path | None) -> dict[str, Any]:
    """What a project gets when its own recaper.toml leaves a key out: defaults < global < cwd file."""
    out = dict(_flat_fields())
    for f in base_config_files(project_dir):
        out.update(read_file(f))
    return out


def inherited_from(project_dir: Path | None) -> dict[str, str]:
    """key -> 'global' / 'cwd' for keys set by a base file (the rest come from the defaults)."""
    out: dict[str, str] = {}
    g = global_config_path()
    for f in base_config_files(project_dir):
        for k in read_file(f):
            out[k] = "global" if f == g else "cwd"
    return out


def read_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    out: dict[str, Any] = {}
    for k, v in data.items():
        if k in ("tts", "render", "panels", "book", "images") and isinstance(v, dict):  # the only tables; tts.args stays a dict value
            out.update({f"{k}.{sk}": sv for sk, sv in v.items()})
        else:
            out[k] = v
    return out


def coerce(key: str, value: Any) -> Any:
    meta = META.get(key)
    kind = meta[3] if meta else "text"
    if value is None or (isinstance(value, str) and value == ""):
        return None
    if kind == "int":
        return int(value)
    if kind == "number":
        return float(value)
    if kind == "json":
        return value if isinstance(value, dict) else json.loads(value)
    if kind == "bool":
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    return str(value)


def _clean(values: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    """Coerce; drop empty values and values equal to what would be inherited anyway."""
    defaults = dict(_flat_fields())
    clean: dict[str, Any] = {}
    for key, raw in values.items():
        if key not in defaults:
            raise ValueError(f"unknown option {key}")
        v = coerce(key, raw)
        if v is None or v == base.get(key, defaults[key]) or v == {}:
            continue
        clean[key] = v
    return clean


def write_values(project_dir: Path, values: dict[str, Any]) -> dict[str, Any]:
    """Validate + write the project's recaper.toml. Keys equal to the inherited value (global settings,
    else the default) are dropped, so changing a global setting later still reaches this project."""
    base = inherited(project_dir)
    clean = _clean(values, base)
    _check(base | clean)
    (project_dir / CONFIG_NAME).write_text(dump_toml(clean), "utf-8")
    return clean


def write_global(values: dict[str, Any]) -> tuple[dict[str, Any], Path]:
    """Validate + write the global settings file (every project inherits it)."""
    bad = PROJECT_ONLY & {k for k, v in values.items() if v not in (None, "")}
    if bad:
        raise ValueError(f"per-project only: {', '.join(sorted(bad))}")
    clean = _clean(values, dict(_flat_fields()))
    _check(clean)
    path = global_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    header = "# recaper global settings: every project inherits these (its own recaper.toml wins)\n"
    path.write_text(header + dump_toml(clean), "utf-8")
    return clean, path


def _check(values: dict[str, Any]) -> None:
    cfg = Config()
    from ..config import _apply

    _apply(cfg, values)  # raises on bad keys
    if cfg.render.width % 2 or cfg.render.height % 2:
        raise ValueError("width and height must be even")
    from ..render.styles import make_background

    make_background(cfg.render.background, 8, 8)  # raises on an unknown background / bad colour
    if not 0.5 <= cfg.render.fill <= 1.0:
        raise ValueError("frame fill must be between 0.5 and 1.0")


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{json.dumps(k)} = {_toml_value(x)}" for k, x in v.items()) + " }"
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    return json.dumps(str(v), ensure_ascii=False)  # JSON strings are valid TOML basic strings


def dump_toml(values: dict[str, Any]) -> str:
    top = {k: v for k, v in values.items() if "." not in k}
    tables: dict[str, dict] = {}
    for k, v in values.items():
        if "." in k:
            t, sub = k.split(".", 1)
            tables.setdefault(t, {})[sub] = v
    lines = ["# recaper settings (written by the web UI)"]
    lines += [f"{k} = {_toml_value(v)}" for k, v in top.items()]
    for t, kv in tables.items():
        lines += ["", f"[{t}]"] + [f"{k} = {_toml_value(v)}" for k, v in kv.items()]
    return "\n".join(lines) + "\n"


__all__ = ["schema", "read_values", "write_values", "dump_toml", "RenderConfig", "TTSConfig"]
