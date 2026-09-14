"""Project-bound Kanban workspaces stay in the assignee backend namespace."""

from __future__ import annotations

import os
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

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


def test_source_task_backend_path_is_parsed_without_controller_pathlib(monkeypatch):
    from hermes_cli import projects_db as pdb

    source = SimpleNamespace(
        id="t_source",
        project_id="p_remote",
        workspace_kind="worktree",
        workspace_path=r"C:\repo\.worktrees\t_source",
        branch_name="remote-app/t_source",
    )
    monkeypatch.setattr(kb, "get_task", lambda _conn, _task_id: source)

    project, repo = kb._project_from_source_task(
        object(), pdb, "p_remote", "t_source",
    )

    assert repo == r"C:\repo"
    assert project.primary_path == r"C:\repo"


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


def test_controller_completion_does_not_cleanup_backend_worktree_on_host(home, monkeypatch):
    _remote_board()
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "shared")
    with kbc.connect(board="shared") as conn:
        task_id = kb.create_task(
            conn,
            title="remote cleanup",
            assignee="worker-b",
            board="shared",
        )
        assert kb.claim_task(conn, task_id) is not None
        host_cleanup = []
        monkeypatch.setattr(
            kbw,
            "_cleanup_worktree_workspace",
            lambda *_args, **_kwargs: host_cleanup.append((_args, _kwargs)),
        )

        kb.complete_task(conn, task_id)

    assert host_cleanup == []


@pytest.mark.parametrize("project_id", [None, "p_remote"])
def test_legacy_cleanup_classification_never_reads_mutable_board_metadata(monkeypatch, project_id):
    task = SimpleNamespace(project_id=project_id)
    monkeypatch.setattr(kb, "get_task", lambda _conn, _task_id: task)
    monkeypatch.setattr(
        kb,
        "read_board_metadata",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("metadata unavailable")),
    )

    binding = kbw._backend_worktree_binding(
        object(), "t_remote", "/srv/shared/repo/.worktrees/t_remote",
    )

    if project_id is None:
        assert binding is None
    else:
        bound_task, meta = binding
        assert bound_task is task
        assert meta["default_workdir"] == "/srv/shared/repo"
        assert meta["filesystem_local"] is None


@pytest.mark.parametrize("source_filesystem_local", [False, True])
def test_worker_completion_cleans_backend_worktree_in_cached_environment(
    home, monkeypatch, source_filesystem_local,
):
    from agent import delegation_context
    from hermes_cli import kanban_worker_workspace as worker_ws
    import tools.terminal_tool as terminal_tool

    kb.create_board(
        "shared",
        project_id="p_remote",
        project_slug="remote-app",
        source_profile="owner",
        default_workdir="/srv/shared/remote-app",
        default_workspace_kind="worktree",
        filesystem_local=source_filesystem_local,
    )
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "shared")
    with kbc.connect(board="shared") as conn:
        task_id = kb.create_task(
            conn,
            title="remote cleanup",
            assignee="worker-b",
            board="shared",
        )
        assert kb.claim_task(conn, task_id) is not None
        monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
        monkeypatch.setattr(delegation_context, "is_dispatcher_owned_worker_context", lambda: True)
        remote_env = SimpleNamespace(is_local=False)
        monkeypatch.setattr(
            terminal_tool,
            "acquire_terminal_environment",
            lambda **kwargs: remote_env,
        )
        backend_cleanup = []
        monkeypatch.setattr(
            worker_ws,
            "cleanup_project_worktree",
            lambda task, meta, env, **kwargs: backend_cleanup.append(
                (task.id, meta["default_workdir"], env, kwargs["profile"])
            ) or True,
        )
        monkeypatch.setattr(
            kbw,
            "_cleanup_worktree_workspace",
            lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("host cleanup ran")),
        )

        kb.complete_task(conn, task_id)

    assert backend_cleanup == [
        (task_id, "/srv/shared/remote-app", remote_env, "worker-b")
    ]


def test_worker_child_completion_never_host_cleans_deferred_backend_parent(home, monkeypatch):
    from agent import delegation_context
    from hermes_cli import kanban_worker_workspace as worker_ws
    import tools.terminal_tool as terminal_tool

    _remote_board()
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "shared")
    with kbc.connect(board="shared") as conn:
        parent_id = kb.create_task(
            conn, title="parent", assignee="worker-b", board="shared",
        )
        child_id = kb.create_task(
            conn, title="child", assignee="worker-b", board="shared", parents=[parent_id],
        )
        assert kb.claim_task(conn, parent_id) is not None
        kb.complete_task(conn, parent_id)
        assert kb.claim_task(conn, child_id) is not None

        monkeypatch.setenv("HERMES_KANBAN_TASK", child_id)
        monkeypatch.setattr(delegation_context, "is_dispatcher_owned_worker_context", lambda: True)
        monkeypatch.setattr(
            terminal_tool,
            "acquire_terminal_environment",
            lambda **_kwargs: SimpleNamespace(is_local=False),
        )
        monkeypatch.setattr(worker_ws, "cleanup_project_worktree", lambda *_a, **_k: True)
        host_cleanup = []
        monkeypatch.setattr(
            kbw,
            "_cleanup_worktree_workspace",
            lambda *args, **kwargs: host_cleanup.append((args, kwargs)),
        )

        kb.complete_task(conn, child_id)

    assert host_cleanup == []


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


def test_rebound_board_keeps_existing_task_backend_workspace_provenance(home, monkeypatch):
    _remote_board()
    with kbc.connect(board="shared") as conn:
        task_id = kb.create_task(
            conn, title="old binding", assignee="worker-b", board="shared",
        )
        task = kb.get_task(conn, task_id)
        kb.write_board_metadata(
            "shared",
            project_id="p_new",
            project_slug="new-project",
            default_workdir="/srv/shared/new-project",
            default_workspace_kind="worktree",
            filesystem_local=False,
        )
        monkeypatch.setattr(
            kbw,
            "_resolve_worktree_workspace",
            lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("controller touched old backend path")),
        )
        spawned = []
        result = kbd.dispatch_once(
            conn,
            board="shared",
            spawn_fn=lambda task, workspace, board=None: spawned.append(workspace) or 4242,
            max_spawn=1,
        )
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "shared")
        host_cleanup = []
        monkeypatch.setattr(
            kbw,
            "_cleanup_worktree_workspace",
            lambda *args, **kwargs: host_cleanup.append((args, kwargs)),
        )
        kb.complete_task(conn, task_id)

    assert [row[0] for row in result.spawned] == [task_id]
    assert spawned == [task.workspace_path]
    assert host_cleanup == []


def test_worker_preflight_uses_task_snapshot_after_board_rebind(home, monkeypatch):
    from hermes_cli import kanban_worker_workspace as worker_ws
    import tools.terminal_tool as terminal_tool

    _remote_board()
    with kbc.connect(board="shared") as conn:
        task_id = kb.create_task(
            conn, title="old binding", assignee="worker-b", board="shared",
        )
        task = kb.get_task(conn, task_id)
        assert kb.claim_task(conn, task_id) is not None
    kb.write_board_metadata(
        "shared",
        project_id="p_new",
        project_slug="new-project",
        default_workdir="/srv/shared/new-project",
        default_workspace_kind="worktree",
        filesystem_local=False,
    )

    class SnapshotEnvironment:
        is_local = False

        def execute(self, _command, **_kwargs):
            return {
                "returncode": 0,
                "output": (
                    f"{worker_ws._ROOT_MARKER}/srv/shared/remote-app\n"
                    f"{worker_ws._WORKSPACE_MARKER}{task.workspace_path}\n"
                    f"{worker_ws._BRANCH_MARKER}{task.branch_name}\n"
                ),
            }

    monkeypatch.setattr(
        terminal_tool,
        "acquire_terminal_environment",
        lambda **_kwargs: SnapshotEnvironment(),
    )
    monkeypatch.setenv("HERMES_PROFILE", "worker-b")
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "shared")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.kanban_db_path(board="shared")))
    monkeypatch.setenv("HERMES_KANBAN_PROJECT_ROOT", "/srv/shared/remote-app")
    monkeypatch.setenv("HERMES_KANBAN_PROJECT_SLUG", "remote-app")

    assert worker_ws.prepare_project_workspace_from_env() == task.workspace_path


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
            workspace_path="/srv/other/output",
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
    assert spawned == ["/srv/other/output"]


def test_nonproject_remote_git_board_is_materialized_by_worker(home, monkeypatch):
    kb.create_board(
        "remote-git",
        default_workdir="/srv/shared/repo",
        default_workspace_kind="worktree",
        filesystem_local=False,
    )
    with kbc.connect(board="remote-git") as conn:
        task_id = kb.create_task(
            conn,
            title="remote git",
            assignee="worker-b",
            board="remote-git",
            workspace_kind="worktree",
        )
        task = kb.get_task(conn, task_id)
        monkeypatch.setattr(
            kbw,
            "_resolve_worktree_workspace",
            lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("controller git touched remote path")),
        )
        spawned = []
        result = kbd.dispatch_once(
            conn,
            board="remote-git",
            spawn_fn=lambda task, workspace, board=None: spawned.append(workspace) or 4242,
            max_spawn=1,
        )

    assert task.workspace_path == f"/srv/shared/repo/.worktrees/{task_id}"
    assert task.branch_name == f"wt/{task_id}"
    assert [row[0] for row in result.spawned] == [task_id]
    assert spawned == [task.workspace_path]


def test_nonproject_remote_explicit_worktree_never_reaches_controller_git(home, monkeypatch):
    kb.create_board(
        "remote-git-explicit",
        default_workdir="/srv/shared/repo",
        default_workspace_kind="worktree",
        filesystem_local=False,
    )
    with kbc.connect(board="remote-git-explicit") as conn:
        task_id = kb.create_task(
            conn,
            title="explicit remote git",
            assignee="worker-b",
            board="remote-git-explicit",
            workspace_kind="worktree",
            workspace_path="/other/backend/worktree",
            branch_name="custom",
        )
        monkeypatch.setattr(
            kbw,
            "_resolve_worktree_workspace",
            lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("controller git touched remote path")),
        )
        spawned = []
        result = kbd.dispatch_once(
            conn,
            board="remote-git-explicit",
            spawn_fn=lambda task, workspace, board=None: spawned.append(workspace) or 4242,
            max_spawn=1,
        )

    assert [row[0] for row in result.spawned] == [task_id]
    assert spawned == [f"/other/backend/worktree/.worktrees/{task_id}"]


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
    command = calls[0][0]
    assert command.startswith("(\nset -eu")
    assert command.endswith("\n)")
    assert "git worktree add" in command
    assert calls[0][1]["timeout"] <= 120


def test_worker_cleans_safe_remote_worktree_in_assignee_environment():
    from hermes_cli import kanban_worker_workspace as worker_ws

    task = _task()
    calls = []

    class FakeRemoteEnvironment:
        is_local = False

        def execute(self, command, **kwargs):
            calls.append((command, kwargs))
            return {
                "returncode": 0,
                "output": f"{worker_ws._CLEANUP_MARKER}removed\n",
            }

    removed = worker_ws.cleanup_project_worktree(
        task,
        {"default_workdir": "/srv/shared/remote-app"},
        FakeRemoteEnvironment(),
        profile="worker-b",
    )

    assert removed is True
    command = calls[0][0]
    assert "status_output=$(git -C" in command
    assert "status --porcelain --untracked-files=normal) || exit 43" in command
    assert "remote_refs=$(git -C" in command
    assert 'actual_branch=$(git -C "$actual_target" branch --show-current)' in command
    assert 'branch_tip=$(git -C "$actual_root" rev-parse --verify "refs/heads/$branch")' in command
    assert 'branch -r --contains "$branch_tip") || exit 43' in command
    assert "worktree remove" in command
    assert calls[0][1]["rewrite_compound_background"] is False


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
                "output": (
                    f"{worker_ws._ROOT_MARKER}/srv/shared/output\n"
                    f"{worker_ws._WORKSPACE_MARKER}/srv/shared/output\n"
                ),
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


def test_worker_rejects_project_dir_when_provenance_root_resolves_elsewhere():
    from hermes_cli import kanban_worker_workspace as worker_ws

    task = _task("t_project_dir")
    task.workspace_kind = "dir"
    task.workspace_path = "/srv/shared/project/subdir"
    task.branch_name = None

    class WrongRootEnvironment:
        is_local = False

        def execute(self, _command, **_kwargs):
            return {
                "returncode": 0,
                "output": (
                    f"{worker_ws._ROOT_MARKER}/other/project\n"
                    f"{worker_ws._WORKSPACE_MARKER}/srv/shared/project/subdir\n"
                ),
            }

    with pytest.raises(ValueError, match="cannot use"):
        worker_ws.materialize_project_workspace(
            task,
            {"default_workdir": "/srv/shared/project"},
            WrongRootEnvironment(),
            profile="worker-b",
        )


def test_worker_accepts_project_subdirectory_only_after_root_and_target_match():
    from hermes_cli import kanban_worker_workspace as worker_ws

    task = _task("t_project_subdir")
    task.workspace_kind = "dir"
    task.workspace_path = "/srv/shared/project/subdir"
    task.branch_name = None

    class MatchingEnvironment:
        is_local = False

        def execute(self, _command, **_kwargs):
            return {
                "returncode": 0,
                "output": (
                    f"{worker_ws._ROOT_MARKER}/srv/shared/project\n"
                    f"{worker_ws._WORKSPACE_MARKER}/srv/shared/project/subdir\n"
                ),
            }

    assert worker_ws.materialize_project_workspace(
        task,
        {"default_workdir": "/srv/shared/project"},
        MatchingEnvironment(),
        profile="worker-b",
    ) == ("/srv/shared/project/subdir", "")


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

    acquired_with = []
    monkeypatch.setattr(
        terminal_tool,
        "acquire_terminal_environment",
        lambda **kwargs: acquired_with.append(kwargs) or WrongFilesystem(),
        raising=False,
    )
    monkeypatch.setenv("HERMES_PROFILE", "worker-b")
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "shared")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.kanban_db_path(board="shared")))
    monkeypatch.setenv("HERMES_KANBAN_PROJECT_ROOT", "/srv/shared/remote-app")

    with pytest.raises(ValueError, match="worker-b.*srv/shared/remote-app"):
        worker_ws.prepare_project_workspace_from_env()

    assert acquired_with == [{"task_id": task_id}]

    with kbc.connect(board="shared") as conn:
        task = kb.get_task(conn, task_id)
        assert task.status == "blocked"
        blocked = [event for event in kb.list_events(conn, task_id) if event.kind == "blocked"][-1]
        assert "worker-b" in str(blocked.payload)
        assert "/srv/shared/remote-app" in str(blocked.payload)


def test_worker_preflight_makes_workspace_authoritative_for_first_file_operation_without_shared_env_leak(
    home, monkeypatch,
):
    from hermes_cli import kanban_worker_workspace as worker_ws
    import tools.file_tools as file_tools
    import tools.terminal_tool as terminal_tool

    _remote_board()
    with kbc.connect(board="shared") as conn:
        task_id = kb.create_task(
            conn, title="write first file", assignee="worker-b", board="shared",
        )
        task = kb.get_task(conn, task_id)
        assert kb.claim_task(conn, task_id) is not None

    calls = []

    class SharedEnvironment:
        is_local = False
        cwd = "/profile-default"

        def execute(self, command, **kwargs):
            calls.append((command, kwargs))
            if worker_ws._WORKSPACE_MARKER in command:
                return {
                    "returncode": 0,
                    "output": (
                        f"{worker_ws._ROOT_MARKER}/srv/shared/remote-app\n"
                        f"{worker_ws._WORKSPACE_MARKER}{task.workspace_path}\n"
                        f"{worker_ws._BRANCH_MARKER}{task.branch_name}\n"
                    ),
                }
            return {"returncode": 0, "output": ""}

    shared = SharedEnvironment()
    monkeypatch.setattr(terminal_tool, "acquire_terminal_environment", lambda **_kwargs: shared)
    monkeypatch.setattr(terminal_tool, "_active_environments", {"default": shared})
    monkeypatch.setattr(file_tools, "_file_ops_cache", {})
    monkeypatch.setenv("HERMES_PROFILE", "worker-b")
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "shared")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.kanban_db_path(board="shared")))
    monkeypatch.setenv("HERMES_KANBAN_BACKEND_ROOT", "/srv/shared/remote-app")

    assert worker_ws.prepare_project_workspace_from_env() == task.workspace_path
    assert shared.cwd == "/profile-default"

    task_ops = file_tools._get_file_ops(task_id)
    task_ops._exec(":")
    assert calls[-1][1]["cwd"] == task.workspace_path

    other_ops = file_tools._get_file_ops("t_other")
    other_ops._exec(":")
    assert calls[-1][1]["cwd"] == "/profile-default"
    assert other_ops is not task_ops


def test_project_dir_task_persists_immutable_workspace_provenance(home):
    _remote_board()
    with kbc.connect(board="shared") as conn:
        task_id = kb.create_task(
            conn,
            title="use project directory",
            assignee="worker-b",
            board="shared",
            workspace_kind="dir",
            workspace_path="/srv/shared/remote-app/subdir",
        )
        task = kb.get_task(conn, task_id)

    assert task.workspace_root == "/srv/shared/remote-app"
    assert task.workspace_requires_preflight is True
    assert task.workspace_filesystem_local is False
    assert task.workspace_source_profile == "owner"
    assert task.workspace_project_slug == "remote-app"


@pytest.mark.parametrize("root", ["/srv/project ", "/srv/project\\"])
def test_backend_workspace_provenance_preserves_canonical_suffix(home, root):
    kb.create_board(
        "spaced",
        project_id="p_space",
        project_slug="space",
        source_profile="owner",
        default_workdir=root,
        default_workspace_kind="worktree",
        filesystem_local=False,
    )
    with kbc.connect(board="spaced") as conn:
        task_id = kb.create_task(
            conn,
            title="preserve backend path",
            assignee="worker-b",
            board="spaced",
        )
        task = kb.get_task(conn, task_id)

    assert task.workspace_root == root
    assert task.workspace_path == f"{root}/.worktrees/{task_id}"


def test_direct_remote_worktree_override_is_repo_root_and_persists_provenance(home):
    kb.create_board(
        "remote-git-override",
        default_workdir="/srv/shared/repo",
        default_workspace_kind="worktree",
        filesystem_local=False,
        source_profile="dashboard-owner",
    )
    with kbc.connect(board="remote-git-override") as conn:
        task_id = kb.create_task(
            conn,
            title="other remote repo",
            assignee="worker-b",
            board="remote-git-override",
            workspace_kind="worktree",
            workspace_path="/other/repo",
        )
        task = kb.get_task(conn, task_id)

    assert task.workspace_path == f"/other/repo/.worktrees/{task_id}"
    assert task.workspace_root == "/other/repo"
    assert task.workspace_requires_preflight is True
    assert task.workspace_filesystem_local is False
    assert task.workspace_source_profile == "dashboard-owner"


def test_dispatch_and_cleanup_use_task_provenance_after_current_board_changes(
    home, monkeypatch,
):
    from agent import delegation_context
    from hermes_cli import kanban_worker_workspace as worker_ws
    import tools.terminal_tool as terminal_tool

    _remote_board()
    kb.create_board(
        "other",
        default_workdir="/host/local/other",
        default_workspace_kind="worktree",
        filesystem_local=True,
    )
    with kbc.connect(board="shared") as conn:
        task_id = kb.create_task(
            conn, title="immutable provenance", assignee="worker-b", board="shared",
        )
        original = kb.get_task(conn, task_id)
        assert kb.claim_task(conn, task_id) is not None
        kb.write_board_metadata("shared", clear_project_binding=True)
        kb.set_current_board("other")

        monkeypatch.setattr(
            kb,
            "read_board_metadata",
            lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("Board metadata consulted")),
        )
        monkeypatch.setattr(
            kb,
            "get_current_board",
            lambda: (_ for _ in ()).throw(AssertionError("current Board consulted")),
        )
        binding = kbd._backend_owned_project_binding(kb.get_task(conn, task_id), "shared")
        assert binding["default_workdir"] == "/srv/shared/remote-app"

        monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
        monkeypatch.setattr(delegation_context, "is_dispatcher_owned_worker_context", lambda: True)
        remote_env = SimpleNamespace(is_local=False)
        monkeypatch.setattr(terminal_tool, "acquire_terminal_environment", lambda **_k: remote_env)
        cleaned = []
        monkeypatch.setattr(
            worker_ws,
            "cleanup_project_worktree",
            lambda task, meta, env, **_k: cleaned.append((task.id, meta["default_workdir"], env)) or True,
        )
        monkeypatch.setattr(
            kbw,
            "_cleanup_worktree_workspace",
            lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("host cleanup ran")),
        )

        kbw._cleanup_workspace(conn, task_id)
    assert cleaned == [(task_id, "/srv/shared/remote-app", remote_env)]


def test_workspace_provenance_columns_are_added_to_legacy_database(tmp_path, monkeypatch):
    import sqlite3

    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE tasks ("
        "id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT, assignee TEXT, "
        "status TEXT NOT NULL, priority INTEGER DEFAULT 0, created_by TEXT, "
        "created_at INTEGER NOT NULL, started_at INTEGER, completed_at INTEGER, "
        "workspace_kind TEXT NOT NULL DEFAULT 'scratch', workspace_path TEXT, "
        "claim_lock TEXT, claim_expires INTEGER)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))

    with kbc.connect() as migrated:
        columns = {row["name"] for row in migrated.execute("PRAGMA table_info(tasks)")}

    assert {
        "workspace_root",
        "workspace_requires_preflight",
        "workspace_filesystem_local",
        "workspace_source_profile",
        "workspace_project_slug",
    } <= columns


def test_remote_cleanup_preserves_switched_worktree_and_unpushed_stored_branch(tmp_path):
    from hermes_cli import kanban_worker_workspace as worker_ws

    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    target = repo / ".worktrees" / "t_cleanup"

    def git(*args, cwd=None):
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        )

    git("init", "--bare", str(remote))
    git("init", str(repo))
    git("config", "user.name", "Test", cwd=repo)
    git("config", "user.email", "test@example.com", cwd=repo)
    (repo / "README").write_text("base\n", encoding="utf-8")
    git("add", "README", cwd=repo)
    git("commit", "-m", "base", cwd=repo)
    git("branch", "-M", "main", cwd=repo)
    git("remote", "add", "origin", str(remote), cwd=repo)
    git("push", "-u", "origin", "main", cwd=repo)
    git("branch", "published", cwd=repo)
    git("push", "origin", "published", cwd=repo)
    git("worktree", "add", "-b", "wt/t_cleanup", str(target), cwd=repo)
    (target / "README").write_text("unpushed\n", encoding="utf-8")
    git("add", "README", cwd=target)
    git("commit", "-m", "unpushed", cwd=target)
    git("switch", "published", cwd=target)

    completed = subprocess.run(
        worker_ws._remote_cleanup_command(
            str(repo.resolve()), str(target.resolve()), "wt/t_cleanup",
        ),
        shell=True,
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert f"{worker_ws._CLEANUP_MARKER}preserved" in completed.stdout
    assert target.is_dir()
    assert git("show-ref", "--verify", "refs/heads/wt/t_cleanup", cwd=repo).returncode == 0


def test_remote_materialization_rejects_unrelated_repo_at_expected_worktree_path(tmp_path):
    from hermes_cli import kanban_worker_workspace as worker_ws

    root = tmp_path / "root"
    target = root / ".worktrees" / "t_collision"
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    target.mkdir(parents=True)
    subprocess.run(["git", "init", str(target)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(target), "checkout", "-b", "wt/t_collision"],
        check=True,
        capture_output=True,
    )

    task = _task("t_collision")
    task.project_id = None
    task.workspace_path = str(target.resolve())
    task.branch_name = "wt/t_collision"
    task.workspace_root = str(root.resolve())
    task.workspace_requires_preflight = True

    class ShellEnvironment:
        is_local = False

        def execute(self, command, **_kwargs):
            completed = subprocess.run(
                command, shell=True, capture_output=True, text=True, check=False,
            )
            return {
                "returncode": completed.returncode,
                "output": completed.stdout + completed.stderr,
            }

    with pytest.raises(ValueError, match="cannot use"):
        worker_ws.materialize_project_workspace(
            task,
            {"default_workdir": str(root.resolve())},
            ShellEnvironment(),
            profile="worker-b",
        )

    assert target.is_dir()


def test_legacy_project_dir_task_falls_back_to_its_immutable_path_without_board_lookup(monkeypatch):
    task = SimpleNamespace(
        id="t_legacy_dir",
        project_id="p_legacy",
        workspace_kind="dir",
        workspace_path="/backend/project/subdir",
        workspace_root=None,
        workspace_requires_preflight=False,
        workspace_filesystem_local=None,
        workspace_source_profile=None,
        workspace_project_slug=None,
    )
    monkeypatch.setattr(
        kb,
        "read_board_metadata",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("Board metadata consulted")),
    )
    monkeypatch.setattr(
        kb,
        "get_current_board",
        lambda: (_ for _ in ()).throw(AssertionError("current Board consulted")),
    )

    provenance = kbd._backend_owned_project_binding(task, "rebound")

    assert provenance == {
        "project_id": "p_legacy",
        "project_slug": None,
        "default_workdir": "/backend/project/subdir",
        "default_workspace_kind": "dir",
        "filesystem_local": None,
    }


@pytest.mark.parametrize("malformed", ["false", 1, None])
def test_malformed_persisted_locality_stays_unknown_and_requires_backend_preflight(
    home, malformed,
):
    kb.create_board(
        "malformed",
        project_id="p_remote",
        project_slug="remote-app",
        source_profile="owner",
        default_workdir="/srv/shared/remote-app",
        default_workspace_kind="worktree",
        filesystem_local=False,
    )
    metadata_path = kb.board_metadata_path("malformed")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["filesystem_local"] = malformed
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with kbc.connect(board="malformed") as conn:
        task_id = kb.create_task(
            conn, title="strict locality", assignee="worker-b", board="malformed")
        task = kb.get_task(conn, task_id)
        assert task.workspace_requires_preflight is True
        assert task.workspace_filesystem_local is None
        conn.execute(
            "UPDATE tasks SET workspace_filesystem_local = 'false' WHERE id = ?", (task_id,))
        assert kb.get_task(conn, task_id).workspace_filesystem_local is None
