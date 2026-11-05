"""Image generation for beats no real image fits (books / light novels mostly).

Each request carries reference images taken from the work's official art (cover, illustrations, linked
adaptation panels) so the result matches its style and characters. Providers:
  gemini[:model]   GEMINI_API_KEY / GOOGLE_API_KEY - default model gemini-2.5-flash-image
  openai[:model]   OPENAI_API_KEY - default model gpt-image-1 (images.edit with the references)
Results are cached by (provider, prompt, references), so re-runs never pay twice.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
from pathlib import Path

from PIL import Image

from .log import console


class ImageGenError(RuntimeError):
    pass


def available(spec: str) -> tuple[bool, str]:
    name = (spec or "none").split(":", 1)[0].lower()
    if name in ("", "none"):
        return False, "image generation is off"
    if name == "gemini":
        ok = bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
        return ok, "" if ok else "set GEMINI_API_KEY to generate images with Gemini"
    if name == "openai":
        ok = bool(os.environ.get("OPENAI_API_KEY"))
        return ok, "" if ok else "set OPENAI_API_KEY to generate images with OpenAI"
    return False, f"unknown image generator {spec!r} (none | gemini[:model] | openai[:model])"


def _ref_bytes(path: Path, max_edge: int = 1024) -> bytes:
    with Image.open(path) as im:
        im = im.convert("RGB")
        s = max_edge / max(im.size)
        if s < 1:
            im = im.resize((round(im.width * s), round(im.height * s)), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return buf.getvalue()


def generate(spec: str, prompt: str, refs: list[Path], dest: Path) -> Path:
    """Generate one 16:9 image to `dest` (PNG). Raises ImageGenError."""
    ok, why = available(spec)
    if not ok:
        raise ImageGenError(why)
    name, _, model = spec.partition(":")
    name = name.lower()
    dest.parent.mkdir(parents=True, exist_ok=True)
    full = (
        f"{prompt}\n\nUse the attached reference images (official art of this work) for the art style and for how "
        "the characters look. Widescreen 16:9 composition. No text, no speech bubbles, no captions, no watermark."
        if refs
        else f"{prompt}\n\nWidescreen 16:9 composition. No text, no speech bubbles, no captions, no watermark."
    )
    try:
        data = _gemini(model or "gemini-2.5-flash-image", full, refs) if name == "gemini" else _openai(model or "gpt-image-1", full, refs)
    except ImageGenError:
        raise
    except Exception as e:  # noqa: BLE001 - surface provider errors uniformly
        raise ImageGenError(f"{name}: {e}") from e
    with Image.open(io.BytesIO(data)) as im:
        im.convert("RGB").save(dest, "PNG")
    return dest


def _gemini(model: str, prompt: str, refs: list[Path]) -> bytes:
    from google import genai
    from google.genai import types

    client = genai.Client()
    contents: list = [types.Part.from_bytes(data=_ref_bytes(r), mime_type="image/png") for r in refs]
    contents.append(prompt)
    resp = client.models.generate_content(
        model=model,
        contents=contents,
        config=types.GenerateContentConfig(response_modalities=["IMAGE"], image_config=types.ImageConfig(aspect_ratio="16:9")),
    )
    for cand in resp.candidates or []:
        for part in (cand.content.parts if cand.content else []) or []:
            if part.inline_data and part.inline_data.data:
                data = part.inline_data.data
                return data if isinstance(data, bytes) else base64.b64decode(data)
    raise ImageGenError("gemini returned no image (possibly blocked by its safety filter)")


def _openai(model: str, prompt: str, refs: list[Path]) -> bytes:
    import openai

    client = openai.OpenAI()
    if refs:
        files = [(r.name, _ref_bytes(r), "image/png") for r in refs]
        resp = client.images.edit(model=model, image=files, prompt=prompt, size="1536x1024")
    else:
        resp = client.images.generate(model=model, prompt=prompt, size="1536x1024")
    b64 = resp.data[0].b64_json if resp.data else None
    if not b64:
        raise ImageGenError("openai returned no image")
    return base64.b64decode(b64)


def cache_key(spec: str, prompt: str, refs: list[Path]) -> str:
    h = hashlib.sha1()
    h.update(f"{spec}\n{prompt}\n".encode())
    for r in refs:
        h.update(f"{r.name}:{r.stat().st_size}\n".encode())
    return h.hexdigest()[:16]


def warn_once(msg: str, _seen: set[str] = set()) -> None:  # noqa: B006 - intentional module-level memo
    if msg not in _seen:
        _seen.add(msg)
        console.print(f"[yellow]{msg}[/]")
