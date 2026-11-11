# Runbook

## Requirements

Docker with Compose v2 (tested on Docker 29.1 and Compose 5.0), `make` and `openssl`.
Give Docker at least 8 GB of memory. The current stack uses about 2.5 GB when idle.

## Common commands

| Command | What it does |
|---|---|
| `make up` | Build, start, seed and wait for healthy |
| `make test` | Run all tests against the running stack |
| `make logs s=trino` | Follow one service's logs |
| `make seed` | Rebuild the demo data from scratch |
| `make down` | Stop, keeping data |
| `make reset` | Stop and delete all data |

## Troubleshooting

**`make up` times out or a service is unhealthy.** Run `make ps`, then `make logs
s=<service>`.

**A port is already in use.** Change the matching `*_HOST_PORT` in `.env` and run
`make up` again. Changing the UI port means updating `NOVA_UI_ORIGIN`, because the API only
accepts requests from that origin, and the dashboard renderer opens pages there.

**Upgrading from a version with Superset.** Run `make up`: it removes the Superset and Redis
containers (`--remove-orphans`), and Nova drops its Superset columns itself. The metadata
Postgres keeps an unused `superset` database, which `DROP DATABASE superset` removes. Sheets built in
Superset show a **Rebuild** button, which builds them again from the request that made them.
Profile each source again, from **Sources** or with `make skill`, to give it a semantic model.

**A semantic model doesn't load.** Cube compiles every published model together, so one broken
file stops all of them: `make logs s=cube` names it. Nova checks a model before publishing it, so
this should only follow a hand edit under `skills/`.

**Authentication errors after deleting `.env`.** The volumes still hold the old
passwords. Run `make reset`, then `make up`.

**"The last 12 months" looks short.** Demo data stops on the day it was seeded. Run
`make seed` to bring it up to yesterday. Closed periods keep the same numbers.

**A test left a schema change behind.** `make seed` drops and rebuilds both sources.

**Nobody can sign in.** The first administrator is created from `NOVA_ADMIN_EMAIL` and
`NOVA_ADMIN_PASSWORD` when the API starts. It's only created once, so changing `.env` afterwards
doesn't change the password.

**The API won't start, saying `NOVA_JWT_SECRET` is too short.** Run `make env`. It adds new settings
to an existing `.env`, and never changes the ones already there.

**SSO sends people back to the sign-in page.** The page says why. "Single sign-on didn't complete"
usually means the redirect URI at the provider doesn't exactly match your `NOVA_PUBLIC_API_URL`
followed by `/api/auth/sso/callback`, and the API log has the provider's reason. "An account already
uses that email" means the provider didn't report the email as verified, so it wasn't linked.

**A source shows as failed.** Press Retry on the Sources page to profile it again. If it failed
before its Trino catalog was created, connect it again with the same name and its password.

**Someone limited to a tenant sees chart errors.** Each dataset on a multi-tenant source must output
the tenant column. That's only enforced for dashboards built after tenancy was turned on, so
rebuild the dashboard.
