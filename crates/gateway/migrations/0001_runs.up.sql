CREATE TYPE run_status AS ENUM ('queued','running','done','failed','cancelled');

CREATE TABLE runs (
  id UUID PRIMARY KEY,
  status run_status NOT NULL DEFAULT 'queued',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  problem_text TEXT NOT NULL,
  competition_type TEXT NOT NULL DEFAULT 'other',
  cost_rmb NUMERIC(10,4) NOT NULL DEFAULT 0,
  notebook_path TEXT,
  paper_path TEXT
);

CREATE INDEX runs_created_at_idx ON runs (created_at DESC);
