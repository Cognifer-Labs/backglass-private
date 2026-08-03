"""`backglass instagram login` / `chats` — the live lane's setup commands.

No test here reaches Instagram. `instagrapi` is not even installed by default (it is an
optional extra), so the import path is exercised as the product state it is, and the
success paths run against a stub module injected into `sys.modules`.
"""

from __future__ import annotations

import json
import stat
import sys
import types
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import backglass.__main__ as cli


class FakeThread:
    def __init__(self, title: str, users: list[str]):
        self.thread_title = title
        self.users = [types.SimpleNamespace(username=u) for u in users]


class FakeClient:
    """instagrapi's surface, only as far as these commands touch it."""

    instances: list[FakeClient] = []
    #: Class-level so a test can flip the behaviour before the command constructs one.
    fail_login = False
    fail_threads = False

    def __init__(self) -> None:
        self.logged_in_as: tuple[str, str] | None = None
        self.loaded: Path | None = None
        self.challenge_code_handler: Any = None
        self.change_password_handler: Any = None
        self.threads = [FakeThread("Ravi's flat", ["ravi", "priya"]), FakeThread("", ["sam"])]
        FakeClient.instances.append(self)

    def login(self, username: str, password: str) -> None:
        if self.fail_login:
            raise RuntimeError("challenge_required")
        self.logged_in_as = (username, password)

    def dump_settings(self, path: Path) -> None:
        Path(path).write_text(json.dumps({"session": "opaque"}))

    def load_settings(self, path: Path) -> None:
        self.loaded = Path(path)

    def direct_threads(self, amount: int = 20) -> list[FakeThread]:
        if self.fail_threads:
            raise RuntimeError("login_required")
        return self.threads[:amount]


@pytest.fixture
def instagrapi(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    FakeClient.instances = []
    module = types.ModuleType("instagrapi")
    module.Client = FakeClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "instagrapi", module)
    return FakeClient


def test_the_extra_is_named_when_instagrapi_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """It ships switched off and uninstalled, so "not installed" is a normal state that
    should print the one command that fixes it."""
    monkeypatch.setitem(sys.modules, "instagrapi", None)
    result = CliRunner().invoke(cli.app, ["instagram", "chats"])
    assert result.exit_code == 1
    assert "uv sync --extra instagram" in result.output


class TestLogin:
    def _invoke(self, tmp_path: Path, settings: Any, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
        monkeypatch.setattr(cli, "get_settings", lambda: settings)
        return CliRunner().invoke(
            cli.app,
            [
                "instagram", "login",
                "--username", "alexrivera",
                "--session-file", str(tmp_path / "session.json"),
                "--env-path", str(tmp_path / ".env"),
            ],
            input="hunter2\n",
        )

    def test_the_session_is_written_and_the_env_points_at_it(
        self, tmp_path: Path, settings: Any, monkeypatch: pytest.MonkeyPatch, instagrapi: Any
    ) -> None:
        result = self._invoke(tmp_path, settings, monkeypatch)

        assert result.exit_code == 0, result.output
        session = tmp_path / "session.json"
        assert session.exists()
        env = (tmp_path / ".env").read_text()
        assert "INSTAGRAM_USERNAME=alexrivera" in env
        assert f"INSTAGRAM_SESSION_FILE={session}" in env

    def test_the_session_file_is_owner_only(
        self, tmp_path: Path, settings: Any, monkeypatch: pytest.MonkeyPatch, instagrapi: Any
    ) -> None:
        """It is a credential in a file, exactly like .env, and the mode is set before the
        session lands in it — write-then-chmod publishes it for one syscall pair."""
        self._invoke(tmp_path, settings, monkeypatch)
        mode = stat.S_IMODE((tmp_path / "session.json").stat().st_mode)
        assert mode == 0o600, oct(mode)

    def test_the_password_never_reaches_the_env_file_or_the_output(
        self, tmp_path: Path, settings: Any, monkeypatch: pytest.MonkeyPatch, instagrapi: Any
    ) -> None:
        """Asked for once, interactively, and kept nowhere. The whole point of storing a
        session is that nothing ever needs the password again."""
        result = self._invoke(tmp_path, settings, monkeypatch)

        assert "hunter2" not in (tmp_path / ".env").read_text()
        assert "hunter2" not in (tmp_path / "session.json").read_text()
        assert "hunter2" not in result.output

    def test_a_two_factor_prompt_is_wired_before_the_login_runs(
        self, tmp_path: Path, settings: Any, monkeypatch: pytest.MonkeyPatch, instagrapi: Any
    ) -> None:
        """instagrapi asks for the code through a callback rather than by raising, so a
        handler set after login() would never be consulted and a challenged account would
        fail mid-flow with nothing to type into."""
        self._invoke(tmp_path, settings, monkeypatch)
        (client,) = FakeClient.instances
        assert callable(client.challenge_code_handler)

    def test_a_refused_login_is_a_message_not_a_traceback(
        self, tmp_path: Path, settings: Any, monkeypatch: pytest.MonkeyPatch, instagrapi: Any
    ) -> None:
        monkeypatch.setattr(FakeClient, "fail_login", True)
        result = self._invoke(tmp_path, settings, monkeypatch)
        assert result.exit_code == 1
        assert "login failed" in result.output


class TestChats:
    def test_thread_titles_are_listed_for_the_allowlist(
        self, tmp_path: Path, settings: Any, monkeypatch: pytest.MonkeyPatch, instagrapi: Any
    ) -> None:
        """The lane reads named threads, never an inbox, and guessing those names from
        memory is how an allowlist ends up silently matching nothing."""
        session = tmp_path / "session.json"
        session.write_text("{}")
        monkeypatch.setattr(
            cli, "get_settings", lambda: settings.model_copy(
                update={"instagram_session_file": session}
            )
        )

        result = CliRunner().invoke(cli.app, ["instagram", "chats"])

        assert result.exit_code == 0, result.output
        assert "Ravi's flat" in result.output
        assert "sam" in result.output, "a titleless thread falls back to its participants"

    def test_no_session_says_which_command_makes_one(
        self, tmp_path: Path, settings: Any, monkeypatch: pytest.MonkeyPatch, instagrapi: Any
    ) -> None:
        monkeypatch.setattr(
            cli, "get_settings", lambda: settings.model_copy(
                update={"instagram_session_file": tmp_path / "absent.json"}
            )
        )
        result = CliRunner().invoke(cli.app, ["instagram", "chats"])
        assert result.exit_code == 1
        assert "instagram login" in result.output

    def test_an_invalidated_session_says_so_rather_than_crashing(
        self, tmp_path: Path, settings: Any, monkeypatch: pytest.MonkeyPatch, instagrapi: Any
    ) -> None:
        """Instagram can invalidate a session at any time; rule 5 says degrade and name
        the next step rather than blow up."""
        session = tmp_path / "session.json"
        session.write_text("{}")
        monkeypatch.setattr(FakeClient, "fail_threads", True)
        monkeypatch.setattr(
            cli, "get_settings", lambda: settings.model_copy(
                update={"instagram_session_file": session}
            )
        )

        result = CliRunner().invoke(cli.app, ["instagram", "chats"])

        assert result.exit_code == 1
        assert "instagram login" in result.output
