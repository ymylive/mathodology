"""Event emission helper: wraps XADD to `mm:events:<run_id>`."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import orjson
from mm_contracts import AgentEvent
from redis.asyncio import Redis

EVENTS_MAXLEN = 5000


class EventEmitter:
    """Per-run event emitter.

    Sequence numbers are drawn from the Redis counter `mm:seq:<run_id>` via
    INCR, which is the single source of truth shared with the Rust gateway's
    token/cost fan-out. The in-memory `_seq` attribute caches the last value
    we received for logging only.
    """

    def __init__(self, redis: Redis, run_id: UUID) -> None:
        self._redis = redis
        self._run_id = run_id
        self._stream_key = f"mm:events:{run_id}"
        self._seq_key = f"mm:seq:{run_id}"
        self._seq = 0

    async def emit(
        self,
        kind: str,
        payload: dict[str, Any] | None = None,
        agent: str | None = None,
    ) -> bytes | str:
        """Build an AgentEvent and XADD it to the run's stream.

        Returns the stream entry id assigned by Redis.
        """
        self._seq = await self._redis.incr(self._seq_key)
        event = AgentEvent(
            run_id=self._run_id,
            agent=agent,  # type: ignore[arg-type]
            kind=kind,  # type: ignore[arg-type]
            seq=self._seq,
            ts=datetime.now(UTC),
            payload=payload or {},
        )
        body = orjson.dumps(event.model_dump(mode="json")).decode("utf-8")
        return await self._redis.xadd(
            self._stream_key,
            {"payload": body},
            maxlen=EVENTS_MAXLEN,
            approximate=True,
        )
