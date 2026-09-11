"""Assignee-side materialization for Project-bound Kanban workspaces.

The dispatcher only transports desired backend paths.  This module runs after
``hermes -p <assignee>`` selected the worker profile and performs all remote
filesystem/Git operations through that profile's terminal environment.
"""

from __future__ import annotations

import os
import shlex
from typing import Any, Mapping, Optional

_ROOT_MARKER = "__HERMES_KANBAN_ROOT__="
_WORKSPACE_MARKER = "__HERMES_KANBAN_WORKSPACE__="
_BRANCH_MARKER = "__HERMES_KANBAN_BRANCH__="
_CLEANUP_MARKER = "__HERMES_KANBAN_CLEANUP__="
_PREFLIGHT_TIMEOUT_SECONDS = 120


def _result_field(result: Any, name: str, default: Any = None) -> Any:
    if isinstance(result, Mapping):
        return result.get(name, default)
    return getattr(result, name, default)


def _single_marked(output: str, marker: str) -> Optional[str]:
    values = [line[len(marker):] for line in str(output or "").splitlines() if line.startswith(marker)]
    if len(values) != 1 or not values[0]:
        return None
    return values[0]


def _remote_materialize_command(root: str, target: str, branch: str) -> str:
    qroot, qtarget, qbranch = map(shlex.quote, (root, target, branch))
    script = f"""
set -eu
expected_root={qroot}
target={qtarget}
branch={qbranch}
actual_root=$(cd -- "$expected_root" && pwd -P)
printf '%s%s\\n' {_ROOT_MARKER!r} "$actual_root"
[ "$actual_root" = "$expected_root" ] || exit 42
target_parent=$(dirname -- "$target")
expected_parent="${{expected_root%/}}/.worktrees"
[ "$target_parent" = "$expected_parent" ] || exit 42
if [ -d "$target_parent" ]; then
  actual_parent=$(cd -- "$target_parent" && pwd -P)
  [ "$actual_parent" = "$expected_parent" ] || exit 42
else
  parent_base=$(dirname -- "$target_parent")
  actual_base=$(cd -- "$parent_base" && pwd -P)
  [ "$actual_base" = "$expected_root" ] || exit 42
fi
git -C "$actual_root" rev-parse --is-inside-work-tree >/dev/null
if [ -e "$target" ]; then
  actual_target=$(cd -- "$target" && pwd -P)
  [ "$actual_target" = "$target" ] || exit 42
  root_common=$(git -C "$actual_root" rev-parse --path-format=absolute --git-common-dir)
  target_common=$(git -C "$actual_target" rev-parse --path-format=absolute --git-common-dir)
  target_git_dir=$(git -C "$actual_target" rev-parse --path-format=absolute --git-dir)
  [ "$target_common" = "$root_common" ] || exit 42
  [ "$target_git_dir" != "$target_common" ] || exit 42
  git -C "$actual_root" worktree list --porcelain | grep -F -x -- "worktree $actual_target" >/dev/null
  actual_branch=$(git -C "$actual_target" branch --show-current)
  [ "$actual_branch" = "$branch" ]
else
  mkdir -p -- "$(dirname -- "$target")"
  cd -- "$actual_root"
  if git show-ref --verify --quiet "refs/heads/$branch"; then
    git worktree add "$target" "$branch"
  else
    git worktree add -b "$branch" "$target" HEAD
  fi
  actual_target=$(cd -- "$target" && pwd -P)
  actual_branch=$(git -C "$actual_target" branch --show-current)
fi
printf '%s%s\\n' {_WORKSPACE_MARKER!r} "$actual_target"
printf '%s%s\\n' {_BRANCH_MARKER!r} "$actual_branch"
""".strip()
    return f"(\n{script}\n)"


def _remote_directory_command(root: str, path: str) -> str:
    qroot, qpath = map(shlex.quote, (root, path))
    return f"""
set -eu
expected_root={qroot}
expected={qpath}
actual_root=$(cd -- "$expected_root" && pwd -P)
actual=$(cd -- "$expected" && pwd -P)
printf '%s%s\\n' {_ROOT_MARKER!r} "$actual_root"
printf '%s%s\\n' {_WORKSPACE_MARKER!r} "$actual"
[ "$actual_root" = "$expected_root" ] || exit 42
[ "$actual" = "$expected" ] || exit 42
""".strip()


def _remote_cleanup_command(root: str, target: str, branch: str) -> str:
    qroot, qtarget, qbranch = map(shlex.quote, (root, target, branch))
    script = f"""
set -eu
expected_root={qroot}
target={qtarget}
branch={qbranch}
actual_root=$(cd -- "$expected_root" && pwd -P)
[ "$actual_root" = "$expected_root" ] || exit 42
[ -d "$target" ] || {{ printf '%s%s\\n' {_CLEANUP_MARKER!r} missing; exit 0; }}
actual_target=$(cd -- "$target" && pwd -P)
[ "$actual_target" = "$target" ] || exit 42
target_parent=$(dirname -- "$actual_target")
[ "$target_parent" = "${{actual_root%/}}/.worktrees" ] || exit 42
status_output=$(git -C "$actual_target" status --porcelain --untracked-files=normal) || exit 43
[ -z "$status_output" ] || {{ printf '%s%s\\n' {_CLEANUP_MARKER!r} preserved; exit 0; }}
actual_branch=$(git -C "$actual_target" branch --show-current) || exit 43
[ "$actual_branch" = "$branch" ] || {{ printf '%s%s\\n' {_CLEANUP_MARKER!r} preserved; exit 0; }}
branch_tip=$(git -C "$actual_root" rev-parse --verify "refs/heads/$branch") || exit 43
remote_refs=$(git -C "$actual_root" branch -r --contains "$branch_tip") || exit 43
[ -n "$remote_refs" ] || {{ printf '%s%s\\n' {_CLEANUP_MARKER!r} preserved; exit 0; }}
git -C "$actual_root" worktree remove -- "$actual_target"
case "$branch" in wt/*) git -C "$actual_root" branch -D -- "$branch" >/dev/null 2>&1 || true ;; esac
printf '%s%s\\n' {_CLEANUP_MARKER!r} removed
""".strip()
    return f"(\n{script}\n)"


def cleanup_project_worktree(
    task: Any,
    board_meta: Mapping[str, Any],
    env: Any,
    *,
    profile: str,
) -> bool:
    """Remove a safe backend worktree; preserve dirty or unpushed work."""
    root = str(board_meta.get("default_workdir") or "").strip()
    target = str(getattr(task, "workspace_path", None) or "").strip()
    branch = str(getattr(task, "branch_name", None) or "").strip()
    if not (root and target):
        return False
    try:
        result = env.execute(
            _remote_cleanup_command(root, target, branch),
            timeout=60,
            rewrite_compound_background=False,
        )
    except Exception:
        return False
    if int(_result_field(result, "returncode", 1) or 0) != 0:
        return False
    status = _single_marked(_result_field(result, "output", ""), _CLEANUP_MARKER)
    if status == "removed":
        return True
    if status in {"missing", "preserved"}:
        return False
    raise ValueError(
        f"profile {profile!r} returned an invalid backend worktree cleanup result"
    )


def materialize_project_workspace(
    task: Any,
    board_meta: Mapping[str, Any],
    env: Any,
    *,
    profile: str,
) -> tuple[str, str]:
    """Validate and materialize ``task`` in ``env`` without host path probes."""
    root = str(board_meta.get("default_workdir") or "").strip()
    target = str(getattr(task, "workspace_path", None) or "").strip()
    branch = str(getattr(task, "branch_name", None) or "").strip()
    kind = str(getattr(task, "workspace_kind", None) or "scratch")
    task_id = str(getattr(task, "id", "") or "")
    if not (root and target) or (kind == "worktree" and not branch):
        raise ValueError(f"profile {profile!r} cannot use bound workspace for task {task_id}: binding is incomplete")

    if getattr(env, "is_local", False) is True:
        from hermes_cli import kanban_db_workspace as kbw

        if kind == "worktree":
            workspace, actual_branch = kbw._resolve_worktree_workspace(task)
            return str(workspace), actual_branch
        return str(kbw.resolve_workspace(task)), ""

    if kind == "dir":
        result = env.execute(
            _remote_directory_command(root, target),
            timeout=30,
            rewrite_compound_background=False,
        )
        output = _result_field(result, "output", "")
        actual_root = _single_marked(output, _ROOT_MARKER)
        actual = _single_marked(output, _WORKSPACE_MARKER)
        if (
            int(_result_field(result, "returncode", 1) or 0) != 0
            or actual_root != root
            or actual != target
        ):
            raise ValueError(
                f"profile {profile!r} cannot use Board path {target!r}; "
                "configure its terminal environment to access the same canonical directory"
            )
        return actual, ""

    result = env.execute(
        _remote_materialize_command(root, target, branch),
        timeout=_PREFLIGHT_TIMEOUT_SECONDS,
        rewrite_compound_background=False,
    )
    actual_root = _single_marked(_result_field(result, "output", ""), _ROOT_MARKER)
    actual_workspace = _single_marked(_result_field(result, "output", ""), _WORKSPACE_MARKER)
    actual_branch = _single_marked(_result_field(result, "output", ""), _BRANCH_MARKER)
    if (
        int(_result_field(result, "returncode", 1) or 0) != 0
        or actual_root != root
        or actual_workspace != target
        or actual_branch != branch
    ):
        raise ValueError(
            f"profile {profile!r} cannot use Project path {root!r}; "
            "configure its terminal environment to access the same canonical Git repository"
        )
    return actual_workspace, actual_branch


def prepare_project_workspace_from_env() -> Optional[str]:
    """Worker startup hook; persist authoritative backend materialization.

    No-op for ordinary/local tasks.  Errors intentionally propagate before the
    agent turn starts, causing normal Kanban worker failure/block accounting.
    """
    task_id = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    project_root = (
        os.environ.get("HERMES_KANBAN_BACKEND_ROOT")
        or os.environ.get("HERMES_KANBAN_PROJECT_ROOT")
        or ""
    ).strip()
    if not (task_id and project_root):
        return None

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_workspace as kbw
    from hermes_cli.profiles import get_active_profile_name
    from tools.terminal_tool import acquire_terminal_environment, register_task_env_overrides

    board = (os.environ.get("HERMES_KANBAN_BOARD") or "").strip() or None
    profile = (
        os.environ.get("HERMES_PROFILE")
        or get_active_profile_name()
        or "default"
    )
    with kbc.connect_closing(board=board) as conn:
        task = kb.get_task(conn, task_id)
        if task is None:
            raise ValueError(f"Kanban task {task_id!r} disappeared before workspace preflight")
        stored_root = str(getattr(task, "workspace_root", None) or "").strip()
        if stored_root and stored_root != project_root:
            raise ValueError(f"Kanban task {task_id!r} workspace provenance does not match worker launch")
        meta = {
            "default_workdir": stored_root or project_root,
            "project_id": task.project_id,
            "project_slug": getattr(task, "workspace_project_slug", None),
            "source_profile": getattr(task, "workspace_source_profile", None),
            "default_workspace_kind": task.workspace_kind,
            "filesystem_local": getattr(task, "workspace_filesystem_local", None),
        }
        try:
            env = acquire_terminal_environment(task_id=task_id)
            workspace, branch = materialize_project_workspace(task, meta, env, profile=profile)
        except Exception as exc:
            reason = str(exc) if isinstance(exc, ValueError) else (
                f"profile {profile!r} cannot use bound path {project_root!r}; "
                "terminal environment setup failed"
            )
            kb.block_task(
                conn,
                task_id,
                reason=reason,
                expected_run_id=task.current_run_id,
            )
            raise ValueError(reason) from None
        kbw.set_workspace_path(conn, task_id, workspace)
        if branch:
            kbw.set_branch_name(conn, task_id, branch)

    # Keep cwd keyed to this worker task.  The environment may be a cached,
    # profile-shared instance, so register_task_env_overrides deliberately does
    # not mutate its shared ``env.cwd`` compatibility attribute.
    register_task_env_overrides(task_id, {"cwd": workspace})

    os.environ["HERMES_KANBAN_WORKSPACE"] = workspace
    if branch:
        os.environ["HERMES_KANBAN_BRANCH"] = branch
    os.environ["TERMINAL_CWD"] = workspace
    return workspace
