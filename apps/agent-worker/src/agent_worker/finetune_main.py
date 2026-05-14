"""Finetune consumer: parallel to the main pipeline consumer in main.py.

Reads from `mm:finetune` (separate Redis Stream from `mm:jobs`) and
dispatches each entry to `run_finetune()`, which loads the run's existing
paper/notebook and invokes `PaperEditorAgent`.

The two streams are kept separate so:
- Worker concurrency budget for full pipelines isn't starved by chat-style
  fine-tunes (which can land in bursts).
- The consumer group can have different ack / retry / DLQ semantics later.
"""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path
from typing import Any
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from agent_worker.config import Settings
from agent_worker.events import EventEmitter
from agent_worker.gateway_client import GatewayClient
from agent_worker.kernel import KernelSession
from agent_worker.logging import get_logger
from agent_worker.matlab import MatlabSession

FINETUNE_STREAM = "mm:finetune"
FINETUNE_GROUP = "mm-finetune-workers"
BLOCK_MS = 5000

log = get_logger("agent_worker.finetune")


def _consumer_name() -> str:
    """Hostname-based consumer id (matches main.py convention)."""
    return (socket.gethostname().split(".")[0] or "worker") + ":ft"


def _decode(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


async def _ensure_group(redis: Redis) -> None:
    """Create the consumer group if missing (mkstream so the stream auto-creates)."""
    try:
        await redis.xgroup_create(
            name=FINETUNE_STREAM,
            groupname=FINETUNE_GROUP,
            id="$",
            mkstream=True,
        )
        log.info("finetune_group_created", stream=FINETUNE_STREAM, group=FINETUNE_GROUP)
    except ResponseError as e:
        if "BUSYGROUP" in str(e):
            log.debug("finetune_group_exists", stream=FINETUNE_STREAM, group=FINETUNE_GROUP)
        else:
            raise


def _parse_entry(
    fields: dict[Any, Any],
) -> tuple[UUID, UUID, str]:
    """Decode (run_id, session_id, message) from a finetune stream entry."""
    decoded = {_decode(k): _decode(v) for k, v in fields.items()}
    return (
        UUID(decoded["run_id"]),
        UUID(decoded["session_id"]),
        decoded["message"],
    )


async def run_finetune(
    redis: Redis,
    cfg: Settings,
    run_id: UUID,
    session_id: UUID,
    user_message: str,
) -> None:
    """Drive one finetune turn against an existing run.

    Imports PaperEditorAgent lazily so this module loads even if the agent
    isn't built yet (e.g. during the parallel-agent dispatch window).
    """
    from agent_worker.agents.paper_editor import PaperEditorAgent  # local import

    runs_dir = Path(cfg.runs_dir).resolve()  # noqa: ASYNC240 — stdlib asyncio, not trio
    run_dir = runs_dir / str(run_id)
    if not run_dir.is_dir():
        log.error("finetune_run_dir_missing", run_id=str(run_id), path=str(run_dir))
        return

    # Reuse the run's existing events.jsonl so finetune turns appear in
    # the same forensic timeline as the original pipeline.
    emitter = EventEmitter(redis, run_id, events_log_path=run_dir / "events.jsonl")
    gateway = GatewayClient(cfg.gateway_http, cfg.dev_auth_token)
    kernel = KernelSession(run_id, runs_dir)
    matlab_session = MatlabSession(run_id, runs_dir)

    try:
        await emitter.emit(
            "finetune.session.start",
            {
                "session_id": str(session_id),
                "user_message": user_message[:1000],
            },
            agent="paper_editor",
        )

        agent = PaperEditorAgent(
            gateway=gateway,
            emitter=emitter,
            kernel=kernel,
            matlab_session=matlab_session,
            run_dir=run_dir,
        )

        summary = await agent.fine_tune(
            user_message=user_message,
            run_dir=run_dir,
            session_id=session_id,
        )

        await emitter.emit(
            "finetune.session.done",
            {"session_id": str(session_id), "summary": summary},
            agent="paper_editor",
        )
    except Exception as exc:  # noqa: BLE001 — never let one job crash the worker
        log.exception("finetune_failed", run_id=str(run_id), error=str(exc))
        try:
            await emitter.emit(
                "finetune.session.error",
                {
                    "session_id": str(session_id),
                    "message": str(exc),
                    "code": type(exc).__name__,
                },
                agent="paper_editor",
            )
        except Exception:  # noqa: BLE001
            log.exception("finetune_error_emit_failed", run_id=str(run_id))
    finally:
        await gateway.close()


async def _process(
    redis: Redis,
    cfg: Settings,
    entry_id: str,
    fields: dict[Any, Any],
    semaphore: asyncio.Semaphore,
) -> None:
    async with semaphore:
        try:
            run_id, session_id, message = _parse_entry(fields)
            log.info(
                "finetune_started",
                run_id=str(run_id),
                session_id=str(session_id),
                entry_id=entry_id,
            )
            await run_finetune(redis, cfg, run_id, session_id, message)
            log.info("finetune_completed", run_id=str(run_id), entry_id=entry_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("finetune_process_failed", entry_id=entry_id, error=str(exc))
        finally:
            try:
                await redis.xack(FINETUNE_STREAM, FINETUNE_GROUP, entry_id)
            except Exception:  # noqa: BLE001
                log.exception("finetune_xack_failed", entry_id=entry_id)


async def consume_loop(
    redis: Redis,
    cfg: Settings,
    consumer: str,
    stop: asyncio.Event,
    semaphore: asyncio.Semaphore,
    in_flight: set[asyncio.Task[None]],
) -> None:
    """Same XREADGROUP loop as main.py but against mm:finetune."""
    await _ensure_group(redis)
    while not stop.is_set():
        try:
            resp = await redis.xreadgroup(
                groupname=FINETUNE_GROUP,
                consumername=consumer,
                streams={FINETUNE_STREAM: ">"},
                count=8,
                block=BLOCK_MS,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("finetune_xreadgroup_failed")
            await asyncio.sleep(1.0)
            continue

        if not resp:
            continue
        for _stream, entries in resp:
            for entry_id, fields in entries:
                task = asyncio.create_task(
                    _process(redis, cfg, _decode(entry_id), fields, semaphore)
                )
                in_flight.add(task)
                task.add_done_callback(in_flight.discard)


__all__ = ["FINETUNE_STREAM", "FINETUNE_GROUP", "consume_loop", "run_finetune"]
