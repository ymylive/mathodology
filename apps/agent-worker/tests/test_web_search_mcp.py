"""Tests for the open-webSearch MCP client.

Unit tests monkeypatch the stdio transport and ClientSession so no Node
subprocess is ever spawned. An optional slow/integration test (``-m mcp``)
exercises the real binary; it's opt-in and off by default.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pytest

from agent_worker.agents.searcher import _normalize_url
from agent_worker.tools.web_search_mcp import (
    WebResult,
    _EngineDebouncer,
    _extract_results_json,
    _payload_to_results,
    batch_search_web,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeText:
    text: str


@dataclass
class _FakeToolResult:
    content: list[_FakeText]
    isError: bool = False  # noqa: N815 — mirrors MCP SDK


class _FakeSession:
    """Minimal async context-manager + session surface.

    Tracks every call_tool invocation with monotonic timestamps so we can
    assert on rate-limit spacing.
    """

    def __init__(
        self,
        response_map: dict[str, Any],
        *,
        per_call_delay: float = 0.0,
        fail_initialize: bool = False,
    ) -> None:
        self._responses = response_map
        self._delay = per_call_delay
        self._fail_init = fail_initialize
        self.calls: list[tuple[str, dict[str, Any], float]] = []

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def initialize(self) -> None:
        if self._fail_init:
            raise RuntimeError("simulated init failure")

    async def call_tool(self, name: str, args: dict[str, Any]) -> _FakeToolResult:
        t = time.monotonic()
        self.calls.append((name, args, t))
        if self._delay:
            await asyncio.sleep(self._delay)
        key = args.get("query", "")
        resp = self._responses.get(key, self._responses.get("*"))
        if isinstance(resp, Exception):
            raise resp
        if resp is None:
            return _FakeToolResult(content=[])
        if isinstance(resp, _FakeToolResult):
            return resp
        # resp is a dict → wrap it as JSON TextContent
        import orjson

        return _FakeToolResult(
            content=[_FakeText(text=orjson.dumps(resp).decode())]
        )


def _session_factory_for(
    session: _FakeSession,
) -> Any:  # noqa: ANN401 — test helper
    """Return a callable that, like ClientSession(read, write), yields the
    given fake session as an async context manager."""

    def _make(_read: object, _write: object) -> _FakeSession:
        return session

    return _make


def _stdio_factory_ok(_cmd: str) -> Any:  # noqa: ANN401
    @asynccontextmanager
    async def _cm():
        yield (object(), object())  # (read_stream, write_stream) sentinels

    return _cm()


def _stdio_factory_that_raises(_cmd: str) -> Any:  # noqa: ANN401
    @asynccontextmanager
    async def _cm():
        raise RuntimeError("cannot spawn node")
        yield  # unreachable, keeps this a valid async-gen

    return _cm()


# ---------------------------------------------------------------------------
# Pure-function unit tests
# ---------------------------------------------------------------------------


def test_normalize_url_strips_trailing_slash_and_utm() -> None:
    a = _normalize_url("https://Example.com/path/?utm_source=x&id=1")
    b = _normalize_url("https://example.com/path?id=1")
    assert a == b


def test_normalize_url_keeps_non_tracking_query() -> None:
    n = _normalize_url("https://example.com/x?id=1&utm_campaign=z")
    assert "id=1" in n
    assert "utm_campaign" not in n


def test_extract_results_json_handles_single_textcontent() -> None:
    payload = {
        "query": "q",
        "engines": ["bing"],
        "totalResults": 1,
        "results": [
            {
                "title": "t",
                "url": "https://x",
                "description": "d",
                "engine": "bing",
                "source": "x",
            }
        ],
    }
    import orjson

    fake = _FakeToolResult(content=[_FakeText(text=orjson.dumps(payload).decode())])
    out = _extract_results_json(fake.content)
    assert out == payload


def test_extract_results_json_returns_none_on_garbage() -> None:
    assert _extract_results_json([_FakeText(text="not json")]) is None
    assert _extract_results_json([]) is None


def test_payload_to_results_drops_missing_title_or_url() -> None:
    payload = {
        "results": [
            {"title": "ok", "url": "https://a", "description": "", "engine": "bing", "source": "s"},
            {"title": "", "url": "https://b"},  # empty title → dropped
            {"title": "t", "url": ""},  # empty url → dropped
            {"title": "both", "url": "https://c", "engine": "baidu"},
        ]
    }
    got = _payload_to_results(payload, query="q")
    urls = [r.url for r in got]
    assert urls == ["https://a", "https://c"]
    assert all(r.query == "q" for r in got)


# ---------------------------------------------------------------------------
# Engine debouncer
# ---------------------------------------------------------------------------


async def test_engine_debouncer_sleeps_between_same_engine() -> None:
    # Tiny cooldown so the test stays fast but the ordering invariant holds.
    d = _EngineDebouncer(cooldown_s=0.05)
    t0 = time.monotonic()
    await d.acquire(["bing"])
    await d.acquire(["bing"])
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.05  # second acquire waited out the cooldown


async def test_engine_debouncer_disjoint_engines_are_free() -> None:
    d = _EngineDebouncer(cooldown_s=0.1)
    t0 = time.monotonic()
    await d.acquire(["bing"])
    await d.acquire(["baidu"])  # different engine → no wait
    assert time.monotonic() - t0 < 0.05


# ---------------------------------------------------------------------------
# batch_search_web
# ---------------------------------------------------------------------------


async def test_batch_search_web_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "query": "math modeling",
        "engines": ["bing"],
        "totalResults": 1,
        "results": [
            {
                "title": "Hello",
                "url": "https://example.com/a",
                "description": "snippet",
                "engine": "bing",
                "source": "example.com",
            }
        ],
    }
    session = _FakeSession({"math modeling": payload})

    # resolve any command string to a valid path
    monkeypatch.setattr(
        "agent_worker.tools.web_search_mcp._resolve_command",
        lambda cmd: "/usr/bin/true",
    )

    out = await batch_search_web(
        ["math modeling"],
        engines=("bing",),
        max_per_query=3,
        concurrency=1,
        command="fake",
        stdio_client_factory=_stdio_factory_ok,
        session_factory=_session_factory_for(session),
    )
    assert list(out.keys()) == ["math modeling"]
    hits = out["math modeling"]
    assert len(hits) == 1
    assert isinstance(hits[0], WebResult)
    assert hits[0].engine == "bing"
    assert hits[0].url == "https://example.com/a"
    # The MCP call was made exactly once with the right engine set.
    assert len(session.calls) == 1
    assert session.calls[0][1]["engines"] == ["bing"]
    assert session.calls[0][1]["limit"] == 3


async def test_batch_search_web_returns_empty_when_command_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "agent_worker.tools.web_search_mcp._resolve_command",
        lambda cmd: None,
    )
    out = await batch_search_web(
        ["q1", "q2"],
        engines=("bing",),
        command="definitely-not-installed",
        stdio_client_factory=_stdio_factory_ok,
        session_factory=_session_factory_for(_FakeSession({})),
    )
    assert out == {"q1": [], "q2": []}


async def test_batch_search_web_empty_queries_short_circuits() -> None:
    assert await batch_search_web([]) == {}


async def test_batch_search_web_degrades_when_spawn_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "agent_worker.tools.web_search_mcp._resolve_command",
        lambda cmd: "/usr/bin/true",
    )
    out = await batch_search_web(
        ["q"],
        engines=("bing",),
        command="fake",
        stdio_client_factory=_stdio_factory_that_raises,
        session_factory=_session_factory_for(_FakeSession({})),
    )
    assert out == {"q": []}


async def test_batch_search_web_degrades_when_initialize_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "agent_worker.tools.web_search_mcp._resolve_command",
        lambda cmd: "/usr/bin/true",
    )
    session = _FakeSession({}, fail_initialize=True)
    out = await batch_search_web(
        ["q"],
        engines=("bing",),
        command="fake",
        stdio_client_factory=_stdio_factory_ok,
        session_factory=_session_factory_for(session),
    )
    assert out == {"q": []}


async def test_batch_search_web_single_query_timeout_does_not_cascade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Query "slow" sleeps 0.5s; 0.05s timeout will trip. Other queries succeed.
    payload = {
        "results": [
            {
                "title": "ok",
                "url": "https://fast.example/ok",
                "description": "",
                "engine": "bing",
                "source": "fast.example",
            }
        ]
    }

    class _MixedSession(_FakeSession):
        async def call_tool(
            self, name: str, args: dict[str, Any]
        ) -> _FakeToolResult:
            self.calls.append((name, args, time.monotonic()))
            if args["query"] == "slow":
                await asyncio.sleep(0.5)
            return _FakeToolResult(
                content=[
                    _FakeText(
                        text=__import__("orjson").dumps(payload).decode()
                    )
                ]
            )

    session = _MixedSession({})
    monkeypatch.setattr(
        "agent_worker.tools.web_search_mcp._resolve_command",
        lambda cmd: "/usr/bin/true",
    )

    out = await batch_search_web(
        ["fast", "slow"],
        engines=("bing",),
        concurrency=2,
        timeout_s=0.05,
        command="fake",
        stdio_client_factory=_stdio_factory_ok,
        session_factory=_session_factory_for(session),
    )
    assert out["slow"] == []
    assert len(out["fast"]) == 1


async def test_batch_search_web_respects_per_engine_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three sequential queries on the same engine must be spaced by the
    per-engine debouncer (monotonically increasing timestamps with delta
    >= cooldown minus a small jitter)."""
    payload = {"results": []}
    session = _FakeSession({"*": payload})

    monkeypatch.setattr(
        "agent_worker.tools.web_search_mcp._resolve_command",
        lambda cmd: "/usr/bin/true",
    )
    # Shrink the cooldown so the test is fast; the invariant (first > second
    # start + cooldown) is what we care about, not the absolute value.
    monkeypatch.setattr(
        "agent_worker.tools.web_search_mcp._ENGINE_COOLDOWN_S", 0.08
    )
    # _EngineDebouncer snapshots cooldown from the module const at construction
    # time (via default param), so it reads the patched value.

    queries = ["q1", "q2", "q3"]
    out = await batch_search_web(
        queries,
        engines=("bing",),  # single engine → all queries serialize on it
        concurrency=3,
        command="fake",
        stdio_client_factory=_stdio_factory_ok,
        session_factory=_session_factory_for(session),
    )
    assert set(out.keys()) == set(queries)
    # Inspect inter-call gaps.
    call_times = [t for (_n, _a, t) in session.calls]
    assert len(call_times) == 3
    gaps = [
        call_times[i] - call_times[i - 1] for i in range(1, len(call_times))
    ]
    # Every gap should be at least ~cooldown (allow 20 ms scheduler slack).
    assert all(g >= 0.06 for g in gaps), gaps


async def test_batch_search_web_swallows_tool_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising call_tool for one query must not poison other queries."""

    class _FlakySession(_FakeSession):
        async def call_tool(
            self, name: str, args: dict[str, Any]
        ) -> _FakeToolResult:
            self.calls.append((name, args, time.monotonic()))
            if args["query"] == "bad":
                raise RuntimeError("boom")
            import orjson

            return _FakeToolResult(
                content=[
                    _FakeText(
                        text=orjson.dumps(
                            {
                                "results": [
                                    {
                                        "title": "ok",
                                        "url": "https://g.example/",
                                        "description": "",
                                        "engine": "bing",
                                        "source": "g",
                                    }
                                ]
                            }
                        ).decode()
                    )
                ]
            )

    session = _FlakySession({})
    monkeypatch.setattr(
        "agent_worker.tools.web_search_mcp._resolve_command",
        lambda cmd: "/usr/bin/true",
    )
    out = await batch_search_web(
        ["good", "bad"],
        engines=("bing",),
        concurrency=2,
        command="fake",
        stdio_client_factory=_stdio_factory_ok,
        session_factory=_session_factory_for(session),
    )
    assert out["bad"] == []
    assert len(out["good"]) == 1


async def test_batch_search_web_ignores_is_error_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # isError=True means the MCP tool itself reported a failure.
    class _ErrSession(_FakeSession):
        async def call_tool(
            self, name: str, args: dict[str, Any]
        ) -> _FakeToolResult:
            self.calls.append((name, args, time.monotonic()))
            return _FakeToolResult(content=[_FakeText(text="{}")], isError=True)

    session = _ErrSession({})
    monkeypatch.setattr(
        "agent_worker.tools.web_search_mcp._resolve_command",
        lambda cmd: "/usr/bin/true",
    )
    out = await batch_search_web(
        ["q"],
        engines=("bing",),
        command="fake",
        stdio_client_factory=_stdio_factory_ok,
        session_factory=_session_factory_for(session),
    )
    assert out == {"q": []}


# ---------------------------------------------------------------------------
# Optional integration test: real subprocess, gated by `-m mcp`.
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.mcp
async def test_integration_real_open_websearch() -> None:  # pragma: no cover
    """Spawns the real `open-websearch` binary. Off by default (slow, network).

    Run with: ``pytest -m mcp -x``
    """
    import shutil as _sh

    if _sh.which("open-websearch") is None:
        pytest.skip("open-websearch not installed on this host")
    out = await batch_search_web(
        ["math modeling queueing"],
        engines=("duckduckgo",),
        max_per_query=3,
        concurrency=1,
        timeout_s=45.0,
    )
    # Don't assert >0 hits (engines may captcha at test time); do assert the
    # key is present and the value is a list of WebResult (structural check).
    assert "math modeling queueing" in out
    assert all(isinstance(r, WebResult) for r in out["math modeling queueing"])
