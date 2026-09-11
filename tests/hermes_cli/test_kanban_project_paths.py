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


@pytest.mark.parametrize(
    ("result", "error"),
    [
        ({"returncode": 255, "output": "ssh: secret-host failed"}, None),
        ({"returncode": 0, "output": ""}, None),
        ({"returncode": 0, "output": f"{kanban_project_paths._GIT_MARKER}maybe\n"}, None),
        ({
            "returncode": 0,
            "output": (
                f"{kanban_project_paths._GIT_MARKER}true\n"
                f"{kanban_project_paths._GIT_MARKER}false\n"
            ),
        }, None),
        (None, RuntimeError("credential=super-secret")),
    ],
)
def test_remote_workspace_kind_probe_fails_closed_with_sanitized_error(
    monkeypatch, result, error,
):
    project_paths = ModuleType("hermes_cli.project_paths")
    project_paths.resolve_project_folder = lambda *_a, **_k: "/srv/canonical/repo"
    monkeypatch.setitem(sys.modules, "hermes_cli.project_paths", project_paths)

    class RemoteEnvironment:
        is_local = False

        def execute(self, *_args, **_kwargs):
            if error is not None:
                raise error
            return result

    import tools.terminal_tool as terminal_tool

    monkeypatch.setattr(
        terminal_tool,
        "acquire_terminal_environment",
        lambda **_kwargs: RemoteEnvironment(),
    )

    with pytest.raises(ValueError) as exc_info:
        kanban_project_paths.resolve_project_directory("/srv/repo")

    assert "Git workspace kind" in str(exc_info.value)
    assert "super-secret" not in str(exc_info.value)


def test_remote_workspace_kind_accepts_exact_false_marker(monkeypatch):
    project_paths = ModuleType("hermes_cli.project_paths")
    project_paths.resolve_project_folder = lambda *_a, **_k: "/srv/canonical/dir"
    monkeypatch.setitem(sys.modules, "hermes_cli.project_paths", project_paths)

    class RemoteEnvironment:
        is_local = False

        def execute(self, *_args, **_kwargs):
            return {
                "returncode": 0,
                "output": f"banner\n{kanban_project_paths._GIT_MARKER}false\n",
            }

    import tools.terminal_tool as terminal_tool

    monkeypatch.setattr(
        terminal_tool,
        "acquire_terminal_environment",
        lambda **_kwargs: RemoteEnvironment(),
    )

    assert kanban_project_paths.resolve_project_directory("/srv/dir")[
        "default_workspace_kind"
    ] == "dir"
