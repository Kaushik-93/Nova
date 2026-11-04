-- Nova's application tables. Applied at startup; every statement is idempotent.

CREATE TABLE IF NOT EXISTS audit_log (
    id          bigserial PRIMARY KEY,
    occurred_at timestamptz NOT NULL DEFAULT now(),
    run_id      text NOT NULL,
    user_id     text NOT NULL,
    kind        text NOT NULL CHECK (kind IN ('sql', 'superset_write')),
    status      text NOT NULL CHECK (status IN ('ok', 'rejected', 'failed')),
    target      text NOT NULL DEFAULT '',
    statement   text NOT NULL DEFAULT '',
    rows        bigint,
    elapsed_ms  integer,
    detail      jsonb NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS audit_log_run_idx ON audit_log (run_id, id);
