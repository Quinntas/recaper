"""Google Gemini: API key (google-genai) when GEMINI_API_KEY / GOOGLE_API_KEY is set,
otherwise the logged-in `gemini` CLI (your Google subscription) in headless mode."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Sequence

from .base import ImagePart, Part, Provider, ProviderError, QuotaError, UnavailableError


def _has_key() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))


class GeminiProvider(Provider):
    name = "gemini"
    vision = True

    def __init__(self, model: str | None, mode: str = "auto"):
        super().__init__(model)
        self.mode = mode  # auto | api | cli

    def _use_api(self) -> bool:
        if self.mode == "api":
            return True
        if self.mode == "cli":
            return False
        return _has_key()

    def check(self) -> None:
        if self._use_api():
            if not _has_key():
                raise UnavailableError("GEMINI_API_KEY not set")
        elif not shutil.which("gemini"):
            raise UnavailableError("no GEMINI_API_KEY and `gemini` CLI not found (npm i -g @google/gemini-cli)")

    def complete(self, system: str, parts: Sequence[Part]) -> str:
        self.check()
        return self._api(system, parts) if self._use_api() else self._cli(system, parts)

    # -- API -------------------------------------------------------------------
    def _api(self, system: str, parts: Sequence[Part]) -> str:
        from google import genai
        from google.genai import errors, types

        client = genai.Client()
        contents = [types.Part.from_bytes(data=p.jpeg_bytes(), mime_type="image/jpeg") if isinstance(p, ImagePart) else p for p in parts]
        try:
            resp = client.models.generate_content(
                model=self.model or "gemini-flash-latest",
                contents=contents,
                config=types.GenerateContentConfig(system_instruction=system, response_mime_type="application/json"),
            )
        except errors.APIError as e:
            if getattr(e, "code", None) == 429:
                raise QuotaError(str(e)) from e
            raise ProviderError(str(e)) from e
        return resp.text or ""

    # -- CLI -------------------------------------------------------------------
    def _cli(self, system: str, parts: Sequence[Part]) -> str:
        workdir = Path(tempfile.mkdtemp(prefix="recaper-gemini-"))
        try:
            chunks = [system, ""]
            n = 0
            for p in parts:
                if isinstance(p, ImagePart):
                    n += 1
                    name = f"img_{n:03d}.jpg"
                    (workdir / name).write_bytes(p.jpeg_bytes())
                    chunks.append(f"@{name}")
                else:
                    chunks.append(p)
            prompt = "\n".join(chunks)
            cmd = [shutil.which("gemini") or "gemini", "--output-format", "json", "--skip-trust"]
            if self.model:
                cmd += ["-m", self.model]
            # Long prompts go through stdin (Windows caps argv at ~32k chars); -p is appended to stdin.
            cmd += ["-p", "Follow the instructions above."]
            proc = subprocess.run(cmd, input=prompt, cwd=workdir, capture_output=True, text=True, encoding="utf-8", timeout=900)
            out = proc.stdout.strip()
            err = proc.stderr.strip()
            if proc.returncode != 0:
                if "quota" in (out + err).lower() or "429" in out + err:
                    raise QuotaError(err[-400:] or out[-400:])
                raise ProviderError(f"gemini CLI exit {proc.returncode}: {(err or out)[-400:]}")
            try:
                return json.loads(out).get("response", "")
            except json.JSONDecodeError:
                return out
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
