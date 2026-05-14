"""Verify `GatewayClient.stream_completion` forwards `cache_breakpoint`.

The Anthropic prompt-cache wire-up relies on the gateway receiving the
`cache_breakpoint` flag on stable-prefix messages. The flag is what the
gateway's Rust adapter translates into `cache_control: { type: "ephemeral" }`
for Anthropic (native or proxied). Caller-side regressions are silent —
they don't raise, they just stop yielding cache hits — so this test asserts
the bytes-on-the-wire shape directly.
"""

from __future__ import annotations

import json
from uuid import uuid4

from agent_worker.gateway_client import GatewayClient


async def test_stream_completion_forwards_cache_breakpoint(httpx_mock) -> None:  # type: ignore[no-untyped-def]
    """A message with `cache_breakpoint: True` must reach the gateway intact."""
    httpx_mock.add_response(
        url="http://gw.invalid/llm/chat/completions",
        method="POST",
        # Minimal valid SSE body so the stream parser terminates cleanly.
        text="data: [DONE]\n\n",
        headers={"content-type": "text/event-stream"},
    )

    client = GatewayClient("http://gw.invalid", dev_token="t")
    try:
        # Drain the (empty) stream to make sure the request was sent.
        async for _ in client.stream_completion(
            run_id=uuid4(),
            agent="analyzer",
            model="gpt-5.5",
            messages=[
                {
                    "role": "system",
                    "content": "stable system prompt",
                    "cache_breakpoint": True,
                },
                {"role": "user", "content": "go"},
            ],
        ):
            pass
    finally:
        await client.close()

    sent = httpx_mock.get_request()
    assert sent is not None, "gateway must be called"
    body = json.loads(sent.read())
    msgs = body["messages"]
    assert len(msgs) == 2

    # System message keeps the breakpoint flag — the gateway uses it to emit
    # Anthropic-style cache_control on the corresponding content block.
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"] == "stable system prompt"
    assert msgs[0]["cache_breakpoint"] is True, (
        "cache_breakpoint must round-trip through the gateway client untouched; "
        "missing this flag silently disables prompt caching."
    )

    # User message has no flag — verifies we didn't accidentally tag everything.
    assert msgs[1]["role"] == "user"
    assert "cache_breakpoint" not in msgs[1]


async def test_stream_completion_without_breakpoint_omits_flag(httpx_mock) -> None:  # type: ignore[no-untyped-def]
    """Default callers (no flag) must produce a body with no `cache_breakpoint`."""
    httpx_mock.add_response(
        url="http://gw.invalid/llm/chat/completions",
        method="POST",
        text="data: [DONE]\n\n",
        headers={"content-type": "text/event-stream"},
    )

    client = GatewayClient("http://gw.invalid", dev_token="t")
    try:
        async for _ in client.stream_completion(
            run_id=uuid4(),
            agent="analyzer",
            model="gpt-5.5",
            messages=[
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "go"},
            ],
        ):
            pass
    finally:
        await client.close()

    sent = httpx_mock.get_request()
    assert sent is not None
    body = json.loads(sent.read())
    for m in body["messages"]:
        assert "cache_breakpoint" not in m, (
            "absent flag must not be filled in by the client (gateway treats "
            "the field as default-false)"
        )
