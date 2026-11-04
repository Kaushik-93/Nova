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

CREATE TABLE IF NOT EXISTS sources (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug          text NOT NULL UNIQUE,
    display_name  text NOT NULL,
    trino_catalog text NOT NULL UNIQUE,
    dialect       text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS skills (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug               text NOT NULL UNIQUE,
    source_id          uuid REFERENCES sources ON DELETE SET NULL,
    kind               text NOT NULL DEFAULT 'source' CHECK (kind IN ('source', 'join')),
    version            integer NOT NULL,
    status             text NOT NULL CHECK (status IN ('draft', 'reviewed')),
    schema_fingerprint text NOT NULL,
    path               text NOT NULL,
    generated_at       timestamptz NOT NULL,
    reviewed_by        text,
    updated_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS dashboards (
    slug        text PRIMARY KEY,
    thread_id   text NOT NULL UNIQUE,
    superset_id integer NOT NULL,
    embed_uuid  text,
    title       text NOT NULL,
    summary     text NOT NULL DEFAULT '',
    created_by  text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
