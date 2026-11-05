#!/bin/sh
# Creates .env from .env.example with fresh random secrets. Never overwrites an
# existing .env, so re-running is safe and keeps the secrets your volumes use.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
ENV_FILE="$ROOT/.env"

command -v openssl >/dev/null || { echo "openssl is required to generate secrets" >&2; exit 1; }

value_for() {
  case "$1" in
    __GENERATE_FERNET__) openssl rand -base64 32 | tr '+/' '-_' ;; # 32 random bytes, url-safe
    __GENERATE__) openssl rand -hex 24 ;;
    *) printf '%s' "$1" ;;
  esac
}

umask 077
if [ ! -f "$ENV_FILE" ]; then
  : >"$ENV_FILE"
  NEW=1
fi

# Fills in whatever is missing, so a new setting reaches an existing .env, and never changes a
# value that's there, because the data volumes depend on the secrets they were created with.
ADDED=0
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in
    ''|\#*)
      [ -n "${NEW:-}" ] && printf '%s\n' "$line" >>"$ENV_FILE" ;;
    *=*)
      key=${line%%=*}
      if ! grep -q "^$key=" "$ENV_FILE"; then
        printf '%s=%s\n' "$key" "$(value_for "${line#*=}")" >>"$ENV_FILE"
        ADDED=$((ADDED + 1))
      fi ;;
  esac
done <"$ROOT/.env.example"

if [ -n "${NEW:-}" ]; then
  echo "created .env with generated secrets; set an LLM key before profiling"
elif [ "$ADDED" -gt 0 ]; then
  echo "added $ADDED new settings to .env"
fi
