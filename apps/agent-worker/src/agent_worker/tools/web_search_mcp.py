"""open-webSearch MCP client — multi-engine web search for the Searcher agent.

Spawns one `open-websearch` subprocess over stdio per batch, issues parallel
`search` tool calls (bounded concurrency), then tears it down. Designed to
mirror `batch_search_arxiv`'s failure semantics: never raise — if Node is
missing, the binary errors, or a single query times out, the caller gets an
empty result for that query and the pipeline continues.

Rate-limit note: the upstream README warns that all backing engines (Bing,
Baidu, DuckDuckGo, CSDN, Juejin, ...) will IP-ban under rapid-fire queries.
We enforce a per-engine debounce: at most one query per engine per second,
via a map of asyncio.Locks keyed on engine name. Queries that target multiple
engines acquire all the relevant locks in order.

The open-websearch tool returns a single TextContent whose text is JSON of
shape ``{query, engines, totalResults, results: [{title,url,description,
source,engine}], partialFailures}``. We pull the `results` array out and
wrap each entry in a frozen `WebResult` dataclass.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from collections.abc import Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

import orjson

_log = logging.getLogger(__name__)

# Per-engine debounce: README warns of IP bans; 1 query / engine / second is
# the documented safe cadence.
_ENGINE_COOLDOWN_S = 1.0

# Default search tool name on the MCP server.
_SEARCH_TOOL = "search"


@dataclass(frozen=True)
class WebResult:
    """One hit from a web search engine (normalized across engines)."""

    title: str
    url: str
    description: str  # search-engine snippet, used as pseudo-abstract
    engine: str  # "bing" / "baidu" / "csdn" / ...
    source: str  # site/domain reported by the engine
    query: str  # original user query that produced this hit


class _EngineDebouncer:
    """Per-engine token bucket: sleep until `cooldown_s` has elapsed since the
    previous acquire for the same engine, then stamp."""

    def __init__(self, cooldown_s: float | None = None) -> None:
        # Read the module const lazily so tests that monkeypatch the constant
        # take effect for Debouncers built inside batch_search_web.
        self._cooldown = (
            cooldown_s if cooldown_s is not None else _ENGINE_COOLDOWN_S
        )
        self._last: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, engine: str) -> asyncio.Lock:
        lk = self._locks.get(engine)
        if lk is None:
            lk = asyncio.Lock()
            self._locks[engine] = lk
        return lk

    async def acquire(self, engines: Sequence[str]) -> None:
        """Sequentially wait out cooldown for each engine in the set.

        We don't hold the locks for the duration of the search — just long
        enough to stamp the previous-query timestamp. This means N concurrent
        queries to the same engine serialize through it at 1/s, while queries
        to disjoint engines run freely in parallel.
        """
        for eng in engines:
            lock = self._lock_for(eng)
            async with lock:
                now = time.monotonic()
                prev = self._last.get(eng, 0.0)
                wait = self._cooldown - (now - prev)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last[eng] = time.monotonic()


def _resolve_command(cmd: str) -> str | None:
    """Resolve `cmd` to a usable executable path, or None if we can't find one.

    Tries, in order: the string as given (if absolute or on PATH), and
    `shutil.which`. Returning None is a hint to the caller to degrade.
    """
    if cmd.startswith("/") or cmd.startswith("./"):
        # Trust absolute/relative; subprocess will error loudly if wrong.
        return cmd
    resolved = shutil.which(cmd)
    return resolved


def _extract_results_json(content: Any) -> dict[str, Any] | None:
    """Pull the JSON payload out of a CallToolResult.content list.

    open-websearch emits one TextContent whose `.text` is a JSON blob. Some
    MCP servers split into multiple parts, so we concatenate text-bearing
    items and try to parse once. Returns None if nothing parseable is found.
    """
    if not content:
        return None
    pieces: list[str] = []
    for item in content:
        txt = getattr(item, "text", None)
        if isinstance(txt, str) and txt:
            pieces.append(txt)
    if not pieces:
        return None
    raw = "".join(pieces).strip()
    try:
        parsed = orjson.loads(raw)
    except Exception as e:  # noqa: BLE001
        _log.warning("open-websearch returned non-JSON text: %s", e)
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _payload_to_results(
    payload: dict[str, Any], query: str
) -> list[WebResult]:
    """Convert an open-websearch response dict into WebResult records."""
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        return []
    out: list[WebResult] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or "").strip()
        url = (item.get("url") or "").strip()
        if not title or not url:
            continue
        out.append(
            WebResult(
                title=title,
                url=url,
                description=(item.get("description") or "").strip(),
                engine=(item.get("engine") or "").strip() or "unknown",
                source=(item.get("source") or "").strip(),
                query=query,
            )
        )
    return out


async def batch_search_web(
    queries: Sequence[str],
    engines: Sequence[str] = ("bing", "duckduckgo", "baidu", "csdn", "juejin"),
    max_per_query: int = 5,
    concurrency: int = 2,
    timeout_s: float = 30.0,
    *,
    command: str = "open-websearch",
    stdio_client_factory: Any = None,  # noqa: ANN401 — test-only seam
    session_factory: Any = None,  # noqa: ANN401 — test-only seam
) -> dict[str, list[WebResult]]:
    """Run N queries through one open-websearch MCP subprocess.

    Returns a dict mapping original query → list of WebResult. Empty list on
    per-query failure; empty dict if the whole subprocess / session fails to
    start. Never raises.

    Parameters:
        queries: batch of search strings.
        engines: backing engines to hit on each query; stacked in the tool
            call, so one MCP round-trip covers all of them.
        max_per_query: `limit` passed to the MCP tool (per-engine cap).
        concurrency: upper bound on in-flight searches; also bounded by the
            per-engine debouncer.
        timeout_s: hard ceiling for a single `call_tool`. Exceeded queries
            return [] (degrade — do not cascade).
        command: executable. Absolute path wins; otherwise resolved via PATH.
        stdio_client_factory / session_factory: injection hooks for tests.
    """
    if not queries:
        return {}

    # Late-import so importing this module doesn't pull the whole mcp stack
    # (useful for unit tests that monkeypatch these hooks).
    if stdio_client_factory is None or session_factory is None:
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as e:
            _log.warning("mcp SDK not importable; skipping web search: %s", e)
            return {q: [] for q in queries}

        if stdio_client_factory is None:
            def _default_stdio_factory(command: str) -> Any:  # noqa: ANN401
                params = StdioServerParameters(
                    command=command,
                    args=[],
                    env={"DEFAULT_SEARCH_ENGINE": engines[0] if engines else "bing"},
                )
                return stdio_client(params)
            stdio_client_factory = _default_stdio_factory
        if session_factory is None:
            session_factory = ClientSession

    resolved_cmd = _resolve_command(command)
    if resolved_cmd is None:
        _log.warning(
            "open-websearch command %r not found on PATH; skipping web search",
            command,
        )
        return {q: [] for q in queries}

    results: dict[str, list[WebResult]] = {q: [] for q in queries}
    debouncer = _EngineDebouncer()
    sem = asyncio.Semaphore(max(1, concurrency))

    try:
        async with AsyncExitStack() as stack:
            try:
                transport = await stack.enter_async_context(
                    stdio_client_factory(resolved_cmd)
                )
            except Exception as e:  # noqa: BLE001
                _log.warning(
                    "open-websearch stdio spawn failed (%r): %s", resolved_cmd, e
                )
                return results

            # stdio_client yields (read_stream, write_stream)
            try:
                read_stream, write_stream = transport
            except (TypeError, ValueError) as e:
                _log.warning("unexpected stdio transport shape: %s", e)
                return results

            try:
                session = await stack.enter_async_context(
                    session_factory(read_stream, write_stream)
                )
                await session.initialize()
            except Exception as e:  # noqa: BLE001
                _log.warning("MCP session init failed: %s", e)
                return results

            async def _one(q: str) -> None:
                async with sem:
                    await debouncer.acquire(engines)
                    try:
                        call = session.call_tool(
                            _SEARCH_TOOL,
                            {
                                "query": q,
                                "limit": max_per_query,
                                "engines": list(engines),
                            },
                        )
                        res = await asyncio.wait_for(call, timeout=timeout_s)
                    except TimeoutError:
                        _log.warning(
                            "web search timed out after %.1fs: %r", timeout_s, q
                        )
                        return
                    except Exception as e:  # noqa: BLE001
                        _log.warning("web search errored on %r: %s", q, e)
                        return

                    # MCP 1.x: `isError` bubbles tool-side failures up.
                    if getattr(res, "isError", False):
                        _log.warning("web search returned isError for %r", q)
                        return
                    payload = _extract_results_json(
                        getattr(res, "content", None)
                    )
                    if payload is None:
                        return
                    results[q] = _payload_to_results(payload, q)

            await asyncio.gather(*[_one(q) for q in queries])
    except Exception as e:  # noqa: BLE001 — belt-and-suspenders
        _log.warning("open-websearch batch failed entirely: %s", e)

    return results


__all__ = ["WebResult", "batch_search_web"]
