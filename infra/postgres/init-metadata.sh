#!/bin/sh
# Runs once, on first boot of the metadata Postgres. The image has already
# created the `superset` database; this adds the separate application database
# the agent uses for checkpoints, skills tracking and the audit log.
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<SQL
CREATE DATABASE nova OWNER "$POSTGRES_USER";
SQL
