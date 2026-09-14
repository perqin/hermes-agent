"""Workspace moves respect the requested profile, not the gateway launch profile."""

import contextlib
import re

import pytest

from agent import terminal_env_registry
from agent.terminal_env_provider import TerminalEnvironmentProvider
from hermes_constants import hermes_home_key
import tui_gateway.server as server


@pytest.mark.parametrize("matching_live", [False, True])
def test_workspace_move_only_updates_matching_profile_live_session(tmp_path, monkeypatch, matching_live):
    home = tmp_path / "requested"
    home.mkdir()
    (home / "config.yaml").write_text("terminal:\n  backend: local\n")
    other = {"session_key": "duplicate", "profile_home": str(tmp_path / "other"), "cwd": "/old-other"}
    requested = {"session_key": "duplicate", "profile_home": str(home / "sub" / ".."), "cwd": "/old-requested"}
    monkeypatch.setattr(server, "_sessions", {"other": other, **({"requested": requested} if matching_live else {})})
    monkeypatch.setattr(server, "_profile_home", lambda _name: home)
    monkeypatch.setattr(server.git_probe, "branch", lambda *_args: "")
    monkeypatch.setattr(server.git_probe, "common_repo_root", lambda *_args: "")
    updated = []
    monkeypatch.setattr(server, "_set_session_cwd", lambda live, cwd, **_kwargs: updated.append(live))
    monkeypatch.setattr(server, "_cwd_info", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(server, "_emit", lambda *_args: None)

    class DB:
        def get_session(self, _key):
            return None  # A foreign-profile runtime cannot authorize moving a draft.

    @contextlib.contextmanager
    def profile_db(_params):
        yield DB()

    monkeypatch.setattr(server, "_profile_db", profile_db)
    response = server._methods["session.workspace.move"](
        "move", {"profile": "requested", "session_key": "duplicate", "cwd": str(tmp_path)})
    if matching_live:
        assert "error" not in response
        assert updated == [requested]
    else:
        assert response["error"]["code"] == 4007
        assert updated == []


@pytest.mark.parametrize("source", ["config", "env"])
def test_workspace_move_uses_requested_profile_backend(tmp_path, monkeypatch, source):
    home = tmp_path / "remote-profile"
    home.mkdir()
    if source == "config":
        (home / "config.yaml").write_text("terminal:\n  backend: move_remote\n")
    else:
        (home / ".env").write_text("TERMINAL_ENV=move_remote\n")
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setattr(server, "_profile_home", lambda _name: home)
    canonical = "/backend/moved "
    calls = []

    class Environment:
        def execute(self, command, **_kwargs):
            calls.append(command)
            marker = re.search(r"(__HERMES_PROJECT_PATH_[0-9a-f]+__)_BEGIN", command).group(1)
            return {"returncode": 0, "output": f"{marker}_BEGIN\n{canonical}\n{marker}_END\n"}

        def cleanup(self):
            pass

    class Provider(TerminalEnvironmentProvider):
        name = "move_remote"
        display_name = "Move remote"
        filesystem_local = False

        def is_available(self):
            return True

        def create_environment(self, **_kwargs):
            return Environment()

    class DB:
        def get_session(self, key):
            return {"id": key}

        def update_session_cwd(self, key, cwd, *_args, **_kwargs):
            assert cwd == canonical

    @contextlib.contextmanager
    def profile_db(_params):
        yield DB()

    monkeypatch.setattr(server, "_profile_db", profile_db)
    monkeypatch.setattr(server.git_probe, "branch", lambda *_args: pytest.fail("host Git probe"))
    provider = Provider()
    scope = hermes_home_key(home)
    previous = terminal_env_registry.snapshot_registration(provider.name, scope=scope)
    terminal_env_registry.register_provider(provider, scope=scope)
    try:
        response = server._methods["session.workspace.move"](
            "move", {"profile": "remote", "session_key": "review-stored", "cwd": "relative move "})
        assert "error" not in response
        assert response["result"]["cwd"] == canonical
        assert calls
    finally:
        terminal_env_registry.restore_registration(provider.name, provider, previous, scope=scope)
