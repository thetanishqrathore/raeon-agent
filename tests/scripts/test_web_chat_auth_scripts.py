import json
import os
import stat
import subprocess
from pathlib import Path


def test_web_chat_setup_auth_uses_profile_home_and_shell_quotes(tmp_path: Path):
    repo = Path(__file__).resolve().parents[2]
    hermes_home = tmp_path / "profile"
    fake_home = tmp_path / "home"
    username = "demo'user\nname"
    password = "pw'with\nnewline"

    env = os.environ.copy()
    env.update(
        {
            "HERMES_HOME": str(hermes_home),
            "HOME": str(fake_home),
            "HERMES_WEB_CHAT_USERNAME": username,
            "HERMES_WEB_CHAT_HOST": "127.0.0.1",
            "HERMES_WEB_CHAT_PORT": "9123",
        }
    )

    result = subprocess.run(
        ["bash", "scripts/web-chat-setup-auth.sh", password],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    auth_env = hermes_home / "web-chat-auth.env"
    creds = hermes_home / "web-chat-credentials.txt"
    assert auth_env.exists()
    assert creds.exists()
    assert not (fake_home / ".hermes" / "web-chat-auth.env").exists()
    assert stat.S_IMODE(auth_env.stat().st_mode) == 0o600
    assert stat.S_IMODE(creds.stat().st_mode) == 0o600
    assert password not in result.stdout

    roundtrip = subprocess.run(
        [
            "bash",
            "-c",
            (
                "set -a; source \"$1\"; set +a; "
                "python3 -c 'import json, os; print(json.dumps({"
                "\"username\": os.environ[\"HERMES_DASHBOARD_BASIC_AUTH_USERNAME\"], "
                "\"host\": os.environ[\"HERMES_WEB_CHAT_HOST\"], "
                "\"port\": os.environ[\"HERMES_WEB_CHAT_PORT\"], "
                "\"url\": os.environ[\"HERMES_DASHBOARD_PUBLIC_URL\"]"
                "}))'"
            ),
            "bash",
            str(auth_env),
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    data = json.loads(roundtrip.stdout)
    assert data == {
        "username": username,
        "host": "127.0.0.1",
        "port": "9123",
        "url": "http://127.0.0.1:9123",
    }
