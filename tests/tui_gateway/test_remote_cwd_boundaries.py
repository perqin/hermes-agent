from __future__ import annotations

import contextlib
from pathlib import Path
import re

import tui_gateway.server as server
from agent import terminal_env_registry
from agent.terminal_env_provider import TerminalEnvironmentProvider
from hermes_constants import hermes_home_key


def _fail_probe(*_args, **_kwargs):
    raise AssertionError("controller filesystem/Git probe called for remote cwd")


def test_only_cwd_config_set_is_dispatched_as_long_running():
    assert server._is_long_handler("config.set", {"key": "cwd"}) is True
    assert server._is_long_handler("config.set", {"key": "terminal.cwd"}) is True
    assert server._is_long_handler("config.set", {"key": "workdir"}) is True
    assert server._is_long_handler("config.set", {"key": "battery"}) is False


def test_remote_process_cwd_suffix_is_preserved(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "plugin-remote")
    monkeypatch.setenv("TERMINAL_CWD", "/backend/workspace ")

    assert server._terminal_task_cwd_with_source(None) == (
        "/backend/workspace ", "process",
    )


def test_remote_attachment_paths_never_resolve_controller_workspace(monkeypatch, tmp_path):
    remote = tmp_path / "remote-that-also-exists"
    remote.mkdir()
    target = tmp_path / "profile" / "attachments" / "note.txt"
    target.parent.mkdir(parents=True)
    target.write_text("note", encoding="utf-8")
    real_resolve = Path.resolve

    def guarded_resolve(path, *args, **kwargs):
        if str(path) == str(remote):
            return _fail_probe()
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", guarded_resolve)
    session = {"cwd": str(remote), "filesystem_local": False}

    assert server._attachment_ref_path(session, target) == str(real_resolve(target))


def test_remote_file_staging_copies_gateway_file_without_resolving_remote_workspace(
    monkeypatch, tmp_path,
):
    remote = tmp_path / "remote-that-also-exists"
    remote.mkdir()
    source = tmp_path / "gateway-note.txt"
    source.write_text("note", encoding="utf-8")
    profile_home = tmp_path / "profile"
    real_resolve = Path.resolve

    def guarded_resolve(path, *args, **kwargs):
        if str(path) == str(remote):
            return _fail_probe()
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", guarded_resolve)
    session = {
        "cwd": str(remote),
        "filesystem_local": False,
        "profile_home": str(profile_home),
    }

    stored, uploaded = server._stage_session_file_attachment(
        session, raw_path=str(source), data_url="", name="",
    )

    assert uploaded is True
    assert stored == profile_home / "attachments" / source.name
    assert stored.read_text(encoding="utf-8") == "note"


def test_remote_session_info_omits_controller_git_probe(monkeypatch, tmp_path):
    remote = tmp_path / "remote-that-also-exists"
    remote.mkdir()
    monkeypatch.setattr(server.git_probe, "branch", _fail_probe)
    monkeypatch.setattr(server.os.path, "isdir", _fail_probe)
    monkeypatch.setattr(server, "_project_info_for_cwd", lambda _cwd: None)
    monkeypatch.setattr(server, "_active_terminal_filesystem_is_local", lambda: True)
    session = {"cwd": str(remote), "filesystem_local": False, "agent": None}

    assert server._display_session_cwd(session) == str(remote)
    info = server._fallback_session_info(session)

    assert info["cwd"] == str(remote)
    assert info["branch"] == ""


def test_remote_workspace_move_resolves_backend_path_and_skips_host_probes(
    monkeypatch, tmp_path,
):
    remote = tmp_path / "remote-that-also-exists"
    remote.mkdir()
    target = "stored-session"
    live = {
        "session_key": target,
        "cwd": "/old/backend/path",
        "filesystem_local": False,
        "agent": None,
    }
    monkeypatch.setitem(server._sessions, "live-sid", live)
    real_path_funcs = {
        name: getattr(server.os.path, name) for name in ("abspath", "expanduser", "isdir")
    }

    def guard_path_func(name):
        def guarded(path, *args, **kwargs):
            if str(path) == str(remote):
                return _fail_probe()
            return real_path_funcs[name](path, *args, **kwargs)
        return guarded

    for probe in real_path_funcs:
        monkeypatch.setattr(server.os.path, probe, guard_path_func(probe))
    monkeypatch.setattr(server.git_probe, "branch", _fail_probe)
    monkeypatch.setattr(server.git_probe, "common_repo_root", _fail_probe)
    monkeypatch.setattr(server, "_register_session_cwd", lambda _session: None)
    monkeypatch.setattr(server, "_emit", lambda *_args: None)
    monkeypatch.setattr(server, "_project_info_for_cwd", lambda _cwd: None)
    monkeypatch.setattr(server, "_active_terminal_filesystem_is_local", lambda: False)

    @contextlib.contextmanager
    def project_runtime_scope():
        yield "workspace-move:test"

    monkeypatch.setattr(server, "_project_runtime_scope", project_runtime_scope)
    from hermes_cli import project_paths
    resolver_calls = []
    monkeypatch.setattr(
        project_paths,
        "resolve_project_folder",
        lambda raw, **kwargs: resolver_calls.append((raw, kwargs)) or str(remote),
    )

    class DB:
        def get_session(self, key):
            return {"id": key}

        def update_session_cwd(self, *_args, **_kwargs):
            return 1

    @contextlib.contextmanager
    def profile_db(_params):
        yield DB()

    @contextlib.contextmanager
    def session_db(_session):
        yield DB()

    monkeypatch.setattr(server, "_profile_db", profile_db)
    monkeypatch.setattr(server, "_session_db", session_db)

    response = server._methods["session.workspace.move"](
        "rid", {"session_key": target, "cwd": str(remote)},
    )

    assert "error" not in response
    assert response["result"] == {
        "cwd": str(remote), "branch": "", "git_repo_root": "",
    }
    assert live["cwd"] == str(remote)
    assert live["filesystem_local"] is False
    assert resolver_calls == [(str(remote), {"operation_scope": "workspace-move:test"})]


def test_remote_workspace_move_resolution_failure_does_not_mutate_session_or_db(
    monkeypatch, tmp_path,
):
    remote = tmp_path / "remote-that-also-exists"
    remote.mkdir()
    target = "stored-session-failure"
    live = {
        "session_key": target,
        "cwd": "/old/backend/path",
        "filesystem_local": False,
        "agent": None,
    }
    monkeypatch.setitem(server._sessions, "live-failure", live)
    monkeypatch.setattr(server, "_active_terminal_filesystem_is_local", lambda: False)

    @contextlib.contextmanager
    def project_runtime_scope():
        yield "workspace-move:test"

    monkeypatch.setattr(server, "_project_runtime_scope", project_runtime_scope)
    from hermes_cli import project_paths
    monkeypatch.setattr(
        project_paths,
        "resolve_project_folder",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("backend rejected cwd")),
    )
    monkeypatch.setattr(
        server,
        "_profile_db",
        lambda _params: (_ for _ in ()).throw(AssertionError("DB opened before resolution")),
    )

    response = server._methods["session.workspace.move"](
        "rid", {"session_key": target, "cwd": str(remote)},
    )

    assert response["error"]["code"] == 4017
    assert live["cwd"] == "/old/backend/path"
    assert live["filesystem_local"] is False


def test_session_context_pins_remote_locality_after_ambient_backend_changes(monkeypatch):
    from agent import runtime_cwd

    session = {
        "session_key": "pinned-remote",
        "cwd": "/backend/project",
        "filesystem_local": False,
        "agent": None,
    }
    monkeypatch.setitem(server._sessions, "pinned-sid", session)

    tokens = server._set_session_context("pinned-remote")
    try:
        assert runtime_cwd.filesystem_is_local() is False
    finally:
        server._clear_session_context(tokens)


def test_session_create_uses_requested_profile_environment_and_preserves_suffix(
    monkeypatch, tmp_path,
):
    profile_home = tmp_path / "coder"
    profile_home.mkdir()
    (profile_home / "config.yaml").write_text(
        "terminal:\n  backend: session_remote\n", encoding="utf-8")
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setattr(server, "_profile_home", lambda profile: profile_home if profile == "coder" else None)
    monkeypatch.setattr(server, "_schedule_agent_build", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
    monkeypatch.setattr(server, "_register_session_cwd", lambda _session: None)
    monkeypatch.setattr(server, "_project_info_for_cwd", lambda _cwd: None)

    canonical = "/srv/project \\"
    fail = False

    class Environment:
        is_local = True  # provider declaration must override this adversarial value

        def execute(self, command, **_kwargs):
            if fail:
                return {"returncode": 1, "output": "no such directory"}
            marker = re.search(r"(__HERMES_PROJECT_PATH_[0-9a-f]+__)_BEGIN", command).group(1)
            return {"returncode": 0, "output": f"{marker}_BEGIN\n{canonical}\n{marker}_END\n"}

        def cleanup(self):
            pass

    class Provider(TerminalEnvironmentProvider):
        name = "session_remote"
        display_name = "Session Remote"
        filesystem_local = False

        def is_available(self):
            return True

        def create_environment(self, **_kwargs):
            return Environment()

    provider = Provider()
    scope = hermes_home_key(profile_home)
    previous = terminal_env_registry.snapshot_registration(provider.name, scope=scope)
    terminal_env_registry.register_provider(provider, scope=scope)
    before = set(server._sessions)
    try:
        response = server._methods["session.create"](
            "create", {"profile": "coder", "cwd": "relative path \\"})
        created = response["result"]["session_id"]
        assert server._sessions[created]["cwd"] == canonical
        assert server._sessions[created]["filesystem_local"] is False
        assert server._sessions[created]["explicit_cwd"] is True

        fail = True
        failed = server._methods["session.create"](
            "failed", {"profile": "coder", "cwd": "missing \\"})
        assert failed["error"]["code"] == 4017
        assert set(server._sessions) == before | {created}
    finally:
        server._sessions.pop(created, None)
        terminal_env_registry.restore_registration(provider.name, provider, previous, scope=scope)


def test_config_set_remote_cwd_uses_backend_authority_and_preserves_suffix(monkeypatch):
    raw = "relative workspace " + chr(92)
    canonical = "/srv/canonical workspace " + chr(92)
    remote = type("RemoteEnvironment", (), {"is_local": False})()

    @contextlib.contextmanager
    def runtime_scope():
        yield "config-cwd:profile-b"

    monkeypatch.setattr(server, "_project_runtime_scope", runtime_scope)
    monkeypatch.setattr(
        "tools.terminal_tool.acquire_terminal_environment", lambda **_kwargs: remote)
    resolver_calls = []
    monkeypatch.setattr(
        "hermes_cli.project_paths.resolve_project_folder",
        lambda value, **kwargs: resolver_calls.append((value, kwargs)) or canonical,
    )
    real_path_funcs = {
        name: getattr(server.os.path, name) for name in ("abspath", "expanduser", "isdir")
    }
    for name, real in real_path_funcs.items():
        monkeypatch.setattr(
            server.os.path,
            name,
            lambda value, *args, _real=real, **kwargs: (
                _fail_probe() if str(value).startswith("relative workspace")
                else _real(value, *args, **kwargs)
            ),
        )
    monkeypatch.setattr(server.git_probe, "branch", _fail_probe)
    writes = []
    monkeypatch.setattr(server, "_write_config_key", lambda key, value: writes.append((key, value)))

    response = server._methods["config.set"](
        "config", {"key": "terminal.cwd", "value": raw})

    assert response["result"]["value"] == canonical
    assert response["result"]["cwd"] == canonical
    assert response["result"]["branch"] == ""
    assert resolver_calls == [(
        raw, {"operation_scope": "config-cwd:profile-b", "environment": remote})]
    assert writes == [("terminal.cwd", canonical)]


def test_session_cwd_set_resolves_before_live_or_db_mutation(monkeypatch):
    raw = "relative cwd " + chr(92)
    canonical = "/srv/session cwd " + chr(92)
    live = {
        "session_key": "cwd-target",
        "cwd": "/srv/old",
        "filesystem_local": False,
        "profile_home": None,
        "agent": None,
        "agent_error": None,
        "history": [],
        "history_lock": __import__("threading").Lock(),
        "transport": None,
        "running": False,
    }
    monkeypatch.setitem(server._sessions, "cwd-sid", live)
    monkeypatch.setattr(
        "tools.terminal_tool.acquire_terminal_environment",
        lambda **_kwargs: type("RemoteEnvironment", (), {"is_local": False})(),
    )
    fail = False
    seen = []

    def resolve(value, **kwargs):
        seen.append((value, kwargs))
        if fail:
            raise ValueError("backend rejected cwd")
        return canonical

    monkeypatch.setattr("hermes_cli.project_paths.resolve_project_folder", resolve)
    monkeypatch.setattr(server, "_register_session_cwd", lambda _session: None)
    monkeypatch.setattr(server, "_emit", lambda *_args: None)
    writes = []

    class DB:
        def update_session_cwd(self, key, cwd, *_args, **_kwargs):
            writes.append((key, cwd))
            return 1

    @contextlib.contextmanager
    def session_db(_session):
        yield DB()

    monkeypatch.setattr(server, "_session_db", session_db)
    response = server._methods["session.cwd.set"](
        "cwd", {"session_id": "cwd-sid", "cwd": raw})

    assert "error" not in response
    assert live["cwd"] == canonical
    assert writes == [("cwd-target", canonical)]
    assert seen[0][0] == raw

    fail = True
    failed = server._methods["session.cwd.set"](
        "cwd-fail", {"session_id": "cwd-sid", "cwd": "missing " + chr(92)})
    assert failed["error"]["code"] == 4017
    assert live["cwd"] == canonical
    assert writes == [("cwd-target", canonical)]
