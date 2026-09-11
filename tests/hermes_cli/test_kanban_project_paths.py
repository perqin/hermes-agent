from __future__ import annotations

import sys
from types import ModuleType

import pytest

from hermes_cli import kanban_project_paths


def test_remote_workspace_kind_is_probed_in_backend(monkeypatch):
    project_paths = ModuleType("hermes_cli.project_paths")
    seen = {}

    def resolve(raw, **kwargs):
        seen["resolve"] = (raw, kwargs)
        return "/srv/canonical/repo"

    project_paths.resolve_project_folder = resolve
    monkeypatch.setitem(sys.modules, "hermes_cli.project_paths", project_paths)

    class RemoteEnvironment:
        is_local = False

        def execute(self, command, **kwargs):
            seen["execute"] = (command, kwargs)
            return {
                "returncode": 0,
                "output": f"login banner\n{kanban_project_paths._GIT_MARKER}true\n",
            }

    import tools.terminal_tool as terminal_tool

    remote_env = RemoteEnvironment()
    monkeypatch.setattr(
        terminal_tool,
        "acquire_terminal_environment",
        lambda **_kwargs: remote_env,
        raising=False,
    )

    snapshot = kanban_project_paths.resolve_project_directory(
        "../repo",
        require_absolute_existing_directory=True,
        operation_scope="dashboard:remote",
    )

    assert snapshot == {
        "default_workdir": "/srv/canonical/repo",
        "default_workspace_kind": "worktree",
        "filesystem_local": False,
    }
    assert seen["resolve"][1]["require_absolute_existing_directory"] is True
    assert seen["resolve"][1]["environment"] is remote_env
    assert "git -C /srv/canonical/repo rev-parse --is-inside-work-tree" in seen["execute"][0]
    assert seen["execute"][1]["timeout"] <= 30


def test_environment_acquisition_failure_is_a_validation_error(monkeypatch):
    import tools.terminal_tool as terminal_tool

    monkeypatch.setattr(
        terminal_tool,
        "acquire_terminal_environment",
        lambda **_kwargs: (_ for _ in ()).throw(
            terminal_tool.TerminalEnvironmentAcquisitionError("backend unavailable")
        ),
    )

    with pytest.raises(ValueError, match="backend unavailable"):
        kanban_project_paths.resolve_project_directory("/srv/project")
