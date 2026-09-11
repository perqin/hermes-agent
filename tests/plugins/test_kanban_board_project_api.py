"""Kanban dashboard plugin: project listing + project-scoped boards.

Attaches the plugin router to a bare FastAPI app (as in
test_kanban_dashboard_plugin.py) and exercises the project surface:
GET /projects, board create/patch/list carrying project scope, and a task
on a scoped board inheriting the project.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import projects_db as pdb
from hermes_cli import kanban_project_paths


def _load_plugin_router():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("hermes_kanban_plugin_proj_test", plugin_file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.router


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def client(kanban_home):
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    return TestClient(app)


@pytest.fixture
def project(tmp_path):
    repo = tmp_path / "widget-repo"
    repo.mkdir()
    with pdb.connect_closing() as conn:
        pid = pdb.create_project(conn, name="Widget", primary_path=str(repo))
    return {"id": pid, "primary_path": str(repo)}


def test_list_projects(client, project):
    r = client.get("/api/plugins/kanban/projects")
    assert r.status_code == 200
    hit = next(p for p in r.json()["projects"] if p["id"] == project["id"])
    assert hit["name"] == "Widget"
    assert hit["primary_path"] == project["primary_path"]


def test_create_board_with_project_mirrors_workdir(client, project):
    r = client.post(
        "/api/plugins/kanban/boards",
        json={"slug": "widget", "name": "Widget", "project_id": project["id"]},
    )
    assert r.status_code == 200, r.text
    board = r.json()["board"]
    assert board["project_id"] == project["id"]
    assert board["project_name"] == "Widget"
    assert board["default_workdir"] == project["primary_path"]


def test_create_board_rejects_unknown_project(client):
    r = client.post("/api/plugins/kanban/boards", json={"slug": "bad", "project_id": "p_nope"})
    assert r.status_code == 400


def test_patch_board_set_and_clear_project(client, project):
    client.post("/api/plugins/kanban/boards", json={"slug": "widget", "name": "Widget"})

    r = client.patch("/api/plugins/kanban/boards/widget", json={"project_id": project["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["board"]["project_id"] == project["id"]

    r = client.patch("/api/plugins/kanban/boards/widget", json={"project_id": ""})
    assert r.status_code == 200
    cleared = r.json()["board"]
    assert cleared["project_id"] is None
    assert cleared["project_slug"] is None
    assert cleared["source_profile"] is None
    with pdb.connect_closing() as conn:
        assert pdb.get_project(conn, project["id"]).board_slug is None


def test_boards_list_surfaces_project(client, project):
    client.post(
        "/api/plugins/kanban/boards",
        json={"slug": "widget", "name": "Widget", "project_id": project["id"]},
    )
    widget = next(b for b in client.get("/api/plugins/kanban/boards").json()["boards"] if b["slug"] == "widget")
    assert widget["project_id"] == project["id"]
    assert widget["project_name"] == "Widget"


def test_task_on_scoped_board_inherits_project(client, project):
    client.post(
        "/api/plugins/kanban/boards",
        json={"slug": "widget", "name": "Widget", "project_id": project["id"]},
    )
    r = client.post("/api/plugins/kanban/tasks?board=widget", json={"title": "do the thing"})
    assert r.status_code == 200, r.text
    task_id = r.json()["task"]["id"]

    conn = kbc.connect(board="widget")
    try:
        assert kb.get_task(conn, task_id).project_id == project["id"]
    finally:
        conn.close()


def test_remote_workdir_is_canonicalized_without_controller_path_or_git(
    client, monkeypatch,
):
    calls = []
    monkeypatch.setattr(
        kanban_project_paths,
        "resolve_project_directory",
        lambda raw, **kwargs: calls.append((raw, kwargs)) or {
            "default_workdir": "/srv/canonical/project",
            "default_workspace_kind": "worktree",
            "filesystem_local": False,
        },
    )
    real_is_dir = Path.is_dir
    def guarded_is_dir(path):
        if str(path).startswith("/srv/"):
            raise AssertionError("controller Path.is_dir touched backend path")
        return real_is_dir(path)
    monkeypatch.setattr(Path, "is_dir", guarded_is_dir)

    response = client.post(
        "/api/plugins/kanban/boards",
        json={"slug": "remote", "default_workdir": "/srv/alias/project"},
    )

    assert response.status_code == 200, response.text
    board = response.json()["board"]
    assert board["default_workdir"] == "/srv/canonical/project"
    assert board["default_workspace_kind"] == "worktree"
    assert calls[0][0] == "/srv/alias/project"
    assert calls[0][1]["require_absolute_existing_directory"] is True


def test_remote_workdir_failure_writes_no_board_metadata(client, monkeypatch):
    monkeypatch.setattr(
        kanban_project_paths,
        "resolve_project_directory",
        lambda *_a, **_k: (_ for _ in ()).throw(ValueError(
            "Project directory is not reachable in profile 'remote'",
        )),
    )

    response = client.post(
        "/api/plugins/kanban/boards",
        json={"slug": "unreachable", "default_workdir": "/srv/missing"},
    )

    assert response.status_code == 400
    assert "not reachable" in response.json()["detail"]
    assert not kb.board_exists("unreachable")


def test_remote_board_list_uses_persisted_workspace_kind(client, monkeypatch):
    kb.create_board(
        "remote-list",
        default_workdir="/srv/project",
        default_workspace_kind="worktree",
        filesystem_local=False,
    )
    monkeypatch.setattr(
        "hermes_cli.kanban_db_workspace._git_toplevel",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("host git probe")),
    )

    response = client.get("/api/plugins/kanban/boards")

    assert response.status_code == 200, response.text
    board = next(row for row in response.json()["boards"] if row["slug"] == "remote-list")
    assert board["default_workspace_kind"] == "worktree"


def test_project_binding_derives_source_profile_and_updates_both_sides(
    client, project, monkeypatch,
):
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "dashboard-owner")
    monkeypatch.setattr(
        kanban_project_paths,
        "resolve_project_directory",
        lambda *_a, **_k: {
            "default_workdir": "/srv/canonical/widget",
            "default_workspace_kind": "worktree",
            "filesystem_local": False,
        },
    )

    response = client.post(
        "/api/plugins/kanban/boards",
        json={"slug": "bound-remote", "project_id": project["id"]},
    )

    assert response.status_code == 200, response.text
    board = response.json()["board"]
    assert board["source_profile"] == "dashboard-owner"
    assert board["default_workdir"] == "/srv/canonical/widget"
    assert board["project_slug"] == "widget"
    with pdb.connect_closing() as conn:
        assert pdb.get_project(conn, project["id"]).board_slug == "bound-remote"


def test_project_binding_rejects_conflicting_explicit_directory(
    client, project, monkeypatch,
):
    resolved = {
        project["primary_path"]: {
            "default_workdir": "/srv/project-primary",
            "default_workspace_kind": "worktree",
            "filesystem_local": False,
        },
        "/srv/explicit": {
            "default_workdir": "/srv/explicit-canonical",
            "default_workspace_kind": "dir",
            "filesystem_local": False,
        },
    }
    monkeypatch.setattr(
        kanban_project_paths,
        "resolve_project_directory",
        lambda raw, **_kwargs: resolved[raw],
    )

    response = client.post(
        "/api/plugins/kanban/boards",
        json={
            "slug": "conflicting-override",
            "project_id": project["id"],
            "default_workdir": "/srv/explicit",
        },
    )

    assert response.status_code == 400
    assert "must use its canonical primary path" in response.json()["detail"]
    assert not kb.board_exists("conflicting-override")
    with pdb.connect_closing() as conn:
        assert pdb.get_project(conn, project["id"]).board_slug is None


def test_idempotent_create_restores_existing_board_if_project_link_fails(
    client, project, monkeypatch,
):
    kb.create_board("existing", name="Original", description="keep me")
    before = kb.board_metadata_path("existing").read_bytes()
    monkeypatch.setattr(pdb, "update_project", lambda *_a, **_k: False)

    response = client.post(
        "/api/plugins/kanban/boards",
        json={"slug": "existing", "name": "Changed", "project_id": project["id"]},
    )

    assert response.status_code == 400
    assert kb.board_exists("existing")
    assert kb.board_metadata_path("existing").read_bytes() == before


def test_patch_restores_board_if_project_link_fails(client, project, monkeypatch):
    kb.create_board("patch-rollback", name="Original", description="keep me")
    before = kb.board_metadata_path("patch-rollback").read_bytes()
    monkeypatch.setattr(pdb, "update_project", lambda *_a, **_k: False)

    response = client.patch(
        "/api/plugins/kanban/boards/patch-rollback",
        json={"name": "Changed", "project_id": project["id"]},
    )

    assert response.status_code == 400
    assert kb.board_metadata_path("patch-rollback").read_bytes() == before
