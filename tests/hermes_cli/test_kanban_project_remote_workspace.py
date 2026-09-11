"""Project-bound Kanban workspaces stay in the assignee backend namespace."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_workspace as kbw


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    (root / "profiles" / "worker-b").mkdir(parents=True)
    (root / "profiles" / "worker-b" / "config.yaml").write_text(
        "toolsets:\n  - kanban\n", encoding="utf-8",
    )
    (root / "config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: (lambda _name: True))
    kb.init_db()
    return root


def _remote_board():
    kb.create_board(
        "shared",
        project_id="p_remote",
        project_slug="remote-app",
        source_profile="owner",
        default_workdir="/srv/shared/remote-app",
        default_workspace_kind="worktree",
        filesystem_local=False,
    )


def test_dispatcher_does_not_materialize_backend_owned_worktree(home, monkeypatch):
    _remote_board()
    with kbc.connect(board="shared") as conn:
        task_id = kb.create_task(
            conn,
            title="remote work",
            assignee="worker-b",
            board="shared",
        )
        monkeypatch.setattr(
            kbw,
            "_resolve_worktree_workspace",
            lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("controller git touched remote path")),
        )
        spawned = []
        result = kbd.dispatch_once(
            conn,
            board="shared",
            spawn_fn=lambda task, workspace, board=None: spawned.append((task, workspace, board)) or 4242,
            max_spawn=1,
        )

    assert [row[0] for row in result.spawned] == [task_id]
    assert spawned[0][1] == f"/srv/shared/remote-app/.worktrees/{task_id}"


def test_source_local_project_still_defers_materialization_to_assignee(home, monkeypatch):
    kb.create_board(
        "source-local",
        project_id="p_local_source",
        project_slug="local-source",
        source_profile="owner",
        default_workdir="/owner/project",
        default_workspace_kind="worktree",
        filesystem_local=True,
    )
    with kbc.connect(board="source-local") as conn:
        task_id = kb.create_task(
            conn, title="cross profile", assignee="worker-b", board="source-local",
        )
        monkeypatch.setattr(
            kbw,
            "_resolve_worktree_workspace",
            lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("dispatcher materialized Project path")),
        )
        spawned = []
        result = kbd.dispatch_once(
            conn,
            board="source-local",
            spawn_fn=lambda task, workspace, board=None: spawned.append(workspace) or 4242,
            max_spawn=1,
        )

    assert [row[0] for row in result.spawned] == [task_id]
    assert spawned == [f"/owner/project/.worktrees/{task_id}"]


def test_nonproject_remote_board_directory_is_not_touched_by_dispatcher(home, monkeypatch):
    kb.create_board(
        "remote-dir",
        default_workdir="/srv/shared/output",
        default_workspace_kind="dir",
        filesystem_local=False,
    )
    with kbc.connect(board="remote-dir") as conn:
        task_id = kb.create_task(
            conn,
            title="write output",
            assignee="worker-b",
            board="remote-dir",
            workspace_kind="dir",
        )
        monkeypatch.setattr(
            kbw,
            "resolve_workspace",
            lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("controller mkdir touched remote path")),
        )
        spawned = []
        result = kbd.dispatch_once(
            conn,
            board="remote-dir",
            spawn_fn=lambda task, workspace, board=None: spawned.append(workspace) or 4242,
            max_spawn=1,
        )

    assert [row[0] for row in result.spawned] == [task_id]
    assert spawned == ["/srv/shared/output"]


def _task(task_id="t_remote"):
    return kb.Task(
        id=task_id,
        title="remote",
        body=None,
        assignee="worker-b",
        status="running",
        priority=0,
        created_by="test",
        created_at=1,
        started_at=None,
        completed_at=None,
        workspace_kind="worktree",
        workspace_path=f"/srv/shared/remote-app/.worktrees/{task_id}",
        branch_name=f"remote-app/{task_id}-remote",
        project_id="p_remote",
        claim_lock="lock",
        claim_expires=None,
        tenant=None,
        current_run_id=7,
    )


def test_default_spawn_uses_neutral_controller_cwd_for_backend_path(home, monkeypatch):
    _remote_board()
    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: ["hermes"])
    real_isdir = os.path.isdir
    def guarded_isdir(path):
        if str(path).startswith("/srv/shared"):
            raise AssertionError("controller isdir touched remote path")
        return real_isdir(path)
    monkeypatch.setattr(os.path, "isdir", guarded_isdir)
    captured = {}
    class FakeProc:
        pid = 4242
    def fake_popen(cmd, **kwargs):
        captured.update(cmd=list(cmd), **kwargs)
        return FakeProc()
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    task = _task()
    assert kbd._default_spawn(task, task.workspace_path, board="shared") == 4242
    assert captured["cwd"] == str(home)
    assert "TERMINAL_CWD" not in captured["env"]
    assert captured["env"]["HERMES_KANBAN_PROJECT_ROOT"] == "/srv/shared/remote-app"
    assert captured["env"]["HERMES_KANBAN_PROJECT_SLUG"] == "remote-app"


def test_worker_materializes_remote_worktree_in_assignee_environment(monkeypatch):
    from hermes_cli import kanban_worker_workspace as worker_ws
    task = _task()
    calls = []
    class FakeRemoteEnvironment:
        is_local = False
        def execute(self, command, **kwargs):
            calls.append((command, kwargs))
            return {
                "returncode": 0,
                "output": (
                    f"{worker_ws._ROOT_MARKER}/srv/shared/remote-app\n"
                    f"{worker_ws._WORKSPACE_MARKER}{task.workspace_path}\n"
                    f"{worker_ws._BRANCH_MARKER}{task.branch_name}\n"
                ),
            }
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("controller git must not run")))
    workspace, branch = worker_ws.materialize_project_workspace(
        task,
        {
            "project_id": "p_remote",
            "project_slug": "remote-app",
            "source_profile": "owner",
            "default_workdir": "/srv/shared/remote-app",
            "default_workspace_kind": "worktree",
            "filesystem_local": False,
        },
        FakeRemoteEnvironment(),
        profile="worker-b",
    )
    assert workspace == task.workspace_path
    assert branch == task.branch_name
    assert len(calls) == 1
    assert "git worktree add" in calls[0][0]
    assert calls[0][1]["timeout"] <= 120


def test_worker_validates_remote_directory_without_git():
    from hermes_cli import kanban_worker_workspace as worker_ws

    task = _task("t_dir")
    task.project_id = None
    task.workspace_kind = "dir"
    task.workspace_path = "/srv/shared/output"
    task.branch_name = None
    calls = []

    class RemoteDirectoryEnvironment:
        is_local = False

        def execute(self, command, **kwargs):
            calls.append((command, kwargs))
            return {
                "returncode": 0,
                "output": f"{worker_ws._ROOT_MARKER}/srv/shared/output\n",
            }

    workspace, branch = worker_ws.materialize_project_workspace(
        task,
        {
            "default_workdir": "/srv/shared/output",
            "default_workspace_kind": "dir",
            "filesystem_local": False,
        },
        RemoteDirectoryEnvironment(),
        profile="worker-b",
    )

    assert workspace == "/srv/shared/output"
    assert branch == ""
    assert "git" not in calls[0][0]


def test_worker_blocks_when_assignee_resolves_different_root():
    from hermes_cli import kanban_worker_workspace as worker_ws
    class WrongFilesystem:
        is_local = False
        def execute(self, _command, **_kwargs):
            return {
                "returncode": 0,
                "output": (
                    f"{worker_ws._ROOT_MARKER}/other/remote-app\n"
                    f"{worker_ws._WORKSPACE_MARKER}/other/worktree\n"
                    f"{worker_ws._BRANCH_MARKER}wrong\n"
                ),
            }
    with pytest.raises(ValueError, match="worker-b.*cannot use.*srv/shared/remote-app"):
        worker_ws.materialize_project_workspace(
            _task(),
            {
                "project_id": "p_remote",
                "project_slug": "remote-app",
                "default_workdir": "/srv/shared/remote-app",
                "filesystem_local": False,
            },
            WrongFilesystem(),
            profile="worker-b",
        )


def test_worker_rejects_root_alias_before_any_git_or_materialization(tmp_path, monkeypatch):
    from hermes_cli import kanban_worker_workspace as worker_ws

    real_root = tmp_path / "real"
    real_root.mkdir()
    alias_root = tmp_path / "alias"
    alias_root.symlink_to(real_root, target_is_directory=True)
    sentinel = tmp_path / "git-was-called"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git = bin_dir / "git"
    git.write_text(f"#!/bin/sh\ntouch {sentinel}\nexit 1\n", encoding="utf-8")
    git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")

    task = _task("t_alias")
    task.workspace_path = f"{alias_root}/.worktrees/{task.id}"

    class ShellEnvironment:
        is_local = False

        def execute(self, command, **_kwargs):
            completed = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                env=os.environ.copy(),
                check=False,
            )
            return {"returncode": completed.returncode, "output": completed.stdout + completed.stderr}

    with pytest.raises(ValueError, match="cannot use"):
        worker_ws.materialize_project_workspace(
            task,
            {
                "project_id": "p_remote",
                "project_slug": "remote-app",
                "default_workdir": str(alias_root),
                "filesystem_local": False,
            },
            ShellEnvironment(),
            profile="worker-b",
        )

    assert not sentinel.exists(), "Git ran before the canonical-root mismatch failed closed"
    assert not (real_root / ".worktrees").exists()


def test_failed_worker_preflight_blocks_task_with_profile_and_path(home, monkeypatch):
    from hermes_cli import kanban_worker_workspace as worker_ws
    import tools.terminal_tool as terminal_tool

    _remote_board()
    with kbc.connect(board="shared") as conn:
        task_id = kb.create_task(
            conn, title="cannot reach", assignee="worker-b", board="shared",
        )
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None

    class WrongFilesystem:
        is_local = False

        def execute(self, _command, **_kwargs):
            return {
                "returncode": 42,
                "output": f"{worker_ws._ROOT_MARKER}/different/root\n",
            }

    monkeypatch.setattr(
        terminal_tool,
        "acquire_terminal_environment",
        lambda **_kwargs: WrongFilesystem(),
        raising=False,
    )
    monkeypatch.setenv("HERMES_PROFILE", "worker-b")
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "shared")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.kanban_db_path(board="shared")))
    monkeypatch.setenv("HERMES_KANBAN_PROJECT_ROOT", "/srv/shared/remote-app")

    with pytest.raises(ValueError, match="worker-b.*srv/shared/remote-app"):
        worker_ws.prepare_project_workspace_from_env()

    with kbc.connect(board="shared") as conn:
        task = kb.get_task(conn, task_id)
        assert task.status == "blocked"
        blocked = [event for event in kb.list_events(conn, task_id) if event.kind == "blocked"][-1]
        assert "worker-b" in str(blocked.payload)
        assert "/srv/shared/remote-app" in str(blocked.payload)
