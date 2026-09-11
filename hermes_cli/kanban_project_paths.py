"""Backend-aware path adapter for Kanban Project bindings.

The canonical Project resolver/environment acquisition APIs are owned by the
Project/terminal implementation.  This narrow module keeps Kanban independent
of provider names and gives the isolated Kanban branch a local-compatible
fallback until that implementation is merged.
"""

from __future__ import annotations

from pathlib import Path
import shlex
from typing import Any, Optional

_GIT_MARKER = "__HERMES_KANBAN_GIT__="


def _active_profile() -> str:
    from hermes_cli.profiles import get_active_profile_name

    return get_active_profile_name() or "default"


def resolve_project_directory(
    raw: str,
    *,
    require_absolute_existing_directory: bool = False,
    operation_scope: Optional[str] = None,
) -> dict[str, Any]:
    """Return canonical path, workspace kind, and filesystem-local capability.

    Prefer the shared Project resolver.  The fallback preserves the pre-change
    local dashboard contract and is deliberately local-only; a non-local
    provider requires the canonical resolver from the Project core branch.
    """
    try:
        from hermes_cli.project_paths import resolve_project_folder
    except ImportError:
        resolve_project_folder = None

    if resolve_project_folder is not None:
        kwargs: dict[str, Any] = {"operation_scope": operation_scope}
        if require_absolute_existing_directory:
            kwargs["require_absolute_existing_directory"] = True
        resolved = resolve_project_folder(raw, **kwargs)
        returned_kind = resolved.get("workspace_kind") if isinstance(resolved, dict) else None
        canonical = str(resolved.get("path")) if isinstance(resolved, dict) else str(resolved)
        from tools.terminal_tool import acquire_terminal_environment

        env = acquire_terminal_environment(operation_scope=operation_scope)
        filesystem_local = getattr(env, "is_local", False) is True
    else:
        requested = Path(raw).expanduser()
        if require_absolute_existing_directory and not requested.is_absolute():
            raise ValueError("Project directory must be an absolute path.")
        if require_absolute_existing_directory and not requested.is_dir():
            raise ValueError("Project directory must be an existing directory.")
        canonical = str(requested.resolve())
        filesystem_local = True

        returned_kind = None

    workspace_kind = returned_kind if returned_kind in {"dir", "worktree"} else "dir"
    if returned_kind not in {"dir", "worktree"} and filesystem_local:
        from hermes_cli import kanban_db_workspace as kbw

        try:
            workspace_kind = "worktree" if kbw._git_toplevel(Path(canonical)) else "dir"
        except (OSError, ValueError):
            workspace_kind = "dir"
    elif returned_kind not in {"dir", "worktree"}:
        command = (
            f"if git -C {shlex.quote(canonical)} rev-parse --is-inside-work-tree >/dev/null 2>&1; "
            f"then printf '%s%s\\n' {shlex.quote(_GIT_MARKER)} true; "
            f"else printf '%s%s\\n' {shlex.quote(_GIT_MARKER)} false; fi"
        )
        result = env.execute(
            command,
            timeout=30,
            rewrite_compound_background=False,
        )
        marked = [
            line[len(_GIT_MARKER):]
            for line in str(result.get("output") or "").splitlines()
            if line.startswith(_GIT_MARKER)
        ]
        if int(result.get("returncode", 1)) == 0 and marked == ["true"]:
            workspace_kind = "worktree"

    return {
        "default_workdir": canonical,
        "default_workspace_kind": workspace_kind,
        "filesystem_local": filesystem_local,
    }
