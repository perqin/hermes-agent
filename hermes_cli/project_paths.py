"""Canonical Project paths in the configured terminal filesystem namespace."""

from __future__ import annotations

import ntpath
import os
import posixpath
import re
import secrets
import shlex
from pathlib import Path
from typing import Iterable, Optional

from tools.terminal_tool import acquire_terminal_environment

_PROJECT_PATH_TIMEOUT = 15


def normalize_local_path(path: str) -> str:
    """Preserve the historical host-local Project normalization exactly."""
    value = os.path.abspath(os.path.expanduser(str(path).strip()))
    return value.rstrip("/\\") or value


def canonical_storage_path(path: str) -> str:
    """Validate an already-canonical path without controller-side interpretation."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError("folder path must not be empty")
    if path == "/" or re.fullmatch(r"[A-Za-z]:[\\/]", path):
        return path
    return path.rstrip("/\\") or path


def _remote_probe(path: str, marker: str, *, require_absolute: bool = False) -> str:
    quoted_path = shlex.quote(path)
    begin = shlex.quote(f"{marker}_BEGIN")
    end = shlex.quote(f"{marker}_END")
    return (
        "("
        f"__hermes_project_path={quoted_path}; "
        'case "$__hermes_project_path" in '
        "'~') __hermes_project_path=$HOME ;; "
        "'~/'*) __hermes_project_path=\"$HOME/${__hermes_project_path#??}\" ;; "
        "esac; "
        + ('case "$__hermes_project_path" in /*) ;; *) exit 64 ;; esac; '
           if require_absolute else "") +
        'cd -- "$__hermes_project_path" && '
        f"printf '%s\\n%s\\n%s\\n' {begin} \"$(pwd -P)\" {end}"
        ")"
    )


def _resolution_error(path: str) -> ValueError:
    return ValueError(f"could not resolve project folder {path!r} in the terminal environment")


def resolve_project_folder(
    path: str, *, task_id: Optional[str] = None, operation_scope: Optional[str] = None,
    require_absolute_existing_directory: bool = False, environment=None,
) -> str:
    """Resolve *path* in the acquired environment, preserving permissive local behavior."""
    raw = str(path)
    try:
        env = environment if environment is not None else acquire_terminal_environment(
            task_id=task_id, operation_scope=operation_scope)
    except Exception:
        raise _resolution_error(raw) from None
    if getattr(env, "is_local", False) is True:
        if require_absolute_existing_directory:
            candidate = Path(raw.strip()).expanduser()
            if not candidate.is_absolute() or not candidate.is_dir():
                raise _resolution_error(raw)
            return str(candidate.resolve())
        return normalize_local_path(raw)

    marker = f"__HERMES_PROJECT_PATH_{secrets.token_hex(8)}__"
    try:
        result = env.execute(
            _remote_probe(
                raw, marker, require_absolute=require_absolute_existing_directory),
            timeout=_PROJECT_PATH_TIMEOUT,
            rewrite_compound_background=False,
        )
    except Exception:
        raise _resolution_error(raw) from None
    if not isinstance(result, dict) or result.get("returncode") != 0:
        raise _resolution_error(raw)
    output = result.get("output")
    if not isinstance(output, str):
        raise _resolution_error(raw)
    pattern = re.compile(
        re.escape(f"{marker}_BEGIN") + r"\r?\n(.*?)\r?\n" +
        re.escape(f"{marker}_END"), re.DOTALL)
    matches = pattern.findall(output)
    if len(matches) != 1 or not matches[0].startswith("/"):
        raise _resolution_error(raw)
    return canonical_storage_path(matches[0])


def resolve_project_folders(
    paths: Iterable[str], primary_path: Optional[str] = None, *,
    task_id: Optional[str] = None, operation_scope: Optional[str] = None,
) -> tuple[list[str], Optional[str]]:
    """Resolve a create batch completely before a caller starts a DB transaction."""
    resolved_by_raw: dict[str, str] = {}

    def resolve(raw: str) -> str:
        key = str(raw)
        if key not in resolved_by_raw:
            resolved_by_raw[key] = resolve_project_folder(
                key, task_id=task_id, operation_scope=operation_scope)
        return resolved_by_raw[key]

    resolved = list(dict.fromkeys(resolve(path) for path in paths))
    primary = resolve(primary_path) if primary_path is not None else None
    if primary and primary not in resolved:
        resolved.insert(0, primary)
    return resolved, primary


def resolve_project_folder_reference(
    project, path: str, *, task_id: Optional[str] = None,
    operation_scope: Optional[str] = None,
) -> str:
    """Resolve a mutation reference, accepting an exact stale stored path first."""
    raw = str(path)
    stored = [folder.path for folder in getattr(project, "folders", ())]
    if raw in stored:
        return raw
    return resolve_project_folder(raw, task_id=task_id, operation_scope=operation_scope)


def _path_parts(path: str) -> tuple[str, str, str]:
    """Return (style, normalized comparison key, separator) without host-OS semantics."""
    value = str(path)
    windows = bool(re.match(r"^[A-Za-z]:[\\/]", value) or value.startswith(("\\", "//")))
    if windows:
        return "windows", ntpath.normcase(ntpath.normpath(value)), "\\"
    return "posix", posixpath.normpath(value), "/"


def path_owns(folder: str, candidate: str) -> bool:
    """Whether *folder* is equal to or a path-segment ancestor of *candidate*."""
    folder_style, folder_key, separator = _path_parts(folder)
    candidate_style, candidate_key, _ = _path_parts(candidate)
    if folder_style != candidate_style:
        return False
    stem = folder_key.rstrip("/\\") or separator
    return candidate_key == folder_key or candidate_key.startswith(stem.rstrip("/\\") + separator)


def canonical_path_key(path: str) -> tuple[str, str]:
    """Style-tagged exact-path comparison key."""
    style, key, _ = _path_parts(path)
    return style, key


def path_segments(path: str) -> list[str]:
    """Path-style-neutral display segments used by Project tree consumers."""
    return [segment for segment in re.split(r"[/\\]", str(path or "").rstrip("/\\")) if segment]


def is_windows_path(path: str) -> bool:
    return _path_parts(path)[0] == "windows"


def path_comparison_segments(path: str) -> list[str]:
    segments = path_segments(path)
    return [segment.casefold() for segment in segments] if is_windows_path(path) else segments


def path_comparison_key(path: str) -> str:
    """Separator-agnostic key, case-folded only for Windows-shaped paths."""
    return "/".join(path_comparison_segments(path))
