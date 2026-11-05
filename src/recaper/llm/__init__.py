"""Provider specs: "name[:model]", comma-separated for a fallback chain.

  claude[:sonnet|opus|haiku|<model id>]      Claude via your Claude Code login (subscription)
  gemini[:model]                            GEMINI_API_KEY if set, else the `gemini` CLI login
  gemini-api[:model] / gemini-cli[:model]   force one Gemini mode
  openai[:model]                            OPENAI_API_KEY (+ OPENAI_BASE_URL for compatible servers)
  ollama[:model]                            local Ollama
  opencode[:provider/model]                 OpenCode CLI, e.g. opencode:opencode/deepseek-v4.1-flash-free
  deepseek-free                             alias for the line above
  antigravity[:model]                       Antigravity via your Google AI login, e.g. antigravity:gemini-4-argon-high
"""

from __future__ import annotations

from .base import Chain, ImagePart, Provider, ProviderError, QuotaError, RefusalError, UnavailableError, extract_json


def make_provider(spec: str) -> Provider:
    name, _, model = spec.strip().partition(":")
    name = name.lower()
    model = model or None
    if name == "claude":
        from .claude import ClaudeProvider

        return ClaudeProvider(model)
    if name in ("gemini", "gemini-api", "gemini-cli", "google"):
        from .gemini import GeminiProvider

        mode = {"gemini-api": "api", "gemini-cli": "cli"}.get(name, "auto")
        return GeminiProvider(model, mode=mode)
    if name == "openai":
        from .openai_provider import OpenAIProvider

        return OpenAIProvider(model)
    if name == "ollama":
        from .ollama_provider import OllamaProvider

        return OllamaProvider(model)
    if name == "opencode":
        from .opencode import OpencodeProvider

        return OpencodeProvider(model)
    if name in ("antigravity", "agy"):
        from .antigravity import AntigravityProvider

        return AntigravityProvider(model)
    raise ValueError(f"unknown provider {name!r} in spec {spec!r}")


def make_chain(spec: str, role: str, need_vision: bool) -> Chain:
    providers = [make_provider(s) for s in spec.split(",") if s.strip()]
    if need_vision:
        blind = [p for p in providers if not p.vision]
        providers = [p for p in providers if p.vision]
        if blind and not providers:
            raise ValueError(
                f"{role}: {', '.join(p.label() for p in blind)} can't see images. "
                "Use a vision model to read panels (e.g. --reader claude) and the text model as --writer."
            )
    return Chain(providers, role)


__all__ = [
    "Chain",
    "ImagePart",
    "Provider",
    "ProviderError",
    "QuotaError",
    "RefusalError",
    "UnavailableError",
    "extract_json",
    "make_chain",
    "make_provider",
]
