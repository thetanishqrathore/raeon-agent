#!/usr/bin/env bash
#
# Create or rotate local Hermes web-chat basic-auth credentials.
#
# Writes:
#   ${HERMES_HOME:-~/.hermes}/web-chat-auth.env
#   ${HERMES_HOME:-~/.hermes}/web-chat-credentials.txt
#
# Usage:
#   scripts/web-chat-setup-auth.sh
#   scripts/web-chat-setup-auth.sh 'my-password'
#   HERMES_WEB_CHAT_USERNAME=admin scripts/web-chat-setup-auth.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

python3 - "${1:-}" <<'PY'
import os
import secrets
import shlex
import stat
import sys

sys.path.insert(0, ".")

from hermes_constants import get_hermes_home
from plugins.dashboard_auth.basic import hash_password

home = get_hermes_home()
home.mkdir(parents=True, exist_ok=True)
os.umask(0o077)

username = os.environ.get("HERMES_WEB_CHAT_USERNAME", "admin")
password = (sys.argv[1] if len(sys.argv) > 1 else "") or secrets.token_urlsafe(16)
secret = secrets.token_hex(32)
pwhash = hash_password(password)
host = os.environ.get("HERMES_WEB_CHAT_HOST", "127.0.0.1")
port = os.environ.get("HERMES_WEB_CHAT_PORT", "9119")
public_url = os.environ.get("HERMES_WEB_CHAT_PUBLIC_URL", f"http://{host}:{port}")

auth_env = home / "web-chat-auth.env"
def q(value: str) -> str:
    return shlex.quote(value)

auth_env.write_text(
    "# Hermes Web Chat basic-auth. Sourced by scripts/web-chat-serve.sh. Private (600).\n"
    f"HERMES_DASHBOARD_BASIC_AUTH_USERNAME={q(username)}\n"
    f"HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH={q(pwhash)}\n"
    f"HERMES_DASHBOARD_BASIC_AUTH_SECRET={q(secret)}\n"
    f"HERMES_WEB_CHAT_PORT={q(port)}\n"
    f"HERMES_WEB_CHAT_HOST={q(host)}\n"
    f"HERMES_DASHBOARD_PUBLIC_URL={q(public_url)}\n"
    "HERMES_DASHBOARD_DISABLE_UPDATE='1'\n",
    encoding="utf-8",
)
auth_env.chmod(stat.S_IRUSR | stat.S_IWUSR)

creds = home / "web-chat-credentials.txt"
creds.write_text(
    "Hermes Web Chat login credentials\n"
    "=================================\n"
    f"Username: {username}\n"
    f"Password: {password}\n",
    encoding="utf-8",
)
creds.chmod(stat.S_IRUSR | stat.S_IWUSR)

print(f"wrote {auth_env} (600)")
print(f"wrote {creds} (600)")
print(f"username: {username}")
print(f"password saved to: {creds}")
PY
