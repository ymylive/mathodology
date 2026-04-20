# Contracts

Single source of truth for cross-language types.

- `openapi.yaml` — REST + LLM passthrough surface for the Rust gateway.
- `events.schema.json` — envelope for every event on `mm:events:<run_id>` Redis stream and its WS forwarding.

Run `just gen` after editing to regenerate Python (Pydantic) and TypeScript types.

## Conventions

- **Seq** is monotonic per run, gateway-assigned. Clients use `last_seq` on WS reconnect for replay.
- **Kind** values are kept narrow and extended via versioned additions, not renames.
- Event payload shapes are defined in `events.schema.json#/$defs`; pick by `kind`.
