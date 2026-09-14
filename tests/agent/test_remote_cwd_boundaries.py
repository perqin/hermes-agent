from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


def _fail_probe(*_args, **_kwargs):
    raise AssertionError("controller filesystem/process probe called for remote cwd")


def test_remote_context_file_discovery_skips_controller_path_probes(monkeypatch, tmp_path):
    from agent import prompt_builder
    from agent import runtime_cwd

    remote = tmp_path / "remote-that-also-exists"
    remote.mkdir()
    (remote / "AGENTS.md").write_text("controller-only", encoding="utf-8")
    monkeypatch.setattr(runtime_cwd, "filesystem_is_local", lambda: False, raising=False)
    monkeypatch.setattr(prompt_builder.Path, "resolve", _fail_probe)

    assert prompt_builder.build_context_files_prompt(cwd=str(remote), skip_soul=True) == ""


def test_remote_cwd_disables_controller_coding_detection_and_workspace_probe(monkeypatch, tmp_path):
    from agent import coding_context
    from agent import runtime_cwd

    remote = tmp_path / "remote-that-also-exists"
    remote.mkdir()
    monkeypatch.setattr(runtime_cwd, "filesystem_is_local", lambda: False, raising=False)
    monkeypatch.setattr(coding_context, "_marker_root", _fail_probe)
    monkeypatch.setattr(coding_context, "_git_root", _fail_probe)
    monkeypatch.setattr(coding_context, "build_coding_workspace_block", _fail_probe)

    mode = coding_context.resolve_runtime_mode(
        platform="desktop",
        cwd=str(remote),
        config={"agent": {"coding_context": "auto"}},
    )

    assert mode.profile == coding_context.GENERAL_PROFILE
    assert mode.system_prompt_parts() == ([], [], [])

    forced = coding_context.resolve_runtime_mode(
        platform="desktop",
        cwd=str(remote),
        config={"agent": {"coding_context": "on"}},
    )
    prefix, workspace, _trailing = forced.system_prompt_parts(
        workspace_block="stale controller snapshot",
    )
    assert prefix
    assert workspace == []


def test_remote_cwd_rejects_controller_codex_process_before_spawn(monkeypatch, tmp_path):
    from agent import codex_runtime
    from agent import runtime_cwd
    from agent.transports import codex_app_server_session

    remote = tmp_path / "remote-that-also-exists"
    remote.mkdir()
    monkeypatch.setattr(runtime_cwd, "filesystem_is_local", lambda: False, raising=False)
    monkeypatch.setattr(codex_app_server_session, "CodexAppServerSession", _fail_probe)
    agent = SimpleNamespace(_codex_session=None, session_cwd=str(remote))

    with pytest.raises(RuntimeError, match="non-local terminal filesystem"):
        codex_runtime._ensure_codex_session(agent)
