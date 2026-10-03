# Web Chat

The web dashboard can be run locally for demo and development. This doc avoids any private hostnames or deployment-specific assumptions.

## Create Local Auth

```bash
scripts/web-chat-setup-auth.sh
```

The script writes credentials under `${HERMES_HOME:-~/.hermes}`:

- `web-chat-auth.env` contains the username, password hash, signing secret, host, and port.
- `web-chat-credentials.txt` contains the generated plaintext password for local login.

Both files are written with mode `600`.

Defaults:

- username: `admin`
- host: `127.0.0.1`
- port: `9119`
- public URL: `http://127.0.0.1:9119`

Override them as needed:

```bash
HERMES_WEB_CHAT_USERNAME=demo \
HERMES_WEB_CHAT_HOST=127.0.0.1 \
HERMES_WEB_CHAT_PORT=9119 \
scripts/web-chat-setup-auth.sh
```

## Run Locally

After Python and npm dependencies are installed, run from the activated Python environment:

```bash
source .venv/bin/activate
scripts/web-chat-serve.sh
```

Open the local URL printed by the server and sign in with the generated credentials.

## Public Hosting Notes

For public hosting, put the dashboard behind HTTPS and a reverse proxy, keep `HERMES_WEB_CHAT_HOST` bound to localhost or a private interface, and set `HERMES_WEB_CHAT_PUBLIC_URL` to the external HTTPS URL. Keep provider keys in `${HERMES_HOME:-~/.hermes}/.env`, not in the repository.

This repo does not include any external deployment workflow. Production hosting, domain configuration, TLS, and access control are intentionally left to the deployment environment.
