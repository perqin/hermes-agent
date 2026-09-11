from __future__ import annotations

import os
import shlex

import pytest

from hermes_cli import project_paths


class _Env:
    def __init__(self, *, is_local, output="", returncode=0):
        self.is_local = is_local
        self.output = output
        self.returncode = returncode
        self.calls = []

    def execute(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return {"output": self.output, "returncode": self.returncode}


def test_resolve_project_folder_preserves_local_semantics_and_frames_remote_probe(monkeypatch, tmp_path):
    local = _Env(is_local=True)
    monkeypatch.setattr(project_paths, "acquire_terminal_environment", lambda **_kw: local)
    raw_local = f"  {tmp_path}/missing/../file///  "

    assert project_paths.resolve_project_folder(raw_local, task_id="local-session") == (
        os.path.abspath(os.path.expanduser(raw_local.strip())).rstrip("/\\")
    )
    assert local.calls == []

    raw_remote = "~/repo; printf PWNED"
    marker = "__HERMES_PROJECT_PATH_deadbeef__"
    remote = _Env(
        is_local=False,
        output=f"login banner\n{marker}_BEGIN\n/workspaces/physical repo\n{marker}_END\n",
    )
    monkeypatch.setattr(project_paths, "acquire_terminal_environment", lambda **_kw: remote)
    monkeypatch.setattr(project_paths.secrets, "token_hex", lambda _n: "deadbeef")

    assert project_paths.resolve_project_folder(raw_remote, operation_scope="projects:coder") == (
        "/workspaces/physical repo"
    )
    command, kwargs = remote.calls[0]
    assert command.startswith("(") and command.endswith(")")
    assert shlex.quote(raw_remote) in command
    assert "builtin cd" not in command
    assert "cd --" in command
    assert "pwd -P" in command
    assert kwargs == {"timeout": 15, "rewrite_compound_background": False}


def test_strict_directory_policy_preserves_local_board_validation(monkeypatch, tmp_path):
    local = _Env(is_local=True)
    monkeypatch.setattr(project_paths, "acquire_terminal_environment", lambda **_kw: local)
    directory = tmp_path / "directory"
    directory.mkdir()
    regular_file = tmp_path / "file.txt"
    regular_file.write_text("x", encoding="utf-8")

    assert project_paths.resolve_project_folder(
        str(directory), require_absolute_existing_directory=True,
    ) == str(directory.resolve())
    with pytest.raises(ValueError):
        project_paths.resolve_project_folder(
            "directory", require_absolute_existing_directory=True)
    with pytest.raises(ValueError):
        project_paths.resolve_project_folder(
            str(regular_file), require_absolute_existing_directory=True)
