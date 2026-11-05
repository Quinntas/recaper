"""Claude through the local Claude Code login (your Claude subscription) via the Agent SDK.

If ANTHROPIC_API_KEY is set, Claude Code bills that key instead; unset it to use the subscription.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from typing import Sequence

from .base import ImagePart, Part, Provider, ProviderError, QuotaError, UnavailableError, is_quota_message


class ClaudeProvider(Provider):
    name = "claude"
    vision = True

    def __init__(self, model: str | None):
        super().__init__(model or "sonnet")

    def check(self) -> None:
        try:
            import claude_agent_sdk  # noqa: F401
        except ImportError as e:
            raise UnavailableError("claude-agent-sdk not installed") from e

    def complete(self, system: str, parts: Sequence[Part]) -> str:
        return asyncio.run(self._complete(system, parts))

    async def _complete(self, system: str, parts: Sequence[Part]) -> str:
        from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

        content = []
        for p in parts:
            if isinstance(p, ImagePart):
                content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": p.b64()}})
            else:
                content.append({"type": "text", "text": p})

        async def messages():
            yield {"type": "user", "message": {"role": "user", "content": content}, "parent_tool_use_id": None}

        workdir = tempfile.mkdtemp(prefix="recaper-claude-")
        options = ClaudeAgentOptions(
            system_prompt=system,
            model=self.model,
            max_turns=1,
            tools=[],  # pure text generation: no built-in tools
            setting_sources=[],  # don't load the user's CLAUDE.md / rules / hooks
            cwd=workdir,
        )
        result = None
        try:
            async for msg in query(prompt=messages(), options=options):
                if isinstance(msg, ResultMessage):
                    result = msg
        except Exception as e:  # noqa: BLE001
            if is_quota_message(str(e)):  # plan cap: stop using Claude this run
                raise QuotaError(str(e)) from e
            raise ProviderError(str(e)) from e
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

        if result is None:
            raise ProviderError("no result from Claude")
        if result.is_error:
            msg = f"{result.subtype}: {result.result or result.errors}"
            if is_quota_message(msg):
                raise QuotaError(msg)  # plan cap; plain 429 rate limits are retried by the chain
            raise ProviderError(msg)
        return result.result or ""
