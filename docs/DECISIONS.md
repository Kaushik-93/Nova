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

`finish_dashboard` runs every chart itself and refuses a dashboard with a failing chart, a
filter that can't reach a chart, or charts on one dataset covering different periods. The model can't declare success, and can't skip
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

## D-034: A check too strict is worse than none

Requiring every chart to have a time window refused a count of today's fleet, which rightly
has none. To get past the check the agent hard-coded dates into its SQL, the one thing it had
been told never to do, and the dashboard would have gone stale. Charts are now compared only
with charts on the same dataset, and fixed dates in dataset SQL are refused. When a harness
rule is wrong, the agent will work around it, so each rule has to allow every legitimate case.

## D-035: Limits belong in code

"At most two repair rounds" in a prompt didn't hold: one run went nine rounds, mostly over
label overlap. `SubagentLimits` now refuses further delegation, and the agent is told to finish
and report what's open.

## D-036: A dashboard filter is checked for reach

Superset applies a dashboard filter by column name, and silently skips a chart whose dataset
lacks that column. That chart keeps showing every tenant under a filter saying otherwise, so
`finish_dashboard` refuses it.

A tenant filter is a convenience, not security: anyone can clear it. Where one tenant must not
see another's data, that takes row-level security in the guest token, which comes with auth.

## D-037: Run steps are stored, then streamed

Each step is written to Postgres and the stream reads from there, so a refresh or dropped
connection loses nothing, and a past run can be inspected later. It adds about half a second of
delay, which doesn't matter at this pace.

## D-038: The live view sees more than the logs

The JSON logs get timings and names only. Tool arguments and what the agents said can hold
data from sources, so those go only to the run's own events, for the person who started it.

## D-039: The UI tells the agent what's selected

Filter choices live inside the Superset iframe. With `emitDataMasks` the embedded SDK reports
them to the page, which shows them and adds them to the next message, because the agent can't
see inside the dashboard.

## D-040: Fixed dates are refused, and windows must end on a boundary

A chart designer copied "2025-11-01 through 2026-10-31" from the plan into every chart. The
prompt forbade it, and it cost twice over: Superset excludes the end, so 31 October vanished from
months labelled complete, and the dashboard would have gone stale. The verifier agreed with it,
because D-027 has it check the window the chart reports. So `ChartSpec` refuses dates unless
`fixed_dates` says the user asked for them, and `finish_dashboard` refuses a window ending part
way through its time grain, which is a check no agent's judgement is involved in.

## D-041: A dashboard streams in three stages

Three minutes of an empty panel feels longer than it is, and useful things exist long before the
run ends. So the plan draws a skeleton of every chart, each finished chart fills its slot, and the
real dashboard takes over as soon as it exists, marked "Checks running" until verification passes.

Previews are drawn by Nova, with ECharts: it's what Superset draws these charts with, so the handover
is close to invisible. Superset can't embed a single chart or update an open dashboard without a
reload, which is why the preview exists at all.

## D-042: Live events carry ids, not data

`chart_ready` names a chart, and the page fetches its rows from `/api/charts/{id}/preview`. Run events
are stored, so putting rows in them would quietly copy source data into Nova's own database, past
any row-level security added later.

## D-043: The dashboard is shown before it's verified, and says so

Waiting for verification would hide a working dashboard for over a minute. The trade-off is that
someone can see numbers a check later changes, so the header says "Checks running" until it passes,
and the embed reloads if a repair changed anything.

## D-044: One palette, one font, filters in a row

Superset is themed with Nova's brand colour and uses a categorical palette validated for colour
blindness on Nova's own surfaces, in both light and dark. Filters sit in a row above the charts,
because they apply to everything below, and a side panel costs an embed a quarter of its width.

## D-045: Sessions are a signed cookie, and the user is read on every request

A session is a JWT in an HttpOnly, SameSite=Lax cookie, so page scripts can't read it and other
sites can't send it. It holds only the user's id. Role and status are read from the database on
each request, so disabling someone or changing their role applies at once. A signing secret shorter
than 32 characters stops startup, because an empty one would let anyone forge a session.

## D-046: SSO is OpenID Connect only

OIDC with PKCE covers Okta, Entra ID, Google, Auth0 and Keycloak, and needs no XML signature
handling. SAML isn't supported. A new SSO user is a viewer with no access, because being known to
the identity provider says who someone is, not what they may read.

## D-047: An email from SSO counts only if the provider says it verified it

Without `email_verified: true`, an email can't link to an existing account or match
`OIDC_ADMIN_EMAILS`. Otherwise anyone who can set their own email at the provider could take over
an account. Providers that leave the claim out still work: those people sign in as new viewers,
and an administrator promotes them.

## D-048: One grant, enforced in two places

Rows are limited by the same grant in the SQL guard, for the agent, and in the guest token, for
dashboards. Both use `scope_clause`, so they can't drift apart. The guest rule names no dataset,
which makes Superset apply it to all of them, and a dataset without the column errors instead of
showing everything. A dashboard over sources with different tenant columns is refused.

## D-049: For someone limited to tenants, tables without the column are refused

A table can't be limited if it doesn't hold the column, so it's refused unless an administrator
marks it as shared, as exchange rates are. The cost is that `invoice_lines`, which reaches its
tenant only through `invoices`, is unavailable to them. Rewriting joins to reach a tenant through
another table would be easy to get subtly wrong, and this is a security boundary.

## D-050: Each run's agent gets its own view of the skills

The agent's file tools read skills directly and aren't subject to the SQL guard. So a non-admin run
gets a temporary folder holding only the skills it's granted, and someone limited to tenants doesn't
get `schema.json`, whose measured values come from every tenant's rows. `load_skill` doesn't need it.
The profiler is told not to name tenants in a skill, but that's an instruction to a model, not a
guarantee, so read a multi-tenant source's skill before approving it.

## D-051: Only administrators share dashboards

Sharing decides what other people can see. A dashboard's datasets aren't limited by who built them
unless the builder was, so an analyst with a whole source could otherwise show it to someone with
less. Shared dashboards are hidden from anyone who can't read every source behind them.

## D-052: A limit that can't be applied means no access

If tenancy is removed from a source, a grant limited to some tenants stops working instead of
widening to the whole source. Sources only reach the agent once they're `ready`, and a dashboard
with no record of its sources can't be shown to a non-admin or shared.

## D-053: Tenancy is confirmed by a person

Nova suggests a tenant column from the profile but never applies it, because a wrong guess is a leak.

## D-054: Credentials go in a file, not in CREATE CATALOG

Trino keeps statement text in its logs and web UI, so `CREATE CATALOG` only names a credentials file
on the `trino-data` volume, readable only by Trino's user. A copy is kept encrypted with Fernet.
Superset holds its own copy, encrypted with its secret key, because `/api/v1/database/` requires one.

## D-055: Who belongs to which tenant lives in Nova, for now

An administrator sets it on the People page. Reading it from an identity provider claim would need
to know how that provider names tenants, so it waits for a real provider's configuration.
