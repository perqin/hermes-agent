from __future__ import annotations

import contextlib
from pathlib import Path

import tui_gateway.server as server


def _fail_probe(*_args, **_kwargs):
    raise AssertionError("controller filesystem/Git probe called for remote cwd")


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
    monkeypatch.setattr(server, "_project_info_for_cwd", lambda _cwd: None)
    monkeypatch.setattr(server, "_active_terminal_filesystem_is_local", lambda: True)
    session = {"cwd": str(remote), "filesystem_local": False, "agent": None}

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
