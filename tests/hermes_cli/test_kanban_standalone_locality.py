"""Missing Board metadata is not a terminal filesystem locality grant."""
from unittest.mock import Mock

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_workspace as kbw


@pytest.mark.parametrize("kind", ["dir", "worktree"])
@pytest.mark.parametrize("backend", ["ssh", "unregistered-provider"])
def test_standalone_backend_workspace_never_reaches_host(tmp_path, monkeypatch, kind, backend):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("TERMINAL_ENV", backend)
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: lambda name: True)
    kb.init_db()
    host = Mock(side_effect=AssertionError("backend path reached controller"))
    monkeypatch.setattr(kbw, "resolve_workspace", host)
    monkeypatch.setattr(kbw, "_resolve_worktree_workspace", host)
    raw = "/backend/opaque "
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="standalone", assignee="default",
                                 workspace_kind=kind, workspace_path=raw)
        task = kb.get_task(conn, task_id)
        assert task.workspace_requires_preflight is True
        assert task.workspace_root == raw
        binding = kbd._backend_owned_project_binding(task, None)
        assert binding["default_workdir"] == raw
        spawned = []
        result = kbd.dispatch_once(conn, spawn_fn=lambda task, workspace, **kw: spawned.append(workspace) or 4242)
        assert [row[0] for row in result.spawned] == [task_id]
        assert spawned == [raw if kind == "dir" else f"{raw}/.worktrees/{task_id}"]
    host.assert_not_called()


@pytest.mark.parametrize("kind", ["dir", "worktree"])
@pytest.mark.parametrize("source", ["config", "dotenv", "assignee-config", "assignee-dotenv"])
def test_standalone_local_creator_cannot_authorize_remote_policy(tmp_path, monkeypatch, kind, source):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("TERMINAL_ENV", "local")
    target = tmp_path
    assignee = "default"
    if source.startswith("assignee"):
        target = tmp_path / "profiles" / "remote"
        target.mkdir(parents=True)
        assignee = "remote"
    if source.endswith("config"):
        (target / "config.yaml").write_text("terminal:\n  backend: ssh\n")
    else:
        (target / ".env").write_text("TERMINAL_ENV=ssh\n")
    kb.init_db()
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="scoped", assignee=assignee,
                                 workspace_kind=kind, workspace_path="/backend/opaque ")
        task = kb.get_task(conn, task_id)
        assert task.workspace_requires_preflight is True
        assert task.workspace_root == "/backend/opaque "


@pytest.mark.parametrize("kind", ["dir", "worktree"])
@pytest.mark.parametrize("backend", ["ssh", "unregistered-provider"])
def test_standalone_dispatch_rechecks_routed_policy(tmp_path, monkeypatch, kind, backend):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: lambda name: True)
    kb.init_db()
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="legacy", assignee="default",
                                 workspace_kind=kind, workspace_path="/backend/opaque ")
        assert kb.get_task(conn, task_id).workspace_requires_preflight is False
        target = tmp_path / "profiles" / "later"
        target.mkdir(parents=True)
        (target / ".env").write_text(f"TERMINAL_ENV={backend}\n")
        conn.execute("UPDATE tasks SET assignee = 'later' WHERE id = ?", (task_id,))
        conn.commit()
        host = Mock(side_effect=AssertionError("host resolution forbidden"))
        monkeypatch.setattr(kbw, "resolve_workspace", host)
        monkeypatch.setattr(kbw, "_resolve_worktree_workspace", host)
        spawn = Mock(return_value=4242)
        result = kbd.dispatch_once(conn, spawn_fn=spawn)
        host.assert_not_called()
        spawn.assert_not_called()
        assert result.spawned == []
        assert kb.get_task(conn, task_id).workspace_path == "/backend/opaque "


@pytest.mark.parametrize("kind", ["dir", "worktree"])
def test_standalone_active_scope_is_authoritative_for_same_profile(tmp_path, monkeypatch, kind):
    from tools.terminal_scope import set_terminal_scope, reset_terminal_scope

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    (tmp_path / ".env").write_text("TERMINAL_ENV=ssh\n")
    kb.init_db()
    token = set_terminal_scope({"TERMINAL_ENV": "local"})
    try:
        with kbc.connect() as conn:
            task_id = kb.create_task(conn, title="active scope", assignee="default",
                                     workspace_kind=kind, workspace_path=str(tmp_path))
            assert kb.get_task(conn, task_id).workspace_requires_preflight is False
    finally:
        reset_terminal_scope(token)


@pytest.mark.parametrize("kind", ["dir", "worktree"])
@pytest.mark.parametrize("locality", [True, False, None])
def test_standalone_scoped_provider_contract(tmp_path, monkeypatch, kind, locality):
    from agent import terminal_env_registry as registry
    from agent.terminal_env_provider import TerminalEnvironmentProvider
    from hermes_constants import hermes_home_key

    class Provider(TerminalEnvironmentProvider):
        name = "standalone_test_provider"
        display_name = "Standalone test"
        filesystem_local = locality

        def is_available(self):
            return True

        def create_environment(self, **kwargs):
            pytest.fail("locality must not acquire an environment")

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: lambda name: True)
    target = tmp_path / "profiles" / "worker"
    target.mkdir(parents=True)
    (target / "config.yaml").write_text(f"terminal:\n  backend: {Provider.name}\n")
    scope = hermes_home_key(target)
    provider = Provider()
    previous = registry.snapshot_registration(provider.name, scope=scope)
    registry.register_provider(provider, scope=scope)
    kb.init_db()
    try:
        with kbc.connect() as conn:
            task_id = kb.create_task(conn, title="provider scope", assignee="worker",
                                     workspace_kind=kind, workspace_path="/backend/opaque ")
            task = kb.get_task(conn, task_id)
            assert task.workspace_requires_preflight is (locality is not True)
            host = Mock(return_value=("/host/workspace", "wt/test") if kind == "worktree"
                        else "/host/workspace")
            monkeypatch.setattr(kbw, "resolve_workspace", host)
            monkeypatch.setattr(kbw, "_resolve_worktree_workspace", host)
            result = kbd.dispatch_once(conn, spawn_fn=lambda *args, **kwargs: 4242)
            assert [row[0] for row in result.spawned] == [task_id]
            if locality is True:
                host.assert_called_once()
            else:
                host.assert_not_called()
                assert task.workspace_root == "/backend/opaque "
    finally:
        registry.restore_registration(provider.name, provider, previous, scope=scope)
