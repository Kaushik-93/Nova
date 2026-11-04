#!/usr/bin/env bash
# One-shot setup, safe to re-run: migrations, admin user, roles, then the
# Nova-specific objects in bootstrap.py.
set -euo pipefail

echo "[nova] applying Superset migrations"
superset db upgrade

echo "[nova] ensuring admin user '${SUPERSET_ADMIN_USER}'"
superset fab create-admin \
  --username "${SUPERSET_ADMIN_USER}" \
  --firstname Nova \
  --lastname Admin \
  --email "${SUPERSET_ADMIN_EMAIL:-admin@nova.local}" \
  --password "${SUPERSET_ADMIN_PASSWORD}"

echo "[nova] syncing roles and permissions"
superset init

echo "[nova] registering Trino"
python /app/nova/bootstrap.py

echo "[nova] Superset init complete"
