# Nova

A GenAI BI platform. Connect your data sources, and an AI agent learns them and builds
dashboards from a sentence or two, on a semantic layer (Cube) that every number comes from.

> **Status: in development.** You can sign in, connect sources, limit people to their own
> tenants, and build dashboards from a prompt while you watch the agent work.

## Quickstart

You need Docker with Compose, `make` and `openssl`.

```sh
make up
```

The first run takes a few minutes. It generates secrets into `.env`, builds images,
seeds the demo data and waits for everything to be healthy.

| Service | URL | Login |
|---|---|---|
| **Nova** | http://localhost:13000 | `NOVA_ADMIN_EMAIL` and `NOVA_ADMIN_PASSWORD` from `.env` |
| API | http://localhost:18000/docs | The same session as Nova |
| Trino | http://localhost:18080 | Any username |

Cube, the semantic layer, has no port of its own: only Nova's API talks to it, with a short-lived
token for each request that carries the person's own access.

`make gallery` builds a **Chart gallery** project with one of every chart Nova draws, on the rental
demo data.

Run the tests with `make test`, and see every command with `make help`. Tests that call
the real model cost money, so they're separate: `make test-llm`. Set `MODEL_PROVIDER`,
`MODEL_NAME` and the matching API key in `.env` first.

To teach the agent a source and build a dashboard from the command line:

```sh
make skill CATALOG=demo_rental DIALECT=postgres NAME="Rental platform"
make agent PROMPT="Show revenue by tenant for the last 12 months"
```

`docker compose up` also works from the repo root once `.env` exists (`make env`). The command
line is trusted and isn't limited by anyone's access, so keep it to administrators.

## Connecting a source

Sign in as an administrator and open **Sources**. On a fresh install, connect a demo source with
one click, or use **Connect a source** with a PostgreSQL or MySQL database.

Nova tests the connection and warns you if the login can write. It then adds a Trino catalog and
profiles the data into a semantic model, checking every definition by running it, and you can follow
each step. Review the definitions in the **Catalog**: until a person approves one, charts built on it
are marked AI-inferred rather than Certified. The password is
stored encrypted and is never returned or shown to the agent. Use a read-only login.

## People and tenants

Nobody sees any data until an administrator gives them access under **People**.

| Role | Can |
|---|---|
| Administrator | Everything, on all data |
| Analyst | Build dashboards from the sources they can read |
| Viewer | Open dashboards shared with them |

If a source holds several tenants' data, open **Sources**, choose **Tenants** and confirm the tenant
column. You can then limit someone to particular tenants. The limit applies everywhere: to every
query the agent runs for them, and to every dashboard they open, including ones an administrator
built for everyone. A tenant filter on a shared dashboard offers them only their own tenants.

Dashboards are private to whoever built them until an administrator shares them. Cube applies each
person's tenant limit to every chart they open, so a shared dashboard shows everyone their own rows.
A cube whose rows can't be limited, because its table has no tenant column and isn't marked shared, is
hidden from anyone limited to tenants.

## Single sign-on

Nova signs people in with OpenID Connect, which Okta, Microsoft Entra ID, Google, Auth0 and Keycloak
all support. SAML isn't supported. SSO stays off, and hidden, until it's configured.

1. Register a web application with your identity provider, using this redirect URI:
   `http://localhost:18000/api/auth/sso/callback` (your `NOVA_PUBLIC_API_URL`, then
   `/api/auth/sso/callback`).
2. Set these in `.env`, then run `make up`:

| Setting | Value |
|---|---|
| `OIDC_DISCOVERY_URL` | The provider's `.well-known/openid-configuration` URL |
| `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` | From the application you registered |
| `OIDC_PROVIDER_NAME` | The name on the sign-in button |
| `OIDC_ALLOWED_DOMAINS` | Optional. Comma-separated email domains allowed to sign in |
| `OIDC_ADMIN_EMAILS` | Optional. Comma-separated emails that become administrators on first sign-in |

Someone new signs in as a viewer with no access, until an administrator gives them some. An email
only links to an existing account, or matches `OIDC_ADMIN_EMAILS`, if the provider reports it as
verified. Some providers, such as Entra ID, don't send that claim, so make the first administrator
through `NOVA_ADMIN_EMAIL` and promote people from **People**.

## Adding a source type

Add a `Connector` to `CONNECTORS` in `agent/tools/connectors.py`, with its Trino connector, JDBC URL,
and a read-only check. Add the type to `Connection.kind` and to the form in
`ui/src/app/sources/connect-form.tsx`.
If the database exposes declared keys in its own way, add that query to `agent/tools/profiler.py`.
Everything after that, including the semantic model, the agent and row limits, works from the Trino catalog.

## Layout

| Path | Contents |
|---|---|
| `infra/` | Compose stack, Trino and Cube config, demo data, infrastructure tests |
| `agent/` | FastAPI app, deep agent, tools and harness; `agent/semantic/` is the semantic layer |
| `skills/` | Generated and reviewed data-source skills, each with its Cube data model |
| `ui/` | Next.js app |
| `evals/` | Golden question sets |
| `docs/` | Decisions, versions and runbook |
