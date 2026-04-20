"""HTTP client for the Rust gateway's `/llm/chat/completions` endpoint.

The gateway is OpenAI-compatible and, when called with `stream: true`, returns
SSE lines of the form:

    data: {"choices":[{"delta":{"content":"..."}}]}\n\n
    data: [DONE]\n\n

It is also responsible for fanning `token` and `cost` events into the run's
WebSocket stream, so callers here only need the concatenated assistant text.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import httpx


class GatewayClient:
    """Thin wrapper around the gateway's chat-completions endpoint."""

    def __init__(self, base_url: str, dev_token: str) -> None:
        self._base = base_url.rstrip("/")
        # Total timeout 600s for long completions; per-chunk read timeout 30s.
        timeout = httpx.Timeout(600.0, connect=5.0, read=30.0)
        self._client = httpx.AsyncClient(timeout=timeout)
        self._headers = {"Authorization": f"Bearer {dev_token}"}

    async def close(self) -> None:
        await self._client.aclose()

    def _build_headers(self, run_id: UUID, agent: str) -> dict[str, str]:
        return {
            **self._headers,
            "X-Run-Id": str(run_id),
            "X-Agent": agent,
        }

    def _build_body(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float,
        max_tokens: int | None,
        response_format: dict[str, Any] | None,
        stream: bool,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": stream,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if response_format is not None:
            body["response_format"] = response_format
        return body

    async def stream_completion(
        self,
        *,
        run_id: UUID,
        agent: str,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float = 0.2,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> AsyncIterator[str]:
        """Yield text deltas from the gateway's SSE stream.

        Parses each `data: <json>` line, extracts `choices[0].delta.content`,
        and yields it. Ignores the terminal `data: [DONE]`. Non-JSON / keep-
        alive lines are skipped silently.
        """
        url = f"{self._base}/llm/chat/completions"
        headers = self._build_headers(run_id, agent)
        body = self._build_body(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            stream=True,
        )

        async with self._client.stream("POST", url, headers=headers, json=body) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    continue
                if not line.startswith("data:"):
                    # Could be an SSE `event:` or `id:` line — ignore for now.
                    continue
                data = line[len("data:") :].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = obj.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                if content:
                    yield content

    async def complete(
        self,
        *,
        run_id: UUID,
        agent: str,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float = 0.2,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        """Non-streaming chat completion. Returns the assistant message text.

        Intended for cached calls (M4 wires it up but nothing calls it yet).
        """
        url = f"{self._base}/llm/chat/completions"
        headers = self._build_headers(run_id, agent)
        body = self._build_body(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            stream=False,
        )

        resp = await self._client.post(url, headers=headers, json=body)
        resp.raise_for_status()
        obj = resp.json()
        choices = obj.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        return message.get("content") or ""


__all__ = ["GatewayClient"]
