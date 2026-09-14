"""Regression boundaries for backend path namespaces and board capabilities."""

import pytest

from hermes_cli.kanban_project_paths import join_backend_path
from hermes_cli.project_paths import path_owns
from hermes_cli import kanban_db as kb


@pytest.mark.parametrize("contents", [None, "{bad", "[]", '"text"', b"\xff", "unreadable"])
def test_failed_board_metadata_keeps_locality_unknown(tmp_path, monkeypatch, contents):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = kb.board_metadata_path("default")
    path.parent.mkdir(parents=True, exist_ok=True)
    if contents == "unreadable":
        path.mkdir()
    elif isinstance(contents, bytes):
        path.write_bytes(contents)
    elif contents is not None:
        path.write_text(contents)
    assert kb.read_board_metadata("default")["filesystem_local"] is None


@pytest.mark.parametrize("metadata", [None, '{"name": "Legacy"}', '{"filesystem_local": true}'])
def test_explicit_project_does_not_inherit_unbound_board_locality(tmp_path, monkeypatch, metadata):
    from hermes_cli import projects_db as pdb
    from hermes_cli import kanban_db_connect as kbc

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    if metadata is not None:
        path = kb.board_metadata_path("default")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(metadata)
    with pdb.connect_closing() as conn:
        pid = pdb.create_project(conn, name="Remote", folders=["/backend/project"])
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="Explicit project", project_id=pid)
        task = kb.get_task(conn, tid)
    assert task.workspace_requires_preflight is True
    assert task.workspace_filesystem_local is None


def test_verified_legacy_unbound_board_remains_local(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = kb.board_metadata_path("default")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"name": "Legacy board"}')
    assert kb.read_board_metadata("default")["filesystem_local"] is True


@pytest.mark.parametrize("root,child,expected", [
    ("//", "/other/repo", False),
    ("/", "//other/repo", False),
    ("//", "//other/repo", True),
    ("/", "/other/repo", True),
])
def test_ownership_preserves_posix_root_namespace(root, child, expected):
    assert path_owns(root, child) is expected


def test_join_preserves_posix_double_slash_namespace():
    assert join_backend_path("//srv/repo", ".worktrees", "task") == "//srv/repo/.worktrees/task"
