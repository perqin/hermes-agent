"""Backend-aware path adapter for Kanban Project bindings.

The canonical Project resolver/environment acquisition APIs are owned by the
Project/terminal implementation.  This narrow module keeps Kanban independent
of provider names and gives the isolated Kanban branch a local-compatible
fallback until that implementation is merged.
"""

from __future__ import annotations

import ntpath
import posixpath
from pathlib import Path
import shlex
from typing import Any, Mapping, Optional

_GIT_MARKER = "__HERMES_KANBAN_GIT__="


def _result_field(result: Any, name: str, default: Any = None) -> Any:
    if isinstance(result, Mapping):
        return result.get(name, default)
    return getattr(result, name, default)


def worktree_root_for_task(path: str, task_id: str) -> Optional[str]:
    """Return a task worktree's backend root without controller path semantics."""
    from hermes_cli.project_paths import is_windows_path

    path_module = ntpath if is_windows_path(path) else posixpath
    normalized = path_module.normpath(str(path))
    task_name = path_module.basename(normalized)
    parent = path_module.dirname(normalized)
    if not (
        path_module.isabs(normalized)
        and path_module.normcase(task_name) == path_module.normcase(str(task_id))
        and path_module.normcase(path_module.basename(parent))
        == path_module.normcase(".worktrees")
    ):
        return None
    return path_module.dirname(parent)


def _active_profile() -> str:
    from hermes_cli.profiles import get_active_profile_name

    return get_active_profile_name() or "default"


def resolve_project_directory(
    raw: str,
    *,
    require_absolute_existing_directory: bool = False,
    operation_scope: Optional[str] = None,
) -> dict[str, Any]:
    """Return canonical path, workspace kind, and filesystem-local capability."""
    from hermes_cli.project_paths import resolve_project_folder
    from tools.terminal_tool import (
        TerminalEnvironmentAcquisitionError,
        acquire_terminal_environment,
    )

    try:
        env = acquire_terminal_environment(operation_scope=operation_scope)
    except TerminalEnvironmentAcquisitionError as exc:
        raise ValueError(str(exc) or "terminal environment is unavailable") from None
    canonical = resolve_project_folder(
        raw,
        operation_scope=operation_scope,
        require_absolute_existing_directory=require_absolute_existing_directory,
        environment=env,
    )
    filesystem_local = getattr(env, "is_local", False) is True
    workspace_kind = "dir"
    if filesystem_local:
        from hermes_cli import kanban_db_workspace as kbw

        try:
            workspace_kind = "worktree" if kbw._git_toplevel(Path(canonical)) else "dir"
        except (OSError, ValueError):
            workspace_kind = "dir"
    else:
        command = (
            f"git -C {shlex.quote(canonical)} rev-parse --is-inside-work-tree >/dev/null 2>&1; "
            "git_rc=$?; "
            f"if [ \"$git_rc\" -eq 0 ]; then printf '%s%s\\n' {shlex.quote(_GIT_MARKER)} true; "
            f"elif [ \"$git_rc\" -eq 128 ]; then printf '%s%s\\n' {shlex.quote(_GIT_MARKER)} false; "
            "else exit \"$git_rc\"; fi"
        )
        try:
            result = env.execute(
                command,
                timeout=30,
                rewrite_compound_background=False,
            )
            marked = [
                line[len(_GIT_MARKER):]
                for line in str(_result_field(result, "output", "") or "").splitlines()
                if line.startswith(_GIT_MARKER)
            ]
            returncode = int(_result_field(result, "returncode", 1))
        except Exception:
            raise ValueError(
                f"Could not determine Git workspace kind for Project directory {raw!r}"
            ) from None
        if returncode != 0 or len(marked) != 1 or marked[0] not in {"true", "false"}:
            raise ValueError(
                f"Could not determine Git workspace kind for Project directory {raw!r}"
            )
        if marked[0] == "true":
            workspace_kind = "worktree"

    return {
        "default_workdir": canonical,
        "default_workspace_kind": workspace_kind,
        "filesystem_local": filesystem_local,
    }
