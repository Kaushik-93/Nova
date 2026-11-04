# Decisions

Choices the build prompt left open, most recent last.

## D-001: Host ports in the 1xxxx range, bound to localhost

Ports 3000, 5432, 8000 and 8080 are commonly in use, so every published port is in
the 1xxxx range, set in `.env`, and bound to `127.0.0.1`. Containers talk to each
other on their normal ports.

## D-002: Run a Celery worker, but no beat

The stack table lists Redis for "cache/async queries", so async SQL Lab is real: one
worker with concurrency 2. Celery beat only serves reports and alerts, which Nova
doesn't use, so it's left out to save memory.

## D-003: Add sources with a credentials file and `CREATE CATALOG`

Connecting a source has to add a Trino catalog without leaking its password, and
`CREATE CATALOG` logs its full statement.

Nova writes the credentials to a file on the `trino-data` volume, then runs
`CREATE CATALOG` with `credential-provider.type=FILE`, so the statement holds only a
path. There's no restart, and the catalog survives one. `test_stack.py` covers this.

Rejected:
- **Password in `CREATE CATALOG`:** breaks "no secrets in logs".
- **Writing a catalog file and restarting Trino:** a coordinator can't shut down
  gracefully, it would need Docker socket access, and it would interrupt running
  queries.

`catalog.management=dynamic` is marked experimental by Trino, so its syntax could
change on upgrade. This is why the version is pinned.

## D-004: No access rules in Trino itself

Making catalogs read-only in Trino would be a useful second layer, but the
file-based plugin refuses `CREATE CATALOG` for every user, and D-003 needs that.

So read-only is enforced by the source: each catalog connects as a user who can only
read, as the spec requires. The consequence is that anything able to reach Trino can
add or drop a catalog. Trino is only reachable from the Compose network and
`127.0.0.1`, and Superset's connection has DML disabled. Put authentication in front
of Trino before deploying anywhere shared.

## D-005: Guests use the Gamma role

`GUEST_ROLE_NAME` defaults to `Public`. Gamma has no datasource access of its own, so
a guest reaches only what the token names. `test_stack.py` checks that a token for
one dashboard can't open another. Phase 6 builds on this for per-user mapping.

## D-006: Demo data is a deterministic function of the date

Each day's rows are generated from that day's own RNG. Closed periods therefore never
change, so evals can assert exact numbers, while "the last 12 months" has data
whenever the stack is seeded. See `infra/seed/README.md`.

## D-007: Demo catalogs are installed at boot

`demo_shop` and `demo_marketing` are static Trino catalogs rather than being created
when a user connects the source. Phase 1 needs them to prove the stack works. The
connect flow in Phase 6 reuses a catalog that already points at the same source.

## D-008: Tests and seeding run in a container

`make test` runs in the `tools` image on the Compose network, so a fresh machine
needs only Docker, make and openssl, with no local Python.

## D-009: PyMySQL rather than mysqlclient for Superset

It's pure Python, so the derived image needs no compiler or client libraries.
Connection URIs use `mysql+pymysql://`.

## D-010: Identify Superset objects by deterministic UUID

The prompt suggested tags or a naming convention. Superset 6.1.0 accepts a
client-supplied `uuid`, so each chart and dataset gets a UUIDv5 of its key, and each
dashboard a stable slug. `ensure_*` looks the object up first. A retry after a
timed-out POST that actually succeeded therefore finds that object, without touching
titles the user might edit.

## D-011: A plain 500 from Superset is not retried

Only 429, 502, 503 and 504 are transient. Superset's 500s are deterministic (bad
dataset SQL returns one), so retrying only wastes time and hides the cause.

## D-012: A synchronous Superset client

Tools run in LangGraph's thread pool and FastAPI runs sync endpoints in its own, so a
sync `httpx.Client` works everywhere, with no async plumbing to carry through.

## D-013: Charts must not treat a partial period as a real one

Rendering a dashboard showed "-85.9% vs previous" and every line collapsing at the
right edge. Nothing was wrong except that the current month was three days old. The
chart designer has to end time series at the last complete period, and the verifier
has to catch a final point that falls off sharply. To be built in Phase 5.

## D-014: All agent SQL runs through Trino

Trino already holds each source's read-only credentials, so the agent only ever names
a catalog and has no connection string to see or leak. It also means one SQL dialect,
one place to enforce timeouts, and the same engine Superset uses, so a query that works
for the agent works in a chart. `to_trino` translates a source's dialect when needed.

## D-015: The guard is an allowlist that fails closed

A denylist can only catch attacks someone thought of. A query passes only if it is one
SELECT over fully qualified tables in connected catalogs, and anything the parser can't
fully account for is refused, including queries that would have been fine. The read-only
users remain the layer underneath.

## D-016: The cost check reads the largest step of the plan

See VERSIONS.md for why the final output is useless. Where Trino has no statistics the
estimate is skipped rather than refused, and the timeout covers that case. A join pushed
down to a single source also reports little, and the source's own `statement_timeout`
bounds it.
