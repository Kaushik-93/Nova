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
`make up` again. If you change `SUPERSET_HOST_PORT`, change `SUPERSET_PUBLIC_URL` too.
Changing the UI port means updating `NOVA_UI_ORIGIN`, because Superset only lets that
origin frame dashboards.

**Authentication errors after deleting `.env`.** The volumes still hold the old
passwords. Run `make reset`, then `make up`.

**"The last 12 months" looks short.** Demo data stops on the day it was seeded. Run
`make seed` to bring it up to yesterday. Closed periods keep the same numbers.

**A test left a schema change behind.** `make seed` drops and rebuilds both sources.
