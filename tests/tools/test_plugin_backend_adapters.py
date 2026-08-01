from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from tools.environments import (
    BackendCapabilities,
    BackendDefinition,
    BackendFactoryRequest,
    BaseEnvironment,
    ExecutionLocation,
    FilesystemSemantics,
)
from tools.environments.facade import reset_environment_facade
from tools.environments.registry import terminal_backend_registry


@pytest.fixture(autouse=True)
def _reset_backend_runtime():
    terminal_backend_registry.reset()
    reset_environment_facade()
    yield
    terminal_backend_registry.reset()
    reset_environment_facade()


def _definition(*, available: bool = True) -> BackendDefinition:
    return BackendDefinition(
        name="third_party_remote",
        label="Third-party Remote",
        description="a third-party remote workspace",
        factory=lambda _request: MagicMock(spec=BaseEnvironment),
        availability_check=lambda: available,
        capabilities=BackendCapabilities(
            execution_location=ExecutionLocation.REMOTE,
            filesystem_semantics=FilesystemSemantics.ISOLATED,
            requires_sandbox_cwd=True,
        ),
    )


def test_experimental_config_uses_registered_sandbox_default_cwd(monkeypatch):
    from tools.terminal_tool import _get_env_config

    definition = _definition()
    definition.default_cwd = "~"
    terminal_backend_registry.register(definition)
    monkeypatch.setenv("EXP_BACKEND", "1")
    monkeypatch.setenv("TERMINAL_ENV", definition.name)
    monkeypatch.setenv("TERMINAL_CWD", "/opt/host-only/project")

    config = _get_env_config()

    assert config["cwd"] == "~"
    assert config["host_cwd"] is None


@pytest.mark.parametrize(
    "candidate",
    [
        "/opt/host-only/project",
        "/home/host-user/project",
        "C:\\Users\\host\\repo",
        "src",
    ],
)
def test_isolated_plugin_rejects_unverified_resolved_cwd(monkeypatch, candidate):
    from tools.terminal_tool import _sanitize_registered_sandbox_cwd

    definition = _definition()
    definition.default_cwd = "~"
    terminal_backend_registry.register(definition)
    monkeypatch.setenv("EXP_BACKEND", "1")

    assert _sanitize_registered_sandbox_cwd(definition.name, candidate, "~") == "~"


def test_plugin_instance_cwd_is_not_host_verified(monkeypatch):
    from tools.terminal_tool import _sanitize_registered_sandbox_cwd

    definition = _definition()
    definition.default_cwd = "~"
    terminal_backend_registry.register(definition)
    monkeypatch.setenv("EXP_BACKEND", "1")

    assert (
        _sanitize_registered_sandbox_cwd(
            definition.name,
            "/home/coder/project",
            "~",
            verified_cwd="/home/coder/project",
        )
        == "~"
    )


@pytest.mark.parametrize(
    ("semantics", "requires_sandbox_cwd", "accepts_host_cwd"),
    [
        (FilesystemSemantics.UNKNOWN, False, False),
        (FilesystemSemantics.HOST, False, True),
        (FilesystemSemantics.ISOLATED, False, False),
        (FilesystemSemantics.ISOLATED, True, False),
        (FilesystemSemantics.ISOLATED, True, True),
    ],
)
def test_terminal_rejects_task_cwd_override_for_unverified_plugin(
    monkeypatch, semantics, requires_sandbox_cwd, accepts_host_cwd
):
    import tools.terminal_tool as terminal_tool

    calls = []

    class FakeEnv:
        env = {}
        cwd = "~"

        def execute(self, command, **kwargs):
            calls.append((command, kwargs))
            return {"output": "ok", "returncode": 0}

    definition = _definition()
    definition.default_cwd = "~"
    definition.capabilities.filesystem_semantics = semantics
    definition.capabilities.requires_sandbox_cwd = requires_sandbox_cwd
    definition.capabilities.accepts_host_cwd = accepts_host_cwd
    terminal_backend_registry.register(definition)
    task_id = "isolated-plugin-task"
    monkeypatch.setenv("EXP_BACKEND", "1")
    monkeypatch.setattr(terminal_tool, "_active_environments", {task_id: FakeEnv()})
    monkeypatch.setattr(terminal_tool, "_last_activity", {})
    monkeypatch.setattr(
        terminal_tool,
        "_task_env_overrides",
        {task_id: {"cwd": "/opt/host-only/project"}},
    )
    monkeypatch.setattr(terminal_tool, "_session_cwd", {})
    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {
            "env_type": definition.name,
            "cwd": "~",
            "timeout": 60,
            "lifetime_seconds": 3600,
        },
    )
    monkeypatch.setattr(
        terminal_tool,
        "_check_all_guards",
        lambda command, env_type, **kwargs: {"approved": True},
    )

    result = json.loads(terminal_tool.terminal_tool(command="pwd", task_id=task_id))

    assert result["exit_code"] == 0
    assert calls == [("pwd", {"timeout": 60, "cwd": "~", "bounded_capture": True})]


def test_background_terminal_passes_sanitized_cwd_to_plugin_local_environment(
    monkeypatch,
):
    import tools.terminal_tool as terminal_tool
    import tools.process_registry as process_registry_module
    from tools.environments.local import LocalEnvironment

    calls = []

    class CapturingLocalEnvironment(LocalEnvironment):
        def execute(self, command, **kwargs):
            calls.append((command, kwargs))
            return {"output": "4321\n", "returncode": 0}

    definition = _definition()
    definition.default_cwd = "~"
    definition.factory = lambda _request: CapturingLocalEnvironment(
        cwd="/opt/host-only/project"
    )
    terminal_backend_registry.register(definition)
    task_id = "isolated-plugin-background-task"
    env = CapturingLocalEnvironment(cwd="/opt/host-only/project")
    monkeypatch.setenv("EXP_BACKEND", "1")
    monkeypatch.setattr(terminal_tool, "_active_environments", {task_id: env})
    monkeypatch.setattr(terminal_tool, "_last_activity", {})
    monkeypatch.setattr(
        terminal_tool,
        "_task_env_overrides",
        {task_id: {"cwd": "/opt/host-only/project"}},
    )
    monkeypatch.setattr(terminal_tool, "_session_cwd", {})
    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {
            "env_type": definition.name,
            "cwd": "~",
            "timeout": 60,
            "lifetime_seconds": 3600,
        },
    )
    monkeypatch.setattr(
        terminal_tool,
        "_check_all_guards",
        lambda command, env_type, **kwargs: {"approved": True},
    )
    fake_thread = MagicMock()
    monkeypatch.setattr(
        process_registry_module.threading, "Thread", lambda **_kwargs: fake_thread
    )
    monkeypatch.setattr(
        process_registry_module.process_registry,
        "_write_checkpoint",
        lambda: None,
    )

    result = json.loads(
        terminal_tool.terminal_tool(
            command="pwd",
            task_id=task_id,
            background=True,
            notify_on_complete=False,
        )
    )

    try:
        assert result["exit_code"] == 0
        assert calls[0][1]["cwd"] == "~"
        assert "/opt/host-only/project" not in calls[0][1].values()
    finally:
        process_registry_module.process_registry._running.pop(
            result["session_id"], None
        )


def test_experimental_requirements_accept_registered_available_backend(monkeypatch):
    from tools.terminal_tool import check_terminal_requirements

    terminal_backend_registry.register(_definition())
    monkeypatch.setenv("EXP_BACKEND", "1")
    monkeypatch.setenv("TERMINAL_ENV", "third_party_remote")

    assert check_terminal_requirements() is True


def test_experimental_requirements_reject_registered_unavailable_backend(
    monkeypatch, caplog
):
    from tools.terminal_tool import check_terminal_requirements

    terminal_backend_registry.register(_definition(available=False))
    monkeypatch.setenv("EXP_BACKEND", "1")
    monkeypatch.setenv("TERMINAL_ENV", "third_party_remote")

    assert check_terminal_requirements() is False
    assert (
        "Experimental terminal backend 'third_party_remote' is unavailable"
        in caplog.text
    )


def test_experimental_remote_capability_suppresses_host_prompt(monkeypatch):
    import agent.prompt_builder as prompt_builder

    terminal_backend_registry.register(_definition())
    monkeypatch.setenv("EXP_BACKEND", "1")
    monkeypatch.setenv("TERMINAL_ENV", "third_party_remote")
    monkeypatch.setattr(prompt_builder, "is_wsl", lambda: False)
    monkeypatch.setattr(prompt_builder, "_probe_remote_backend", lambda _name: None)
    prompt_builder._clear_backend_probe_cache()

    hint = prompt_builder.build_environment_hints()

    assert "Host:" not in hint
    assert "Current working directory:" not in hint
    assert "Terminal backend: third_party_remote" in hint
    assert "third-party remote workspace" in hint


def test_experimental_unknown_location_suppresses_host_prompt(monkeypatch):
    import agent.prompt_builder as prompt_builder

    definition = _definition()
    definition.capabilities.execution_location = ExecutionLocation.UNKNOWN
    terminal_backend_registry.register(definition)
    monkeypatch.setenv("EXP_BACKEND", "1")
    monkeypatch.setenv("TERMINAL_ENV", definition.name)
    monkeypatch.setattr(prompt_builder, "_probe_remote_backend", lambda _name: None)
    prompt_builder._clear_backend_probe_cache()

    hint = prompt_builder.build_environment_hints()

    assert "Host:" not in hint
    assert "Current working directory:" not in hint


def test_third_party_local_declaration_cannot_expose_host_prompt(monkeypatch):
    import agent.prompt_builder as prompt_builder

    definition = _definition()
    definition.capabilities.execution_location = ExecutionLocation.LOCAL
    definition.capabilities.filesystem_semantics = FilesystemSemantics.HOST
    terminal_backend_registry.register(definition)
    monkeypatch.setenv("EXP_BACKEND", "1")
    monkeypatch.setenv("TERMINAL_ENV", definition.name)
    monkeypatch.setattr(prompt_builder, "_probe_remote_backend", lambda _name: None)
    prompt_builder._clear_backend_probe_cache()

    hint = prompt_builder.build_environment_hints()

    assert "Host:" not in hint
    assert "User home directory:" not in hint
    assert "Current working directory:" not in hint


def test_experimental_plugin_does_not_run_sensitive_live_probe(monkeypatch):
    import agent.prompt_builder as prompt_builder
    from tools.environments.local import LocalEnvironment

    definition = _definition()
    definition.factory = lambda _request: LocalEnvironment(cwd="/home/yuki-bot")
    definition.capabilities.execution_location = ExecutionLocation.LOCAL
    definition.capabilities.filesystem_semantics = FilesystemSemantics.HOST
    terminal_backend_registry.register(definition)
    monkeypatch.setenv("EXP_BACKEND", "1")
    monkeypatch.setenv("TERMINAL_ENV", definition.name)
    probe = MagicMock(return_value="OS: Linux\nUser: yuki-bot\nHome: /home/yuki-bot")
    monkeypatch.setattr(prompt_builder, "_probe_remote_backend", probe)

    hint = prompt_builder.build_environment_hints()

    probe.assert_not_called()
    assert "yuki-bot" not in hint
    assert "/home/yuki-bot" not in hint


def test_third_party_local_declaration_cannot_enable_host_path_resolution(monkeypatch):
    from tools.file_tools import _uses_container_paths

    definition = _definition()
    definition.capabilities.execution_location = ExecutionLocation.LOCAL
    definition.capabilities.filesystem_semantics = FilesystemSemantics.HOST
    terminal_backend_registry.register(definition)
    monkeypatch.setenv("EXP_BACKEND", "1")
    monkeypatch.setenv("TERMINAL_ENV", definition.name)

    assert _uses_container_paths() is True


def test_experimental_file_paths_ignore_environment_class_and_host_cwd(monkeypatch):
    import tools.terminal_tool as terminal_tool
    from tools.environments.local import LocalEnvironment
    from tools.file_tools import (
        _resolve_path_for_task,
        _terminal_env_type_for_task,
        _uses_container_paths,
    )

    definition = _definition()
    definition.default_cwd = "~"
    terminal_backend_registry.register(definition)
    monkeypatch.setenv("EXP_BACKEND", "1")
    monkeypatch.setenv("TERMINAL_ENV", definition.name)
    monkeypatch.setenv("TERMINAL_CWD", "/opt/host-only/project")
    monkeypatch.setattr(
        terminal_tool,
        "_active_environments",
        {"default": LocalEnvironment(cwd="/opt/host-only/project")},
    )

    assert _terminal_env_type_for_task() == definition.name
    assert _uses_container_paths() is True
    assert str(_resolve_path_for_task("evidence.txt")) == "~/evidence.txt"
    assert str(_resolve_path_for_task("~")) == "~"
    assert str(_resolve_path_for_task("~/evidence.txt")) == "~/evidence.txt"
    assert (
        str(_resolve_path_for_task("/workspace/evidence.txt"))
        == "/workspace/evidence.txt"
    )


def test_threaded_process_handle_is_public_plugin_contract():
    from tools.environments import ThreadedProcessHandle

    handle = ThreadedProcessHandle(lambda: ("ok", 0))
    assert handle.wait(timeout=1) == 0


@pytest.mark.parametrize(
    "backend_name",
    ["docker", "modal", "singularity", "daytona", "ssh", "vercel_sandbox"],
)
def test_experimental_registered_names_never_skip_hardline_guards(
    monkeypatch, backend_name
):
    from tools.approval import check_dangerous_command

    monkeypatch.setenv("EXP_BACKEND", "1")

    result = check_dangerous_command("rm -rf /", backend_name, has_host_access=True)

    assert result["approved"] is False


@pytest.mark.parametrize(
    "backend_name",
    ["docker", "modal", "singularity", "daytona", "ssh", "vercel_sandbox"],
)
def test_experimental_registered_names_never_skip_execute_code_guard(
    monkeypatch, backend_name
):
    import tools.approval as approval

    monkeypatch.setenv("EXP_BACKEND", "1")
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    monkeypatch.setattr(approval, "_get_cron_approval_mode", lambda: "deny")

    result = approval.check_execute_code_guard(
        "import os; os.remove('/host/evidence')",
        backend_name,
        has_host_access=True,
    )

    assert result["approved"] is False


def test_forward_env_helper_preserves_explicit_opt_in_and_filters_implicit_secrets(
    monkeypatch,
):
    from tools.environments import collect_forwarded_env_values
    import tools.env_passthrough as env_passthrough

    monkeypatch.setattr(
        env_passthrough,
        "get_all_passthrough",
        lambda: ["OPENAI_API_KEY", "SAFE_IMPLICIT", "AUXILIARY_TEST_API_KEY"],
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("SAFE_IMPLICIT", raising=False)
    monkeypatch.delenv("EXPLICIT_VALUE", raising=False)

    resolved = collect_forwarded_env_values(
        ["OPENAI_API_KEY", "EXPLICIT_VALUE"],
        config_name="test_forward_env",
        dotenv_loader=lambda: {
            "OPENAI_API_KEY": "explicit-provider-secret",
            "SAFE_IMPLICIT": "safe-value",
            "AUXILIARY_TEST_API_KEY": "internal-secret",
            "EXPLICIT_VALUE": "explicit-value",
        },
    )

    assert resolved == {
        "EXPLICIT_VALUE": "explicit-value",
        "OPENAI_API_KEY": "explicit-provider-secret",
        "SAFE_IMPLICIT": "safe-value",
    }


def test_forward_env_helper_normalizes_untrusted_implicit_names(monkeypatch):
    from tools.environments import collect_forwarded_env_values
    import tools.env_passthrough as env_passthrough

    monkeypatch.setattr(
        env_passthrough,
        "get_all_passthrough",
        lambda: [
            " SAFE_IMPLICIT ",
            "SAFE_IMPLICIT",
            "BAD-NAME",
            "OPENAI_API_KEY",
            ["UNHASHABLE"],
            42,
        ],
    )
    monkeypatch.delenv("SAFE_IMPLICIT", raising=False)

    resolved = collect_forwarded_env_values(
        None,
        dotenv_loader=lambda: {
            "SAFE_IMPLICIT": "safe-value",
            "OPENAI_API_KEY": "must-not-forward",
        },
    )

    assert resolved == {"SAFE_IMPLICIT": "safe-value"}


def test_ship_file_to_remote_raises_when_backend_write_fails():
    from tools.code_execution_tool import _ship_file_to_remote

    env = MagicMock()
    env.execute.return_value = {"returncode": 23, "output": "write failed"}

    with pytest.raises(RuntimeError, match="23"):
        _ship_file_to_remote(env, "/tmp/code.py", "print('hello')")


def test_execute_code_resolves_overrides_with_raw_task_id(monkeypatch):
    import threading
    import tools.code_execution_tool as code_execution
    import tools.terminal_tool as terminal_tool

    captured: dict[str, object] = {}
    fake_env = MagicMock(spec=BaseEnvironment)

    def create_environment(**kwargs):
        captured.update(kwargs)
        return fake_env

    resolve_overrides = MagicMock(return_value={"cwd": "/session/workspace"})
    monkeypatch.setattr(terminal_tool, "_active_environments", {})
    monkeypatch.setattr(terminal_tool, "_last_activity", {})
    monkeypatch.setattr(terminal_tool, "_creation_locks", {})
    monkeypatch.setattr(terminal_tool, "_env_lock", threading.Lock())
    monkeypatch.setattr(terminal_tool, "_creation_locks_lock", threading.Lock())
    monkeypatch.setattr(
        terminal_tool, "_resolve_container_task_id", lambda _task: "collapsed"
    )
    monkeypatch.setattr(terminal_tool, "resolve_task_overrides", resolve_overrides)
    monkeypatch.setattr(terminal_tool, "_create_environment", create_environment)
    monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {
            "env_type": "local",
            "cwd": "/global/workspace",
            "timeout": 60,
            "local_persistent": False,
            "host_cwd": None,
        },
    )

    env, env_type = code_execution._get_or_create_env("raw-session")

    assert env is fake_env
    assert env_type == "local"
    resolve_overrides.assert_called_once_with("raw-session")
    assert captured["cwd"] == "/session/workspace"
    assert captured["task_id"] == "collapsed"


def test_manager_resolves_plugin_config_before_calling_factory():
    from tools.environments.manager import EnvironmentManager

    received: dict[str, object] = {}

    def factory(request: BackendFactoryRequest):
        received.update(request.backend_config)
        return MagicMock(spec=BaseEnvironment)

    definition = _definition()
    definition.factory = factory
    definition.config_resolver = lambda: {
        "url": "https://coder.example",
        "workspace": "from-environment",
    }
    terminal_backend_registry.register(definition)

    EnvironmentManager().create_environment(
        BackendFactoryRequest(
            backend_name=definition.name,
            backend_config={"workspace": "explicit-override"},
        )
    )

    assert received == {
        "url": "https://coder.example",
        "workspace": "explicit-override",
    }


def test_manager_sanitizes_isolated_factory_request_cwd_and_host_cwd():
    from tools.environments.manager import EnvironmentManager

    received: dict[str, object] = {}

    def factory(request: BackendFactoryRequest):
        received["cwd"] = request.cwd
        received["host_cwd"] = request.host_cwd
        return MagicMock(spec=BaseEnvironment)

    definition = _definition()
    definition.default_cwd = "~"
    definition.factory = factory
    terminal_backend_registry.register(definition)

    EnvironmentManager().create_environment(
        BackendFactoryRequest(
            backend_name=definition.name,
            cwd="/opt/host-only/project",
            host_cwd="/opt/host-only/project",
        )
    )

    assert received == {"cwd": "~", "host_cwd": None}


@pytest.mark.parametrize(
    ("semantics", "requires_sandbox_cwd", "accepts_host_cwd"),
    [
        (FilesystemSemantics.UNKNOWN, False, False),
        (FilesystemSemantics.HOST, False, True),
        (FilesystemSemantics.ISOLATED, False, False),
        (FilesystemSemantics.ISOLATED, True, True),
    ],
)
def test_manager_never_trusts_third_party_capabilities_for_host_cwd(
    semantics, requires_sandbox_cwd, accepts_host_cwd
):
    from tools.environments.manager import EnvironmentManager

    received: dict[str, object] = {}

    def factory(request: BackendFactoryRequest):
        received.update(cwd=request.cwd, host_cwd=request.host_cwd)
        return MagicMock(spec=BaseEnvironment)

    definition = _definition()
    definition.default_cwd = "~"
    definition.factory = factory
    definition.capabilities.filesystem_semantics = semantics
    definition.capabilities.requires_sandbox_cwd = requires_sandbox_cwd
    definition.capabilities.accepts_host_cwd = accepts_host_cwd
    terminal_backend_registry.register(definition)

    EnvironmentManager().create_environment(
        BackendFactoryRequest(
            backend_name=definition.name,
            cwd="/opt/host-only/project",
            host_cwd="/opt/host-only/project",
        )
    )

    assert received == {"cwd": "~", "host_cwd": None}


def test_manager_rejects_plugin_factory_returning_host_local_environment():
    from tools.environments.local import LocalEnvironment
    from tools.environments.manager import EnvironmentManager

    definition = _definition()
    definition.factory = lambda _request: LocalEnvironment(cwd="/opt/host-only")
    terminal_backend_registry.register(definition)

    with pytest.raises(TypeError, match="host-owned LocalEnvironment"):
        EnvironmentManager().create_environment(
            BackendFactoryRequest(backend_name=definition.name)
        )


def test_manager_rejects_non_mapping_plugin_config():
    from tools.environments.manager import EnvironmentManager

    definition = _definition()
    definition.config_resolver = lambda: ["not", "a", "mapping"]
    terminal_backend_registry.register(definition)

    with pytest.raises(TypeError, match="config_resolver must return a mapping"):
        EnvironmentManager().create_environment(
            BackendFactoryRequest(backend_name=definition.name)
        )
