# Versions

Exact versions Nova is pinned to, and the API details checked against them. All
checked on 2026-11-04 by reading the installed package source, the image, or the
registry, rather than from memory.

## Images

| Component | Version | Notes |
|---|---|---|
| Apache Superset | `apache/superset:6.1.0` | Latest stable. 7.0.0 is at rc2. Extended by `infra/superset/Dockerfile`. |
| Trino | `trinodb/trino:484` | Java 25. |
| Postgres | `postgres:17.11-alpine` | Metadata and demo source. |
| Redis | `redis:8.8.4-alpine` | |
| MySQL | `mysql:8.4.13` | LTS line; demo source. |
| Python (tools) | `python:3.13-slim` | Seed job and test runner. |

## Python

| Package | Version | Used by |
|---|---|---|
| psycopg2-binary | 2.9.13 | Superset |
| pymysql | 1.2.3 | Superset, tools |
| trino | 0.340.0 | Superset, tools, agent |
| httpx | 0.28.1 | tools |
| psycopg[binary] | 3.3.8 | tools, agent |
| pytest | 9.2.0 | tools |
| redis | 8.1.0 | tools |

| deepagents | 0.7.32 | agent |
| fastapi | 0.162.0 | agent |
| langchain | 1.4.8 | agent |
| langchain-anthropic | 1.7.9 | agent |
| langchain-openai | 1.7.3 | agent |
| langgraph | 1.2.19 | agent |
| langgraph-checkpoint-postgres | 3.2.0 | agent |
| playwright | 1.64.1 | agent |
| prison | 0.2.1 | agent |
| sse-starlette | 3.6.0 | agent |
| uvicorn | 0.54.0 | agent |
| psycopg-pool | 3.3.3 | agent |
| python-json-logger | 4.2.0 | agent |
| sqlglot | 30.29.0 | agent |
| pydantic | 2.14.2 | agent |
| pydantic-settings | 2.16.2 | agent |
| pyyaml | 6.0.3 | agent |
| tenacity | 9.1.4 | agent. `deepagents` pulls in `google-genai`, which needs `<9.2.0`. |

`agent/requirements.txt` holds the direct pins, and `agent/requirements.lock` the full
resolved tree that the image installs. Run `make lock` after changing the first.

Verified for later phases, and pinned when they start:

| Package | Version |
|---|---|
| cryptography | 50.0.2 |
| langsmith | 0.14.17 |

## JavaScript (pinned in `ui/package.json`)

| Package | Version |
|---|---|
| next | 16.4.5 |
| react | 19.3.1 |
| tailwindcss | 4.3.4 |
| typescript | 7.0.2 |
| @superset-ui/embedded-sdk | 0.4.0 |

## API details that shaped the design

**Superset 6.1.0**
- The image contains no database drivers, so `psycopg2`, `trino` and a MySQL driver
  are added in a derived image.
- It runs with `SUPERSET_ENV=production`, which enables the rate limiter, so it
  needs a shared store (Redis db 5).
- It inherits a `HEALTHCHECK` that curls port 8088. Celery workers must replace it.
- `GUEST_ROLE_NAME` defaults to `Public`, and `GUEST_TOKEN_JWT_SECRET` defaults to a
  published value. Both are overridden.
- Talisman's default `X-Frame-Options: SAMEORIGIN` blocks embedding. Nova sets
  `frame_options: None` and uses CSP `frame-ancestors`.
- `/embedded/<uuid>` returns 403 when the `Referer` isn't in the dashboard's
  `allowed_domains`.
- Guest tokens come from `POST /api/v1/security/guest_token/` and need
  `can_grant_guest_token`.
- `POST` on charts, datasets and dashboards accepts a client-supplied `uuid`, and
  charts and datasets can be searched by it. Nova's idempotency is built on this.
- `PUT /api/v1/dashboard/{id}` with `positions` inside `json_metadata` rebuilds the
  dashboard's chart list from the layout, and fills in each chart's UUID. Writing
  `position_json` alone doesn't link charts.
- Chart data reports the window it applied, in `from_dttm` and `to_dttm` as epoch milliseconds.
- Line chart data can come back newest first.
- `DATETRUNC(DATETIME('today'), month)` and `DATEADD(..., -12, month)` work in time ranges, for
  every grain.
- A chart's data endpoint (`GET /api/v1/chart/{id}/data/`) only works if the chart
  has a saved `query_context`, so Nova always stores one.
- Creating a virtual dataset runs its SQL to read the columns. Bad SQL returns a
  bare 500, and nothing is saved.
- `POST /api/v1/sqllab/execute/` rejects a `json` field. An async response carries
  the numeric id in `query.queryId`; `query.id` is the client id.

**Trino 484**
- `CREATE CATALOG` logs the full statement, including any password, and shows it in
  the Web UI.
- JDBC connectors accept `credential-provider.type=FILE` with
  `connection-credential-file`, which keeps the password out of the statement.
- The file-based access-control plugin refuses `CREATE CATALOG` for every user,
  even with `allow: all`.
- `TABLE(catalog.system.query(query => '...'))` sends raw SQL straight to the source.
- `EXPLAIN (TYPE IO)` reports only the final output, so a `count(*)` over a cross join
  estimates one row. `EXPLAIN (TYPE LOGICAL, FORMAT JSON)` has estimates per step.
- A bare `COUNT(*)` over a cross join returns almost instantly, because Trino doesn't
  need to touch each row.
- A coordinator can't shut down gracefully: `PUT /v1/info/state` fails with `Cannot
  shutdown coordinator`.

**sqlglot 30.29**
- The `system.query` table function parses as a `Select` over a table whose name is
  empty, so reading table names alone misses it.
- `a.b.c.d` parses with `c` inside a `Dot`, and `table.name` reports `d`.
- `EXPLAIN ANALYZE`, `CALL`, `SHOW` and `PREPARE` fall back to a `Command` node, with a
  warning rather than an error.

**Trino sampling**
- `LIMIT n` takes the oldest rows, so on the demo orders it reports data ending in
  January 2025. `TABLESAMPLE BERNOULLI` is fair, but reads the whole table.
- `approx_distinct` is off by about 2%, which is enough to make a unique column look
  like it has duplicates.
- `information_schema` has no constraints or comments for JDBC catalogs. They have to
  be read from the source, through `system.query`.

**Deep Agents 0.7.32**
- Skill names may only contain lowercase alphanumerics and single hyphens, and must
  match the directory name. A bad name logs a warning but still loads.
- Only `name`, `description`, `license`, `compatibility`, `metadata` and
  `allowed-tools` are read from frontmatter. Nova's own fields (`source_id`,
  `schema_fingerprint`, ...) must go under `metadata:`, whose values become strings.
- `skills=` takes directories of skill directories and looks one level deep, so
  `/skills/_joins/` must be passed as a separate source.
- Descriptions over 1,024 characters are truncated.

**LangGraph 1.2 and Deep Agents checkpoints**
- Subagents checkpoint under their own namespace, so a raw `saver.list()` returns their state
  mixed with the main thread's.
- The root checkpoint doesn't hold `messages` in `channel_values`. Read a conversation with
  `agent.get_state(config)`.
- Subagents get the same `files` state as the main agent, but not its `messages` or `todos`.

**langchain-openai 1.7.3 with `gpt-6-luna`** (tested live)
- Needs the Responses API (`use_responses_api=True`).
- `response_format` raises on a missing field and loses the run, which is one reason the
  profiler submits through a tool (D-023).
- `usage_metadata` is populated, so budgets count real tokens.

**langchain-anthropic 1.7.9 with `claude-opus-5-5`** (from source, not tested live)
- `temperature` and `thinking: disabled` are rejected. Lower effort with
  `reasoning_effort` instead.
- Forced `tool_choice` returns a 400, so structured output must use
  `method="json_schema"` or rely on the library's guard for this model.

**@superset-ui/embedded-sdk 0.4.0**
- `embedDashboard({ id, supersetDomain, mountPoint, fetchGuestToken, ... })`, and
  `setThemeMode('dark' | 'default' | 'system')` on the returned object.
- With `dashboardUiConfig.emitDataMasks: true`, `observeDataMask` reports filter selections to
  the host page, keyed by native filter id.
