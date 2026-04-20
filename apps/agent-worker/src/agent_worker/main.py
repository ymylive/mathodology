"""Worker entrypoint: consume `mm:jobs` via XREADGROUP, run the pipeline."""

from __future__ import annotations

import asyncio
import contextlib
import signal
import socket
from typing import Any
from uuid import UUID

import orjson
from mm_contracts import ProblemInput
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from agent_worker.config import Settings, get_settings
from agent_worker.events import EventEmitter
from agent_worker.logging import configure_logging, get_logger
from agent_worker.pipeline import run_pipeline

JOBS_STREAM = "mm:jobs"
CONSUMER_GROUP = "mm-workers"
BLOCK_MS = 5000

log = get_logger("agent_worker")


def _consumer_name() -> str:
    """Short hostname, used as the XREADGROUP consumer name."""
    return socket.gethostname().split(".")[0] or "worker"


async def _ensure_group(redis: Redis) -> None:
    """Create the consumer group if it doesn't already exist."""
    try:
        await redis.xgroup_create(
            name=JOBS_STREAM,
            groupname=CONSUMER_GROUP,
            id="$",
            mkstream=True,
        )
        log.info("consumer_group_created", stream=JOBS_STREAM, group=CONSUMER_GROUP)
    except ResponseError as e:
        if "BUSYGROUP" in str(e):
            log.debug("consumer_group_exists", stream=JOBS_STREAM, group=CONSUMER_GROUP)
        else:
            raise


def _decode(value: Any) -> str:
    """Redis responses may come back as bytes or str depending on decode_responses."""
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _parse_entry(fields: dict[Any, Any]) -> tuple[UUID, ProblemInput]:
    """Parse one stream entry's fields into (run_id, ProblemInput)."""
    decoded = {_decode(k): _decode(v) for k, v in fields.items()}
    run_id = UUID(decoded["run_id"])
    payload_raw = decoded["payload"]
    payload_obj = orjson.loads(payload_raw)
    problem = ProblemInput.model_validate(payload_obj)
    return run_id, problem


async def _process(
    redis: Redis,
    entry_id: str,
    fields: dict[Any, Any],
    semaphore: asyncio.Semaphore,
) -> None:
    """Process one job entry: run the pipeline, emit error on failure, XACK."""
    async with semaphore:
        run_id: UUID | None = None
        try:
            run_id, problem = _parse_entry(fields)
            log.info("job_started", run_id=str(run_id), entry_id=entry_id)
            await run_pipeline(redis, run_id, problem)
            log.info("job_completed", run_id=str(run_id), entry_id=entry_id)
        except Exception as exc:  # noqa: BLE001 — we want to catch everything here
            log.exception("job_failed", entry_id=entry_id, error=str(exc))
            if run_id is not None:
                try:
                    emitter = EventEmitter(redis, run_id)
                    await emitter.emit(
                        "error",
                        {"message": str(exc), "code": type(exc).__name__},
                        agent=None,
                    )
                except Exception:  # noqa: BLE001
                    log.exception("error_emit_failed", run_id=str(run_id))
        finally:
            try:
                await redis.xack(JOBS_STREAM, CONSUMER_GROUP, entry_id)
            except Exception:  # noqa: BLE001
                log.exception("xack_failed", entry_id=entry_id)


async def _consume_loop(
    redis: Redis,
    consumer: str,
    stop: asyncio.Event,
    semaphore: asyncio.Semaphore,
    in_flight: set[asyncio.Task[None]],
) -> None:
    """Main XREADGROUP loop. Runs until `stop` is set."""
    while not stop.is_set():
        try:
            resp = await redis.xreadgroup(
                groupname=CONSUMER_GROUP,
                consumername=consumer,
                streams={JOBS_STREAM: ">"},
                count=16,
                block=BLOCK_MS,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("xreadgroup_failed")
            await asyncio.sleep(1.0)
            continue

        if not resp:
            continue

        for _stream, entries in resp:
            for entry_id, fields in entries:
                task = asyncio.create_task(
                    _process(redis, _decode(entry_id), fields, semaphore)
                )
                in_flight.add(task)
                task.add_done_callback(in_flight.discard)


def _install_signal_handlers(stop: asyncio.Event) -> None:
    """Wire SIGINT/SIGTERM to set the stop event."""
    loop = asyncio.get_running_loop()

    def _handle(signame: str) -> None:
        log.info("signal_received", signal=signame)
        stop.set()

    for sig, name in ((signal.SIGINT, "SIGINT"), (signal.SIGTERM, "SIGTERM")):
        # Windows / some restricted envs don't support this — fall back silently.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _handle, name)


async def run(settings: Settings | None = None) -> None:
    """Start the worker. Blocks until shutdown."""
    configure_logging()
    cfg = settings or get_settings()
    consumer = _consumer_name()

    log.info(
        "worker_starting",
        redis_url=cfg.redis_url,
        consumer=consumer,
        concurrency=cfg.worker_concurrency,
    )

    redis = Redis.from_url(cfg.redis_url, decode_responses=False)
    await _ensure_group(redis)

    stop = asyncio.Event()
    _install_signal_handlers(stop)

    semaphore = asyncio.Semaphore(cfg.worker_concurrency)
    in_flight: set[asyncio.Task[None]] = set()
    consumer_task = asyncio.create_task(
        _consume_loop(redis, consumer, stop, semaphore, in_flight)
    )

    await stop.wait()
    log.info("worker_draining", in_flight=len(in_flight))

    consumer_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await consumer_task

    if in_flight:
        await asyncio.gather(*in_flight, return_exceptions=True)

    await redis.aclose()
    log.info("worker_stopped")
