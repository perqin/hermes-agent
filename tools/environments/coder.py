"""Minimal Coder execution environment.

v1 bootstrap implementation: resolve a workspace agent over the Coder REST API,
optionally auto-start a stopped workspace, open the workspace PTY websocket,
and read terminal output until EOF.

Current intentional limitations for the bootstrap step:
- ignores the requested command and always runs `pwd`
- treats websocket EOF/close as successful completion
- no persistent shell/session snapshot integration yet
"""

from __future__ import annotations

import time
import urllib.parse
import uuid

import requests
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from tools.environments.base import BaseEnvironment, _ThreadedProcessHandle


def coder_workspace_exists(*, base_url: str, workspace: str, api_key: str, timeout: int = 10) -> bool:
    """Return True when the configured workspace can be fetched from the Coder API."""
    workspace_url = f"{base_url.rstrip('/')}/api/v2/workspaces/{urllib.parse.quote(workspace, safe='')}"
    response = requests.get(
        workspace_url,
        headers={"Coder-Session-Token": api_key},
        timeout=timeout,
    )
    if response.status_code == 404:
        return False
    response.raise_for_status()
    payload = response.json()
    return isinstance(payload, dict) and bool(payload)


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

    def _workspace_build_url(self, build_id: str) -> str:
        return f"{self.base_url}/api/v2/workspacebuilds/{urllib.parse.quote(build_id, safe='')}"

    def _workspace_builds_url(self, workspace_id: str) -> str:
        return f"{self.base_url}/api/v2/workspaces/{urllib.parse.quote(workspace_id, safe='')}/builds"

    def _get_workspace_payload(self) -> dict:
        response = requests.get(
            self._workspace_url(),
            headers=self._headers(),
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError(f"Unexpected workspace payload for Coder workspace {self.workspace!r}")
        return payload

    def _start_workspace(self, workspace_id: str) -> str:
        response = requests.post(
            self._workspace_builds_url(workspace_id),
            headers=self._headers(),
            json={"transition": "start"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        build_id = payload.get("id")
        if not build_id:
            raise RuntimeError(f"Coder start build for workspace {self.workspace!r} did not return a build id")
        return build_id

    def _wait_for_build_completion(self, build_id: str) -> None:
        deadline = time.time() + max(self.timeout, 300)
        while time.time() < deadline:
            response = requests.get(
                self._workspace_build_url(build_id),
                headers=self._headers(),
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
            job = payload.get("job") or {}
            status = (job.get("status") or "").lower()
            if job.get("completed_at"):
                if status != "succeeded":
                    raise RuntimeError(
                        f"Coder workspace build {build_id} finished with status {status or 'unknown'}"
                    )
                return
            time.sleep(2)
        raise TimeoutError(f"Timed out waiting for Coder workspace build {build_id} to complete")

    def _resolve_agent_id(self) -> str:
        payload = self._get_workspace_payload()
        latest_build = payload.get("latest_build") or {}
        transition = (latest_build.get("transition") or "").lower()

        if transition != "start":
            if transition == "delete":
                raise RuntimeError(f"Coder workspace {self.workspace!r} is deleted")
            if (latest_build.get("status") or "").lower() != "stopped":
                raise RuntimeError(
                    f"Coder workspace {self.workspace!r} must be started before terminal execution"
                )
            workspace_id = payload.get("id")
            if not workspace_id:
                raise RuntimeError(f"Coder workspace {self.workspace!r} did not include a workspace id")
            build_id = self._start_workspace(workspace_id)
            self._wait_for_build_completion(build_id)
            payload = self._get_workspace_payload()
            latest_build = payload.get("latest_build") or {}

        resources = latest_build.get("resources") or []
        for resource in resources:
            agents = resource.get("agents") or []
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
                "reconnect": str(uuid.uuid4()),
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
