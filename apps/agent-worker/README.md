# agent-worker

Python asyncio worker that consumes jobs from the `mm:jobs` Redis stream
(consumer group `mm-workers`) and emits events to `mm:events:<run_id>`.

## Run

From the repo root:

```bash
uv run python -m agent_worker
# or
uv run agent-worker
```

Requires a running Redis reachable at `REDIS_URL` (see `.env.example`).

## M1 scope

The pipeline is a three-event stub: `stage.start(analyzer)`,
`stage.start(modeler)`, `done`. No LLM calls, no Jupyter kernel, no retries.
