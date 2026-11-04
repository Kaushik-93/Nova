#!/bin/sh
# Creates .env from .env.example with fresh random secrets. Never overwrites an
# existing .env, so re-running is safe and keeps the secrets your volumes use.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
ENV_FILE="$ROOT/.env"

if [ -f "$ENV_FILE" ]; then
  echo ".env already exists; keeping it"
  exit 0
fi

command -v openssl >/dev/null || { echo "openssl is required to generate secrets" >&2; exit 1; }

umask 077
: >"$ENV_FILE"
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in
    *=__GENERATE_FERNET__)
      # Fernet keys are 32 random bytes in url-safe base64.
      printf '%s=%s\n' "${line%%=*}" "$(openssl rand -base64 32 | tr '+/' '-_')" >>"$ENV_FILE" ;;
    *=__GENERATE__)
      printf '%s=%s\n' "${line%%=*}" "$(openssl rand -hex 24)" >>"$ENV_FILE" ;;
    *)
      printf '%s\n' "$line" >>"$ENV_FILE" ;;
  esac
done <"$ROOT/.env.example"

echo "created .env with generated secrets; set an LLM key before profiling"
