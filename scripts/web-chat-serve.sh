#!/usr/bin/env bash
#
# Serve the Hermes web chat (dashboard) with basic auth.
#
# Reads credentials from ${HERMES_HOME:-~/.hermes}/web-chat-auth.env
# (create/rotate with scripts/web-chat-setup-auth.sh).
#
# Usage:
#   scripts/web-chat-serve.sh              # build the SPA, then serve
#   scripts/web-chat-serve.sh --skip-build # serve the existing web_dist
#
# IMPORTANT: plain HTTP sends the password in cleartext. For anything beyond a
# trusted LAN, front this with TLS (see docs/web-chat-deploy.md) or use an SSH
# tunnel instead of a public bind.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_HOME_DEFAULT="${HERMES_HOME:-$HOME/.hermes}"
AUTH_ENV="${HERMES_WEB_CHAT_AUTH_ENV:-$HERMES_HOME_DEFAULT/web-chat-auth.env}"

if [[ ! -f "$AUTH_ENV" ]]; then
  echo "Missing $AUTH_ENV — run scripts/web-chat-setup-auth.sh first." >&2
  exit 1
fi

# shellcheck disable=SC1090
set -a; source "$AUTH_ENV"; set +a

HOST="${HERMES_WEB_CHAT_HOST:-127.0.0.1}"
PORT="${HERMES_WEB_CHAT_PORT:-9119}"

if [[ "${1:-}" != "--skip-build" ]]; then
  echo "Building web UI…"
  ( cd "$REPO_DIR" && npm run -w web build )
fi

echo "Starting Hermes dashboard on ${HOST}:${PORT} (basic auth user: ${HERMES_DASHBOARD_BASIC_AUTH_USERNAME:-unset})"
exec hermes dashboard --skip-build --no-open --host "$HOST" --port "$PORT"
