from __future__ import annotations

import json
import os

import pytest

from hermes_cli import project_paths
from tools import project_tools


@pytest.fixture(autouse=True)
def _reset_workspace_callback():
    project_tools.set_project_workspace_callback(None)
    yield
    project_tools.set_project_workspace_callback(None)


def test_project_create_resolves_in_session_environment_and_reports_workspace_failure(monkeypatch):
    calls = []

    def resolve(path, **kwargs):
        calls.append((path, kwargs))
        return "/remote/canonical/repo"

    monkeypatch.setattr(project_paths, "resolve_project_folder", resolve)
    monkeypatch.setattr(
        os.path, "isdir",
        lambda *_a: (_ for _ in ()).throw(AssertionError("host isdir must not run")),
    )
    moved = []
    project_tools.set_project_workspace_callback(
        lambda task_id, path, name: moved.append((task_id, path, name)))

    result = json.loads(project_tools.project_create("Remote", "relative ", task_id="session-1"))

    assert result["success"] is True
    assert result["primary_path"] == "/remote/canonical/repo"
    assert calls == [("relative ", {"task_id": "session-1"})]
    assert moved == [("session-1", "/remote/canonical/repo", "Remote")]

    project_tools.set_project_workspace_callback(
        lambda *_a: (_ for _ in ()).throw(RuntimeError("move rejected")))
    failed = json.loads(project_tools.project_create("Remote", "relative", task_id="session-1"))
    assert failed["success"] is False
    assert "workspace" in failed["error"]
