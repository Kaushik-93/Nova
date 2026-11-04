# Nova

A GenAI BI platform. Connect your data sources, and an AI agent learns them and builds
Apache Superset dashboards from a sentence or two.

> **Status: in development.** Infrastructure, the Superset client, the SQL harness, profiling
> and the dashboard agent are complete. The API and UI are still to come.

## Quickstart

You need Docker with Compose, `make` and `openssl`.

```sh
make up
```

The first run takes a few minutes. It generates secrets into `.env`, builds images,
seeds the demo data and waits for everything to be healthy.

| Service | URL | Login |
|---|---|---|
| Superset | http://localhost:18088 | `admin`, with the password from `.env` |
| Trino | http://localhost:18080 | Any username |

Run the tests with `make test`, and see every command with `make help`. Tests that call
the real model cost money, so they're separate: `make test-llm`. Set `MODEL_PROVIDER`,
`MODEL_NAME` and the matching API key in `.env` first.

`docker compose up` also works from the repo root once `.env` exists (`make env`).

## Layout

| Path | Contents |
|---|---|
| `infra/` | Compose stack, Superset and Trino config, demo data, infrastructure tests |
| `agent/` | FastAPI app, deep agent, tools and harness |
| `skills/` | Generated and reviewed data-source skills |
| `ui/` | Next.js app |
| `evals/` | Golden question sets |
| `docs/` | Decisions, versions and runbook |
