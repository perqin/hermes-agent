"""Minimal Coder execution environment.

v1 bootstrap implementation: resolve a workspace agent over the Coder REST API,
open the workspace PTY websocket, and read terminal output until EOF.

Current intentional limitations for the bootstrap step:
- ignores the requested command and always runs `pwd`
- treats websocket EOF/close as successful completion
- no persistent shell/session snapshot integration yet
"""

from __future__ import annotations

import urllib.parse

import requests
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from tools.environments.base import BaseEnvironment, _ThreadedProcessHandle


class CoderEnvironment(BaseEnvironment):
    """Execute commands inside a Coder workspace via the /pty websocket."""

    def __init__(
        self,
        *,
        base_url: str,
        workspace: str,
        api_key: str,
        cwd: str = "~",
        timeout: int = 60,
    ):
        super().__init__(cwd=cwd, timeout=timeout)
        self.base_url = base_url.rstrip("/")
        self.workspace = workspace
        self.api_key = api_key

    def _headers(self) -> dict[str, str]:
        return {"Coder-Session-Token": self.api_key}

    def _workspace_url(self) -> str:
        workspace = urllib.parse.quote(self.workspace, safe="")
        return f"{self.base_url}/api/v2/workspaces/{workspace}"

    def _resolve_agent_id(self) -> str:
        response = requests.get(
            self._workspace_url(),
            headers=self._headers(),
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        resources = payload.get("latest_build", {}).get("resources", [])
        for resource in resources:
            agents = resource.get("agents", [])
            if agents:
                agent_id = agents[0].get("id")
                if agent_id:
                    return agent_id
        raise RuntimeError(f"No workspace agent found for Coder workspace {self.workspace!r}")

    def _pty_url(self, agent_id: str) -> str:
        parsed = urllib.parse.urlparse(self.base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        query = urllib.parse.urlencode(
            {
                "reconnect": self._session_id,
                "command": "pwd",
                "height": 80,
                "width": 80,
            }
        )
        return urllib.parse.urlunparse(
            (
                scheme,
                parsed.netloc,
                f"/api/v2/workspaceagents/{agent_id}/pty",
                "",
                query,
                "",
            )
        )

    def _execute_via_pty(self) -> tuple[str, int]:
        agent_id = self._resolve_agent_id()
        pty_url = self._pty_url(agent_id)
        output_parts: list[str] = []

        with connect(
            pty_url,
            additional_headers=self._headers(),
            open_timeout=self.timeout,
            close_timeout=1,
        ) as websocket:
            while True:
                try:
                    message = websocket.recv(timeout=self.timeout, decode=False)
                except EOFError:
                    break
                except ConnectionClosed:
                    break

                if isinstance(message, bytes):
                    output_parts.append(message.decode("utf-8", errors="replace"))
                else:
                    output_parts.append(message)

        return "".join(output_parts), 0

    def _run_bash(
        self,
        cmd_string: str,
        *,
        login: bool = False,
        timeout: int = 120,
        stdin_data: str | None = None,
    ):
        del cmd_string, login, timeout, stdin_data
        return _ThreadedProcessHandle(self._execute_via_pty)

    def cleanup(self):
        return None
