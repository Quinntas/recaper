"""Local models through Ollama (OLLAMA_HOST, default http://localhost:11434)."""

from __future__ import annotations
import os

from typing import Sequence

from .base import ImagePart, Part, Provider, ProviderError, UnavailableError


class OllamaProvider(Provider):
    name = "ollama"
    vision = True  # assumes a VL model (qwen2.5vl, llama3.2-vision, gemma3...) when used as reader

    def __init__(self, model: str | None):
        super().__init__(model or "qwen2.5vl:7b")

    def check(self) -> None:
        import ollama

        try:
            ollama.list()
        except Exception as e:  # noqa: BLE001
            raise UnavailableError(f"ollama server not reachable: {e}") from e

    def complete(self, system: str, parts: Sequence[Part]) -> str:
        import ollama

        # Ollama attaches images to the message rather than interleaving them,
        # so label them in order inside the text.
        text, images, n = [], [], 0
        for p in parts:
            if isinstance(p, ImagePart):
                n += 1
                images.append(p.jpeg_bytes())
                text.append(f"(attached image #{n})")
            else:
                text.append(p)
        try:
            resp = ollama.chat(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": "\n".join(text), **({"images": images} if images else {})},
                ],
                format="json",
                options={
                    "num_ctx": int(os.environ.get("OLLAMA_NUM_CTX", "65536")),
                    "num_predict": int(os.environ.get("OLLAMA_NUM_PREDICT", "4096")),
                    "temperature": 0.2,
                },
            )
        except Exception as e:  # noqa: BLE001
            raise ProviderError(str(e)) from e
        return resp["message"]["content"]
