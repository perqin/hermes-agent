import json
import logging
from unittest.mock import MagicMock

from tools.environments.coder import CoderEnvironment
import tools.terminal_tool as terminal_tool_module


class _FakeWebSocket:
    def __init__(self, messages):
        self._messages = list(messages)
        self.requested = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def recv(self, timeout=None, decode=None):
        self.requested.append({"timeout": timeout, "decode": decode})
        if not self._messages:
            raise EOFError
        return self._messages.pop(0)


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self):
        return self._payload

def test_get_env_config_reads_coder_values(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "coder")
    monkeypatch.setenv("CODER_URL", "https://coder.example")
    monkeypatch.setenv("CODER_API_KEY", "secret-token")
    monkeypatch.setenv("CODER_WORKSPACE", "workspace-id")

    config = terminal_tool_module._get_env_config()

    assert config["env_type"] == "coder"
    assert config["coder_url"] == "https://coder.example"
    assert config["coder_api_key"] == "secret-token"
    assert config["coder_workspace"] == "workspace-id"


def test_get_env_config_prefers_config_terminal_backend(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("CODER_URL", "https://coder.example")
    monkeypatch.setenv("CODER_API_KEY", "secret-token")
    monkeypatch.setenv("CODER_WORKSPACE", "workspace-id")

    import hermes_cli.config as hermes_config
    monkeypatch.setattr(hermes_config, "load_config", lambda: {"terminal": {"backend": "coder"}})

    config = terminal_tool_module._get_env_config()

    assert config["env_type"] == "coder"


def test_create_environment_constructs_coder_backend(monkeypatch):
    sentinel = object()
    ctor = MagicMock(return_value=sentinel)
    monkeypatch.setattr(terminal_tool_module, "_CoderEnvironment", ctor)

    result = terminal_tool_module._create_environment(
        env_type="coder",
        image="ignored",
        cwd="/root",
        timeout=30,
        container_config={
            "coder_url": "https://coder.example",
            "coder_api_key": "secret-token",
            "coder_workspace": "workspace-id",
        },
        task_id="task-coder",
    )

    assert result is sentinel
    ctor.assert_called_once_with(
        base_url="https://coder.example",
        workspace="workspace-id",
        api_key="secret-token",
        cwd="/root",
        timeout=30,
    )


def test_coder_requirements_missing_workspace_logs_error(monkeypatch, caplog):
    monkeypatch.setenv("TERMINAL_ENV", "coder")
    monkeypatch.setenv("CODER_URL", "https://coder.example")
    monkeypatch.setenv("CODER_API_KEY", "secret-token")
    monkeypatch.delenv("CODER_WORKSPACE", raising=False)

    with caplog.at_level(logging.ERROR):
        ok = terminal_tool_module.check_terminal_requirements()

    assert ok is False
    assert any(
        "Coder backend selected but CODER_URL, CODER_API_KEY, and CODER_WORKSPACE must all be set"
        in record.getMessage()
        for record in caplog.records
    )


def test_coder_requirements_checks_workspace_exists(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "coder")
    monkeypatch.setenv("CODER_URL", "https://coder.example")
    monkeypatch.setenv("CODER_API_KEY", "secret-token")
    monkeypatch.setenv("CODER_WORKSPACE", "workspace-id")

    exists = MagicMock(return_value=True)
    monkeypatch.setattr(terminal_tool_module, "_coder_workspace_exists", exists)

    assert terminal_tool_module.check_terminal_requirements() is True
    exists.assert_called_once_with(
        base_url="https://coder.example",
        workspace="workspace-id",
        api_key="secret-token",
        timeout=180,
    )


def test_coder_requirements_missing_remote_workspace_logs_error(monkeypatch, caplog):
    monkeypatch.setenv("TERMINAL_ENV", "coder")
    monkeypatch.setenv("CODER_URL", "https://coder.example")
    monkeypatch.setenv("CODER_API_KEY", "secret-token")
    monkeypatch.setenv("CODER_WORKSPACE", "missing-workspace")
    monkeypatch.setattr(terminal_tool_module, "_coder_workspace_exists", MagicMock(return_value=False))

    with caplog.at_level(logging.ERROR):
        ok = terminal_tool_module.check_terminal_requirements()

    assert ok is False
    assert any(
        "missing-workspace" in record.getMessage() and "not found or was not accessible" in record.getMessage()
        for record in caplog.records
    )


def test_coder_environment_execute_reads_pty_until_eof(monkeypatch):
    workspace_payload = {
        "latest_build": {
            "transition": "start",
            "resources": [
                {"agents": [{"id": "agent-123"}]},
            ]
        }
    }
    fake_ws = _FakeWebSocket([b"/workspace\n"])
    connect_mock = MagicMock(return_value=fake_ws)
    requests_get = MagicMock(return_value=_FakeResponse(workspace_payload))

    monkeypatch.setattr("tools.environments.coder.requests.get", requests_get)
    monkeypatch.setattr("tools.environments.coder.connect", connect_mock)

    env = CoderEnvironment(
        base_url="https://coder.example",
        workspace="workspace-id",
        api_key="secret-token",
        cwd="/root",
        timeout=5,
    )

    result = env.execute("echo ignored")

    assert result["returncode"] == 0
    assert result["output"] == "/workspace\n"

    requests_get.assert_called_once()
    connect_kwargs = connect_mock.call_args.kwargs
    assert connect_kwargs["additional_headers"]["Coder-Session-Token"] == "secret-token"
    assert "command=pwd" in connect_mock.call_args.args[0]
    assert "/api/v2/workspaceagents/agent-123/pty" in connect_mock.call_args.args[0]


def test_coder_environment_autostarts_stopped_workspace(monkeypatch):
    stopped_workspace = {
        "id": "workspace-123",
        "latest_build": {
            "transition": "stop",
            "status": "stopped",
            "resources": [],
        },
    }
    started_workspace = {
        "id": "workspace-123",
        "latest_build": {
            "transition": "start",
            "resources": [{"agents": [{"id": "agent-123"}]}],
        },
    }
    requests_get = MagicMock(
        side_effect=[
            _FakeResponse(stopped_workspace),
            _FakeResponse({"job": {"status": "succeeded", "completed_at": "2026-05-19T10:10:00Z"}}),
            _FakeResponse(started_workspace),
        ]
    )
    requests_post = MagicMock(return_value=_FakeResponse({"id": "build-123"}, status_code=201))

    monkeypatch.setattr("tools.environments.coder.requests.get", requests_get)
    monkeypatch.setattr("tools.environments.coder.requests.post", requests_post)
    monkeypatch.setattr("tools.environments.coder.time.sleep", lambda _seconds: None)

    env = CoderEnvironment(
        base_url="https://coder.example",
        workspace="workspace-id",
        api_key="secret-token",
    )

    assert env._resolve_agent_id() == "agent-123"
    requests_post.assert_called_once_with(
        "https://coder.example/api/v2/workspaces/workspace-123/builds",
        headers={"Coder-Session-Token": "secret-token"},
        json={"transition": "start"},
        timeout=60,
    )


def test_terminal_tool_passes_coder_config_into_environment_factory(monkeypatch):
    class _FakeEnv:
        def execute(self, command, timeout=None, workdir=None, pty=False):
            assert command == "pwd"
            return {"output": "/workspace\n", "returncode": 0}

    create_env = MagicMock(return_value=_FakeEnv())
    monkeypatch.setattr(terminal_tool_module, "_create_environment", create_env)
    monkeypatch.setattr(terminal_tool_module, "_check_all_guards", lambda *_args, **_kwargs: {"approved": True})
    monkeypatch.setattr(terminal_tool_module, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(terminal_tool_module, "_active_environments", {})
    monkeypatch.setattr(terminal_tool_module, "_last_activity", {})
    monkeypatch.setattr(terminal_tool_module, "_creation_locks", {})
    monkeypatch.setattr(terminal_tool_module, "_task_env_overrides", {})
    monkeypatch.setenv("TERMINAL_ENV", "coder")
    monkeypatch.setenv("CODER_URL", "https://coder.example")
    monkeypatch.setenv("CODER_API_KEY", "secret-token")
    monkeypatch.setenv("CODER_WORKSPACE", "workspace-id")

    payload = json.loads(terminal_tool_module.terminal_tool(command="pwd", task_id="coder-test"))

    assert payload["exit_code"] == 0
    create_env.assert_called_once()
    assert create_env.call_args.kwargs["container_config"]["coder_url"] == "https://coder.example"
    assert create_env.call_args.kwargs["container_config"]["coder_api_key"] == "secret-token"
    assert create_env.call_args.kwargs["container_config"]["coder_workspace"] == "workspace-id"
