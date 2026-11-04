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

## D-017: Nova's skill fields live under `metadata:`

The prompt lists `source_id`, `schema_fingerprint` and others as frontmatter fields, and
also says to follow the Deep Agents convention. Deep Agents ignores unknown top-level
keys and keeps `metadata` as strings, so that's where they go.

## D-018: Join skills will be named `join-<a>-<b>`

The prompt's `/skills/_joins/<a>__<b>/` breaks the skill naming rules, which allow only
lowercase letters, digits and single hyphens, and require the name to match its
directory. They'll live at `/skills/_joins/join-<a>-<b>/`, and `_joins` is passed as its
own skills source because Deep Agents only looks one level deep. To be built in Phase 8.

## D-019: The profiler measures; the model interprets

Everything in `SourceProfile` is measured, with no LLM involved, so the subagent's
conclusions rest on real numbers, and a skill can be audited against them. Declared keys
use Trino's passthrough through `SqlExecutor.run_internal`, which skips the guard. That
is only safe because its text is fixed and server-written, so no agent tool may call it.

## D-020: Personal data never becomes a top value

Columns named like emails, names and phone numbers never have their values stored in a
skill, even at low cardinality. Skills are shown to the model and shared across an org.

## D-021: Install from a lockfile, with uv

`pip` searched for over seven minutes on an unsatisfiable pin rather than failing, where
`uv` reported the conflict in half a second. The image now installs a generated
`requirements.lock`, which also makes builds reproducible.

## D-022: The LLM provider is configuration; testing uses OpenAI

The prompt specifies Claude, which stays the default in `.env.example`. Testing uses
`MODEL_PROVIDER=openai` and `MODEL_NAME=gpt-6-luna`, at the owner's request. Nothing
outside `agent/llm.py` knows which provider is in use, and API keys aren't in `Settings`,
so they can't reach a prompt or a log. The Claude path is written but not yet run live.

## D-023: The profiler finishes by calling a tool

`submit_skill` is an ordinary tool. A malformed submission becomes feedback rather than an
exception that loses the run, verification happens inside it so repairs stay in the same
conversation, and it needs no forced `tool_choice`, which Claude Opus 5.5 rejects.

## D-024: The harness decides what is true in a skill

The model proposes, but only metrics and examples the harness ran itself reach the skill.
Text the model wrote can still echo hostile source content, so a generated skill stays
`draft` until reviewed, and the rules that matter are enforced in code, which no skill
text can change.

## D-025: `make test` makes no paid calls

Tests that call the real model are marked `llm` and run with `make test-llm`, so the normal
suite is free and works with no key. Evals in Phase 9 need the same decision.

## D-026: Agents finish through a tool the harness controls

`finish_dashboard` runs every chart itself and refuses a dashboard with a failing chart, or
with charts covering different periods. The model can't declare success, and can't skip
the check. As with `submit_skill`, failures go back as feedback a limited number of times,
and what still fails is reported to the user.

## D-027: Date windows are decided in code, and models are told the date

Models don't know today's date. In one run the verifier assumed an earlier month,
"disproved" a correct dashboard with a confident calculation, and the repair broke it.
So every prompt states the date, `superset_get_chart_data` reports the exact window Superset
applied, and the verifier checks that window rather than working one out. Trends end at
the last complete period in `ChartSpec`, not by instruction (D-013).

## D-028: The guest token never passes through the model

The prompt lists `superset_guest_token` as a tool, but a token in the conversation would be
a secret in agent context and in the checkpoints. The tool only prepares embedding. The
run boundary creates the token and returns it to the caller.

## D-029: Shared build state comes from Superset

Subagents start empty, and given no way to see what existed they created seven duplicate
datasets during one repair. `superset_build_status` reads Superset directly, so it also
works after a resume or on a follow-up. A chart's key is kept in its `description`, because
Superset has nowhere else to put it. Datasets and charts per dashboard are capped, and
finishing removes anything unused, touching only objects with this dashboard's prefix.

## D-030: The profiler runs as a job, not a `task` subagent

Sources are profiled when connected, in the background, when no main agent exists. So it's
a deep agent of its own with only the tools it needs, started by the API. By the time a
dashboard is requested the skill is there, which is what lets a second dashboard reuse it.

## D-031: `load_skill` sits alongside Deep Agents' skills

`SkillsMiddleware` gives progressive disclosure: only names and descriptions stay in context.
The spec also requires a freshness check before a skill is used, which reading a file can't
do. So agents load through `load_skill`, which checks freshness and refreshes if needed.

## D-032: One conversation is one dashboard

A dashboard's key is a hash of its thread id. Retries, resumes and follow-ups therefore all
reach the same objects without any lookup table.

## D-033: The verifier reports problems and notes separately

An earlier run spent a repair round on an axis label. Only something that makes a number
wrong, or stops a chart being read, is worth another round. The label itself was fixed in
code: each time grain now has an explicit format.
