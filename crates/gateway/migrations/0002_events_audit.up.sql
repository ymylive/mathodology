CREATE TABLE events_audit (
  run_id UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  seq BIGINT NOT NULL,
  ts TIMESTAMPTZ NOT NULL,
  agent TEXT,
  kind TEXT NOT NULL,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY (run_id, seq)
);

CREATE INDEX events_audit_run_seq_idx ON events_audit (run_id, seq);
