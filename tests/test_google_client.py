"""`backglass google-client` — loading a downloaded OAuth client into .env.

The command exists because of a mistake this machine had already made: a
`client_secret_*.json` sitting in ~/Downloads that was a **web** client from an unrelated
project. Google's Credentials page hands those out just as readily as Desktop ones, they
look identical by filename, and the failure lands deep inside the consent flow talking
about redirect URIs rather than about the client type.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

import backglass.__main__ as cli


def client_file(tmp_path: Path, kind: str = "installed", **fields: str) -> Path:
    body = {
        "client_id": "123-abc.apps.googleusercontent.com",
        "client_secret": "GOCSPX-not-a-real-secret",
        "project_id": "my-first-project",
        **fields,
    }
    path = tmp_path / f"client_secret_{kind}.json"
    path.write_text(json.dumps({kind: body}))
    return path


def run(*args: str):  # type: ignore[no-untyped-def]
    return CliRunner().invoke(cli.app, ["google-client", *args])


def test_a_desktop_client_lands_in_the_env_file(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    result = run(str(client_file(tmp_path)), "--env-path", str(env))

    assert result.exit_code == 0, result.output
    written = env.read_text()
    assert "GOOGLE_CLIENT_ID=123-abc.apps.googleusercontent.com" in written
    assert "GOOGLE_CLIENT_SECRET=GOCSPX-not-a-real-secret" in written


def test_the_secret_is_never_printed(tmp_path: Path) -> None:
    """It goes from the file the browser downloaded straight into .env. Echoing it would
    put a live credential into a terminal scrollback and any transcript of the run."""
    env = tmp_path / ".env"
    result = run(str(client_file(tmp_path)), "--env-path", str(env))
    assert "GOCSPX-not-a-real-secret" not in result.output


def test_a_web_client_is_refused_with_the_actual_fix(tmp_path: Path) -> None:
    """The real failure, reproduced: the file on this machine was a `web` client for a
    Supabase callback. Accepting it would have produced a redirect-URI error from inside
    google-auth-oauthlib that names nothing the owner can act on."""
    env = tmp_path / ".env"
    result = run(str(client_file(tmp_path, kind="web")), "--env-path", str(env))

    assert result.exit_code == 1
    assert "not a Desktop app one" in result.output
    assert "Application type: Desktop app" in result.output
    assert not env.exists(), "a refused client must not half-write the env file"


def test_a_client_missing_its_secret_is_refused(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    result = run(str(client_file(tmp_path, client_secret="")), "--env-path", str(env))
    assert result.exit_code == 1
    assert not env.exists()


def test_an_unreadable_file_is_a_message_not_a_traceback(tmp_path: Path) -> None:
    """Rule 5: degrade, never blow up. The argument is a path someone typed."""
    result = run(str(tmp_path / "nope.json"), "--env-path", str(tmp_path / ".env"))
    assert result.exit_code == 1
    assert "cannot read" in result.output


def test_running_it_twice_writes_nothing_the_second_time(tmp_path: Path) -> None:
    """`envfile.set_keys` is idempotent and reports only real changes; the command's
    summary has to reflect that rather than claiming work it did not do."""
    env = tmp_path / ".env"
    source = client_file(tmp_path)
    run(str(source), "--env-path", str(env))
    second = run(str(source), "--env-path", str(env))
    assert second.exit_code == 0
    assert "no change(s)" in second.output
