"""OpenAI (or any OpenAI-compatible server via OPENAI_BASE_URL)."""

from __future__ import annotations

import os
from typing import Sequence

from .base import ImagePart, Part, Provider, ProviderError, QuotaError, UnavailableError


class OpenAIProvider(Provider):
    name = "openai"
    vision = True

    def __init__(self, model: str | None):
        super().__init__(model or "gpt-5-mini")

    def check(self) -> None:
        if not os.environ.get("OPENAI_API_KEY"):
            raise UnavailableError("OPENAI_API_KEY not set")

    def complete(self, system: str, parts: Sequence[Part]) -> str:
        self.check()
        import openai

        content = [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{p.b64()}"}}
            if isinstance(p, ImagePart)
            else {"type": "text", "text": p}
            for p in parts
        ]
        client = openai.OpenAI()
        try:
            resp = client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": content}],
            )
        except openai.RateLimitError as e:
            raise QuotaError(str(e)) from e
        except openai.OpenAIError as e:
            raise ProviderError(str(e)) from e
        return resp.choices[0].message.content or ""
