-- Nova's application tables. Applied at startup; every statement is idempotent.

CREATE TABLE IF NOT EXISTS audit_log (
    id          bigserial PRIMARY KEY,
    occurred_at timestamptz NOT NULL DEFAULT now(),
    run_id      text NOT NULL,
    user_id     text NOT NULL,
    kind        text NOT NULL CHECK (kind IN ('sql', 'semantic_query', 'superset_write')),
    status      text NOT NULL CHECK (status IN ('ok', 'rejected', 'failed')),
    target      text NOT NULL DEFAULT '',
    statement   text NOT NULL DEFAULT '',
    rows        bigint,
    elapsed_ms  integer,
    detail      jsonb NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS audit_log_run_idx ON audit_log (run_id, id);

-- Runs query Cube rather than writing to Superset. Rows from before keep their superset_write kind.
ALTER TABLE audit_log DROP CONSTRAINT IF EXISTS audit_log_kind_check;
ALTER TABLE audit_log ADD CONSTRAINT audit_log_kind_check CHECK (kind IN ('sql', 'semantic_query', 'superset_write'));

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
    title       text NOT NULL,
    summary     text NOT NULL DEFAULT '',
    created_by  text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS runs (
    run_id      text PRIMARY KEY,
    thread_id   text NOT NULL,
    prompt      text NOT NULL DEFAULT '',
    status      text NOT NULL,
    created_by  text NOT NULL,
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    result      jsonb NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS runs_thread_idx ON runs (thread_id, started_at);

CREATE TABLE IF NOT EXISTS run_events (
    id     bigserial PRIMARY KEY,
    run_id text NOT NULL REFERENCES runs ON DELETE CASCADE,
    at     timestamptz NOT NULL DEFAULT now(),
    event  jsonb NOT NULL
);

CREATE INDEX IF NOT EXISTS run_events_run_idx ON run_events (run_id, id);

CREATE TABLE IF NOT EXISTS users (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email         text NOT NULL UNIQUE,
    display_name  text NOT NULL DEFAULT '',
    password_hash text,
    role          text NOT NULL CHECK (role IN ('admin', 'analyst', 'viewer')),
    sso_subject   text UNIQUE,
    is_active     boolean NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_login_at timestamptz
);

-- What a non-admin may read. No row means no access. tenant_ids NULL means the whole source.
CREATE TABLE IF NOT EXISTS source_access (
    user_id    uuid NOT NULL REFERENCES users ON DELETE CASCADE,
    source_id  uuid NOT NULL REFERENCES sources ON DELETE CASCADE,
    tenant_ids text[],
    PRIMARY KEY (user_id, source_id)
);

ALTER TABLE sources ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'postgres';
ALTER TABLE sources ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'ready';
ALTER TABLE sources ADD COLUMN IF NOT EXISTS read_only boolean;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS encrypted_credentials text;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS tenant_column text;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS tenant_table text;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS tenant_label_column text;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS shared_tables text[] NOT NULL DEFAULT '{}';

UPDATE sources SET kind = 'mysql' WHERE dialect = 'mysql' AND kind = 'postgres';

ALTER TABLE dashboards ADD COLUMN IF NOT EXISTS catalogs text[] NOT NULL DEFAULT '{}';

ALTER TABLE runs ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'dashboard';
ALTER TABLE runs ADD COLUMN IF NOT EXISTS dashboard_key text;

-- A project holds sheets, and each sheet is one row in dashboards.
CREATE TABLE IF NOT EXISTS projects (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug        text NOT NULL UNIQUE,
    name        text NOT NULL,
    description text NOT NULL DEFAULT '',
    created_by  text NOT NULL,
    shared      boolean NOT NULL DEFAULT false,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE dashboards ADD COLUMN IF NOT EXISTS project_id uuid REFERENCES projects ON DELETE RESTRICT;
ALTER TABLE dashboards ADD COLUMN IF NOT EXISTS sheet_name text;
ALTER TABLE dashboards ADD COLUMN IF NOT EXISTS position integer NOT NULL DEFAULT 0;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS project_id uuid REFERENCES projects ON DELETE SET NULL;

-- Sharing used to be on each dashboard, and is now on a project, so shared and unshared dashboards
-- must not end up together: everything in a shared project is shared. Each owner gets one project
-- for each, which leaves everyone seeing exactly what they saw before.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema()
               AND table_name = 'dashboards' AND column_name = 'shared') THEN
        INSERT INTO projects (slug, name, created_by, shared)
        SELECT DISTINCT 'earlier-' || substr(md5(created_by), 1, 8) || CASE WHEN shared THEN '-shared' ELSE '' END,
               CASE WHEN shared THEN 'Shared dashboards' ELSE 'Earlier dashboards' END, created_by, shared
        FROM dashboards
        ON CONFLICT (slug) DO NOTHING;

        UPDATE dashboards d SET project_id = p.id, sheet_name = d.title, position = r.place
        FROM projects p, (SELECT slug, row_number() OVER (PARTITION BY created_by, shared ORDER BY created_at) - 1 AS place
                          FROM dashboards) r
        WHERE r.slug = d.slug
          AND p.slug = 'earlier-' || substr(md5(d.created_by), 1, 8) || CASE WHEN d.shared THEN '-shared' ELSE '' END;

        ALTER TABLE dashboards DROP COLUMN shared;
    END IF;
END $$;

-- Nova draws dashboards itself now. A dashboard is a spec, and each chart's numbers come from Cube
-- for whoever opens it. Sheets built in Superset before have no spec, and are rebuilt from their prompt.
ALTER TABLE dashboards ADD COLUMN IF NOT EXISTS spec jsonb;
ALTER TABLE dashboards DROP COLUMN IF EXISTS superset_id;
ALTER TABLE dashboards DROP COLUMN IF EXISTS embed_uuid;
ALTER TABLE sources DROP COLUMN IF EXISTS superset_database_id;
