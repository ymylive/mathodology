CREATE TABLE cost_ledger (
  id BIGSERIAL PRIMARY KEY,
  run_id UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  ts TIMESTAMPTZ NOT NULL DEFAULT now(),
  agent TEXT,
  model TEXT NOT NULL,
  prompt_tokens INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  cost_rmb NUMERIC(12,6) NOT NULL DEFAULT 0,
  cache_hit BOOLEAN NOT NULL DEFAULT false
);

CREATE INDEX cost_ledger_run_ts_idx ON cost_ledger (run_id, ts);
