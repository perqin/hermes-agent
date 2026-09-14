"""Kanban <-> Projects integration: project-linked tasks get a deterministic
worktree path + branch instead of the random ``wt/<task-id>`` fallback."""

from __future__ import annotations

import os
from argparse import Namespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import projects_db as pdb
from hermes_cli import projects_cmd


@pytest.fixture
def kanban_conn(tmp_path):
    c = kbc.connect(db_path=tmp_path / "kanban.db")
    try:
        yield c
    finally:
        c.close()


def _make_project(name="Web App", repo="/tmp/webapp"):
    with pdb.connect_closing() as pc:
        pid = pdb.create_project(pc, name=name, folders=[repo])
        return pdb.get_project(pc, pid)


def test_project_linked_task_gets_deterministic_worktree_and_branch(kanban_conn):
    proj = _make_project()
    tid = kb.create_task(kanban_conn, title="Add login", project_id=proj.slug)
    task = kb.get_task(kanban_conn, tid)

    assert task.project_id == proj.id
    assert task.workspace_kind == "worktree"
    # Worktree dir anchored under the project's primary repo, keyed on task id.
    assert task.workspace_path == os.path.join(proj.primary_path, ".worktrees", tid)
    # Deterministic branch: <slug>/<task-id>-<title-slug>. NOT a random wt/...
    assert task.branch_name == f"{proj.slug}/{tid}-add-login"
    assert not task.branch_name.startswith("wt/")


def test_explicit_branch_overrides_project_default(kanban_conn):
    proj = _make_project()
    tid = kb.create_task(
        kanban_conn,
        title="x",
        project_id=proj.slug,
        workspace_kind="worktree",
        branch_name="feature/custom",
    )
    task = kb.get_task(kanban_conn, tid)
    assert task.branch_name == "feature/custom"


def test_unlinked_task_unchanged(kanban_conn):
    tid = kb.create_task(kanban_conn, title="plain")
    task = kb.get_task(kanban_conn, tid)

    assert task.project_id is None
    assert task.workspace_kind == "scratch"
    # No branch is persisted — the worker still owns the wt/<id> fallback for
    # genuinely ad-hoc worktree tasks, but unlinked scratch tasks have none.
    assert task.branch_name is None


def test_bound_board_snapshot_is_self_contained_for_another_profile(
    kanban_conn, monkeypatch,
):
    """A worker profile does not need the owner's Project row to inherit it."""
    proj = _make_project(repo="/remote/shared/webapp")
    kb.write_board_metadata(
        "shared",
        project_id=proj.id,
        project_slug=proj.slug,
        source_profile="owner",
        default_workdir=proj.primary_path,
        default_workspace_kind="worktree",
        filesystem_local=False,
    )

    # Simulate task creation under an assignee profile with an empty projects.db.
    monkeypatch.setattr(pdb, "get_project", lambda *_args, **_kwargs: None)
    tid = kb.create_task(
        kanban_conn,
        title="Remote fix",
        assignee="worker-b",
        board="shared",
    )
    task = kb.get_task(kanban_conn, tid)

    assert task.project_id == proj.id
    assert task.workspace_kind == "worktree"
    assert task.workspace_path == f"{proj.primary_path}/.worktrees/{tid}"
    assert task.branch_name == f"{proj.slug}/{tid}-remote-fix"


def test_explicit_unknown_project_does_not_fall_back_to_board_snapshot(
    kanban_conn,
):
    kb.write_board_metadata(
        "shared",
        project_id="p_bound",
        project_slug="bound-project",
        source_profile="owner",
        default_workdir="/remote/shared/bound",
        default_workspace_kind="worktree",
        filesystem_local=False,
    )

    with pytest.raises(ValueError, match="explicit project .* does not exist"):
        kb.create_task(
            kanban_conn,
            title="must fail closed",
            board="shared",
            project_id="missing-project",
        )


def test_bind_board_snapshots_canonical_project_contract(monkeypatch):
    proj = _make_project(repo="/remote/alias/webapp")
    kb.create_board("shared")
    monkeypatch.setattr(
        projects_cmd,
        "_project_binding_snapshot",
        lambda _proj: {
            "default_workdir": "/remote/canonical/webapp",
            "default_workspace_kind": "worktree",
            "filesystem_local": False,
        },
    )
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "owner")

    rc = projects_cmd.projects_command(Namespace(
        project_action="bind-board", project=proj.slug, board="shared",
    ))

    assert rc == 0
    bound = kb.read_board_metadata("shared")
    assert bound["project_id"] == proj.id
    assert bound["project_slug"] == proj.slug
    assert bound["source_profile"] == "owner"
    assert bound["default_workdir"] == "/remote/canonical/webapp"
    assert bound["default_workspace_kind"] == "worktree"
    assert bound["filesystem_local"] is False
    with pdb.connect_closing() as conn:
        assert pdb.get_project(conn, proj.id).board_slug == "shared"


def test_bind_board_failure_does_not_leave_one_sided_project_link(monkeypatch):
    proj = _make_project(repo="/tmp/webapp-bind-failure")
    kb.create_board("shared")
    monkeypatch.setattr(
        projects_cmd,
        "_project_binding_snapshot",
        lambda _proj: {
            "default_workdir": _proj.primary_path,
            "default_workspace_kind": "dir",
            "filesystem_local": True,
        },
    )
    monkeypatch.setattr(kb, "write_board_metadata", lambda *_a, **_k: (_ for _ in ()).throw(OSError("disk full")))

    rc = projects_cmd.projects_command(Namespace(
        project_action="bind-board", project=proj.slug, board="shared",
    ))

    assert rc == 2
    with pdb.connect_closing() as conn:
        assert pdb.get_project(conn, proj.id).board_slug is None


def test_rebinding_project_clears_its_previous_board_snapshot(monkeypatch):
    proj = _make_project(repo="/tmp/rebind-project")
    kb.create_board("board-a")
    kb.create_board("board-b")
    monkeypatch.setattr(
        projects_cmd,
        "_project_binding_snapshot",
        lambda _proj: {
            "default_workdir": _proj.primary_path,
            "default_workspace_kind": "dir",
            "filesystem_local": True,
        },
    )
    for board in ("board-a", "board-b"):
        assert projects_cmd.projects_command(Namespace(
            project_action="bind-board", project=proj.slug, board=board,
        )) == 0

    assert kb.read_board_metadata("board-a")["project_id"] is None
    assert kb.read_board_metadata("board-b")["project_id"] == proj.id


def test_bind_rejects_board_owned_by_another_project(monkeypatch):
    proj = _make_project(repo="/tmp/conflict-project")
    kb.create_board(
        "occupied",
        project_id="p_other",
        project_slug="other",
        source_profile="other-owner",
        default_workdir="/tmp/other",
        default_workspace_kind="dir",
        filesystem_local=True,
    )
    monkeypatch.setattr(
        projects_cmd,
        "_project_binding_snapshot",
        lambda _proj: {
            "default_workdir": _proj.primary_path,
            "default_workspace_kind": "dir",
            "filesystem_local": True,
        },
    )

    rc = projects_cmd.projects_command(Namespace(
        project_action="bind-board", project=proj.slug, board="occupied",
    ))

    assert rc == 2
    assert kb.read_board_metadata("occupied")["project_id"] == "p_other"
    with pdb.connect_closing() as conn:
        assert pdb.get_project(conn, proj.id).board_slug is None


def test_unbind_reports_metadata_rollback_failure(monkeypatch):
    from hermes_cli import kanban_project_binding as binding

    proj = _make_project(repo="/tmp/unbind-rollback")
    kb.create_board("shared", project_id=proj.id, default_workdir=proj.primary_path)
    with pdb.connect_closing() as conn:
        monkeypatch.setattr(pdb, "update_project", lambda *_a, **_k: False)
        monkeypatch.setattr(
            binding,
            "_restore_metadata",
            lambda *_a, **_k: (_ for _ in ()).throw(OSError("restore failed")),
        )

        with pytest.raises(ValueError, match="metadata rollback failed"):
            binding.clear_owned_project_binding(conn, proj, "shared")


