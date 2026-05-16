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
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

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


def test_coder_environment_execute_reads_pty_until_eof(monkeypatch):
    workspace_payload = {
        "latest_build": {
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
