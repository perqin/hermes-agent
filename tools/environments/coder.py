"""Minimal Coder execution environment.

v1 bootstrap implementation: resolve a workspace agent over the Coder REST API,
optionally create or auto-start a stopped workspace, open the workspace PTY
websocket, and read terminal output until EOF.

Current intentional limitations for the bootstrap step:
- treats websocket EOF/close as successful completion
- no persistent shell/session snapshot integration yet
"""

from __future__ import annotations

import json
import re
import shlex
import threading
import time
import urllib.parse
import uuid

import requests
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from hermes_state import SessionDB
from tools.environments.base import BaseEnvironment, _ThreadedProcessHandle


_WORKSPACE_NAME_PREFIX = "hermes-"
_WORKSPACE_NAME_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,31})$")


def _coder_headers(api_key: str) -> dict[str, str]:
    return {"Coder-Session-Token": api_key}



def _resolve_lineage_root_session_id(session_id: str, db: SessionDB | None = None) -> str:
    """Resolve the lineage root for a Hermes session ID.

    If the session cannot be found, fall back to the provided ID unchanged.
    """
    if not session_id:
        raise ValueError("Coder workspace resolution requires a non-empty session/task id")

    database = db or SessionDB()
    current = session_id
    visited: set[str] = set()

    while current and current not in visited:
        visited.add(current)
        session = database.get_session(current)
        if not session:
            break
        parent = session.get("parent_session_id")
        if not parent:
            return current
        current = parent

    return current or session_id



def coder_workspace_name_for_task(task_id: str, db: SessionDB | None = None) -> str:
    """Map a Hermes task/session to a deterministic Coder workspace name."""
    root_session_id = _resolve_lineage_root_session_id(task_id, db=db)
    workspace_name = f"{_WORKSPACE_NAME_PREFIX}{root_session_id.replace('_', '-')}"
    if not _WORKSPACE_NAME_PATTERN.fullmatch(workspace_name):
        raise ValueError(
            "Derived Coder workspace name %r is invalid; expected %r + a Hermes lineage root session id"
            % (workspace_name, _WORKSPACE_NAME_PREFIX)
        )
    return workspace_name



def _workspace_search_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/api/v2/workspaces"



def _user_organizations_url(base_url: str, user: str = "me") -> str:
    user_part = urllib.parse.quote(user, safe="")
    return f"{base_url.rstrip('/')}/api/v2/users/{user_part}/organizations"


def _user_organization_by_name_url(base_url: str, organization_name: str, user: str = "me") -> str:
    user_part = urllib.parse.quote(user, safe="")
    org_part = urllib.parse.quote(organization_name, safe="")
    return f"{base_url.rstrip('/')}/api/v2/users/{user_part}/organizations/{org_part}"


def _template_by_name_url(base_url: str, organization_id: str, template_name: str) -> str:
    org_part = urllib.parse.quote(organization_id, safe="")
    template_part = urllib.parse.quote(template_name, safe="")
    return f"{base_url.rstrip('/')}/api/v2/organizations/{org_part}/templates/{template_part}"



def _create_workspace_url(base_url: str, organization_id: str, user: str = "me") -> str:
    org_part = urllib.parse.quote(organization_id, safe="")
    user_part = urllib.parse.quote(user, safe="")
    return f"{base_url.rstrip('/')}/api/v2/organizations/{org_part}/members/{user_part}/workspaces"



def _find_workspace_by_name(*, base_url: str, workspace_name: str, api_key: str, timeout: int = 10) -> dict | None:
    response = requests.get(
        _workspace_search_url(base_url),
        headers=_coder_headers(api_key),
        params={"q": f"owner:me name:{workspace_name}", "limit": 100},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    workspaces = payload.get("workspaces") if isinstance(payload, dict) else None
    if not isinstance(workspaces, list):
        raise RuntimeError(f"Unexpected workspace search payload while looking up {workspace_name!r}")
    for workspace in workspaces:
        if isinstance(workspace, dict) and workspace.get("name") == workspace_name:
            owner = workspace.get("owner_name") or workspace.get("owner", {}).get("username")
            if owner in (None, "", "me") or owner == workspace.get("owner_name"):
                return workspace
    return None



def _get_organization_id(*, base_url: str, api_key: str, organization_name: str | None = None, timeout: int = 10) -> str:
    if organization_name:
        response = requests.get(
            _user_organization_by_name_url(base_url, organization_name),
            headers=_coder_headers(api_key),
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
        org_id = payload.get("id") if isinstance(payload, dict) else None
        if not org_id:
            raise RuntimeError(f"Coder organization {organization_name!r} payload did not include an id")
        return org_id

    response = requests.get(
        _user_organizations_url(base_url),
        headers=_coder_headers(api_key),
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list) or not payload:
        raise RuntimeError("Coder user has no accessible organizations for workspace creation")
    default_org = next((org for org in payload if isinstance(org, dict) and org.get("is_default")), None)
    chosen = default_org or payload[0]
    org_id = chosen.get("id") if isinstance(chosen, dict) else None
    if not org_id:
        raise RuntimeError("Coder organization payload did not include an id")
    return org_id


def _resolve_template_id(*, base_url: str, organization_id: str, template_name: str, api_key: str, timeout: int = 10) -> str:
    response = requests.get(
        _template_by_name_url(base_url, organization_id, template_name),
        headers=_coder_headers(api_key),
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    template_id = payload.get("id") if isinstance(payload, dict) else None
    if not template_id:
        raise RuntimeError(f"Coder template {template_name!r} payload did not include an id")
    return template_id



def _create_workspace(
    *,
    base_url: str,
    workspace_name: str,
    template_name: str,
    api_key: str,
    organization_name: str | None = None,
    timeout: int = 10,
) -> dict:
    organization_id = _get_organization_id(
        base_url=base_url,
        api_key=api_key,
        organization_name=organization_name,
        timeout=timeout,
    )
    template_id = _resolve_template_id(
        base_url=base_url,
        organization_id=organization_id,
        template_name=template_name,
        api_key=api_key,
        timeout=timeout,
    )
    response = requests.post(
        _create_workspace_url(base_url, organization_id),
        headers={**_coder_headers(api_key), "Content-Type": "application/json", "Accept": "application/json"},
        json={"name": workspace_name, "template_id": template_id},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected create-workspace payload for Coder workspace {workspace_name!r}")
    return payload



def coder_workspace_exists(*, base_url: str, workspace_name: str, api_key: str, timeout: int = 10) -> bool:
    """Return True when a workspace with the given name exists for the current user."""
    return _find_workspace_by_name(
        base_url=base_url,
        workspace_name=workspace_name,
        api_key=api_key,
        timeout=timeout,
    ) is not None


class CoderEnvironment(BaseEnvironment):
    """Execute commands inside a Coder workspace via the /pty websocket."""

    _stdin_mode = "passthrough"
    _STDIN_CHUNK_SIZE = 32 * 1024

    def __init__(
        self,
        *,
        base_url: str,
        template_name: str,
        task_id: str,
        api_key: str,
        organization_name: str | None = None,
        workspace_name: str | None = None,
        cwd: str = "~",
        timeout: int = 60,
        init_session: bool = True,
    ):
        super().__init__(cwd=cwd, timeout=timeout)
        self.base_url = base_url.rstrip("/")
        self.template_name = template_name
        self.organization_name = organization_name or None
        self.task_id = task_id
        self.workspace = workspace_name or coder_workspace_name_for_task(task_id)
        self.api_key = api_key
        self._workspace_id: str | None = None

        # Safe to call here: init_session() uses _run_bash() directly, which
        # resolves the workspace/agent and opens a PTY without going back
        # through BaseEnvironment.execute(), so there is no recursive wrapping
        # or re-entry into init_session().
        if init_session:
            self.init_session()

    def _headers(self) -> dict[str, str]:
        return _coder_headers(self.api_key)

    def _workspace_url(self) -> str:
        if not self._workspace_id:
            raise RuntimeError(f"Coder workspace {self.workspace!r} has not been resolved yet")
        workspace_id = urllib.parse.quote(self._workspace_id, safe="")
        return f"{self.base_url}/api/v2/workspaces/{workspace_id}"

    def _workspace_build_url(self, build_id: str) -> str:
        return f"{self.base_url}/api/v2/workspacebuilds/{urllib.parse.quote(build_id, safe='')}"

    def _workspace_builds_url(self, workspace_id: str) -> str:
        return f"{self.base_url}/api/v2/workspaces/{urllib.parse.quote(workspace_id, safe='')}/builds"

    def _ensure_workspace(self) -> dict:
        payload = _find_workspace_by_name(
            base_url=self.base_url,
            workspace_name=self.workspace,
            api_key=self.api_key,
            timeout=self.timeout,
        )
        if payload is None:
            payload = _create_workspace(
                base_url=self.base_url,
                workspace_name=self.workspace,
                template_name=self.template_name,
                api_key=self.api_key,
                organization_name=self.organization_name,
                timeout=self.timeout,
            )
        workspace_id = payload.get("id") if isinstance(payload, dict) else None
        if not workspace_id:
            raise RuntimeError(f"Coder workspace {self.workspace!r} did not include a workspace id")
        self._workspace_id = workspace_id
        return payload

    def _get_workspace_payload(self) -> dict:
        self._ensure_workspace()
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
        else:
            job = latest_build.get("job") or {}
            if latest_build.get("id") and not job.get("completed_at"):
                self._wait_for_build_completion(latest_build["id"])
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

    @staticmethod
    def _exit_marker(reconnect_id: str) -> str:
        return f"__HERMES_EXIT_{reconnect_id}__"

    @staticmethod
    def _extract_exit_code(output: str, exit_marker: str) -> tuple[str, int]:
        pattern = re.compile(
            rf"(?:\r?\n)?{re.escape(exit_marker)}(\d{{1,3}}){re.escape(exit_marker)}\r?\n?"
        )
        matches = list(pattern.finditer(output))
        if not matches:
            warning = "[Coder PTY exit marker missing]"
            separator = "\n" if output and not output.endswith("\n") else ""
            return f"{output}{separator}{warning}", 1

        match = matches[-1]
        exit_code = int(match.group(1))
        if not 0 <= exit_code <= 255:
            exit_code = 1
        cleaned = output[: match.start()] + output[match.end() :]
        return cleaned, exit_code

    def _pty_command(self, cmd_string: str, *, login: bool, exit_marker: str) -> str:
        inner_shell_flag = "-lc" if login else "-c"
        capture_script = "\n".join(
            [
                f"bash {inner_shell_flag} {shlex.quote(cmd_string)}",
                "__coder_ec=$?",
                f"printf '\\n{exit_marker}%s{exit_marker}\\n' \"$__coder_ec\"",
                "exit \"$__coder_ec\"",
            ]
        )
        return f"bash -c {shlex.quote(capture_script)}"

    def _pty_url(self, agent_id: str, *, command: str, reconnect_id: str) -> str:
        parsed = urllib.parse.urlparse(self.base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        query = urllib.parse.urlencode(
            {
                "reconnect": reconnect_id,
                "command": command,
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

    @classmethod
    def _stdin_frame(cls, data: str) -> bytes:
        return json.dumps({"data": data}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    @classmethod
    def _send_stdin_data(cls, websocket, stdin_data: str) -> None:
        if not stdin_data:
            return
        chunk_size = max(1, cls._STDIN_CHUNK_SIZE)
        for start in range(0, len(stdin_data), chunk_size):
            websocket.send(cls._stdin_frame(stdin_data[start : start + chunk_size]))

    @classmethod
    def _send_stdin_eof(cls, websocket) -> None:
        # EOT / Ctrl+D signals EOF for stdin-driven commands.
        websocket.send(cls._stdin_frame("\u0004"))

    @classmethod
    def _interrupt_pty(cls, websocket) -> None:
        """Send Ctrl+C to a Coder PTY websocket and close it.

        Coder PTY expects binary WebSocket frames that carry JSON payloads.
        Interrupt is ETX (0x03) in the "data" field.
        """
        try:
            websocket.send(cls._stdin_frame("\u0003"))
        except Exception:
            pass
        try:
            websocket.close()
        except Exception:
            pass

    def _execute_via_pty(
        self,
        cmd_string: str,
        *,
        login: bool,
        stdin_data: str | None = None,
        cancel_state: dict | None = None,
    ) -> tuple[str, int]:
        agent_id = self._resolve_agent_id()
        reconnect_id = str(uuid.uuid4())
        exit_marker = self._exit_marker(reconnect_id)
        pty_command = self._pty_command(cmd_string, login=login, exit_marker=exit_marker)
        pty_url = self._pty_url(agent_id, command=pty_command, reconnect_id=reconnect_id)
        output_parts: list[str] = []

        with connect(
            pty_url,
            additional_headers=self._headers(),
            open_timeout=self.timeout,
            close_timeout=1,
        ) as websocket:
            should_interrupt = False
            if cancel_state is not None:
                lock = cancel_state["lock"]
                with lock:
                    cancel_state["websocket"] = websocket
                    should_interrupt = bool(cancel_state.get("cancelled"))
            if should_interrupt:
                self._interrupt_pty(websocket)

            if stdin_data is not None:
                self._send_stdin_data(websocket, stdin_data)
                self._send_stdin_eof(websocket)

            try:
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
            finally:
                if cancel_state is not None:
                    with cancel_state["lock"]:
                        if cancel_state.get("websocket") is websocket:
                            cancel_state["websocket"] = None

        return self._extract_exit_code("".join(output_parts), exit_marker)

    def _run_bash(
        self,
        cmd_string: str,
        *,
        login: bool = False,
        timeout: int = 120,
        stdin_data: str | None = None,
    ):
        del timeout
        cancel_state = {"lock": threading.Lock(), "websocket": None, "cancelled": False}

        def cancel_pty() -> None:
            websocket = None
            with cancel_state["lock"]:
                cancel_state["cancelled"] = True
                websocket = cancel_state.get("websocket")
            if websocket is not None:
                self._interrupt_pty(websocket)

        return _ThreadedProcessHandle(
            lambda: self._execute_via_pty(
                cmd_string,
                login=login,
                stdin_data=stdin_data,
                cancel_state=cancel_state,
            ),
            cancel_fn=cancel_pty,
        )

    def cleanup(self):
        return None
