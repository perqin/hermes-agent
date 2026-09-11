"""Compensated reciprocal Project/Board binding operations."""

from __future__ import annotations

import contextlib
import shutil
from contextlib import contextmanager
from typing import Any, Iterator

from hermes_cli import kanban_db as kb
from hermes_cli import projects_db as pdb


def _restore_metadata(path, content: bytes | None) -> None:
    if content is None:
        path.unlink(missing_ok=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


@contextmanager
def reciprocal_project_bind(
    project_conn,
    project: Any,
    board: str,
) -> Iterator[None]:
    """Compensate both board files if the reciprocal Project update fails."""
    target_meta = kb.read_board_metadata(board)
    target_owner = target_meta.get("project_id")
    if target_owner and target_owner != project.id:
        raise ValueError(f"board {board!r} is already bound to another project")

    old_board = project.board_slug if project.board_slug and project.board_slug != board else None
    target_dir = kb.board_dir(board)
    target_existed = target_dir.exists()
    target_path = kb.board_metadata_path(board)
    target_before = target_path.read_bytes() if target_path.exists() else None
    old_path = kb.board_metadata_path(old_board) if old_board else None
    old_before = old_path.read_bytes() if old_path is not None and old_path.exists() else None

    try:
        yield
        if old_board:
            old_meta = kb.read_board_metadata(old_board)
            if old_meta.get("project_id") == project.id:
                kb.write_board_metadata(old_board, clear_project_binding=True)
        if not pdb.update_project(project_conn, project.id, board_slug=board):
            raise ValueError(f"project {project.slug!r} disappeared during bind")
    except Exception as exc:
        try:
            if target_existed:
                _restore_metadata(target_path, target_before)
            else:
                shutil.rmtree(target_dir, ignore_errors=True)
            if old_path is not None:
                _restore_metadata(old_path, old_before)
        except OSError:
            raise ValueError("board bind failed and metadata rollback failed") from None
        if isinstance(exc, ValueError):
            raise
        raise ValueError("board bind failed before reciprocal metadata was committed") from None


def clear_owned_project_binding(project_conn, project: Any, board: str) -> None:
    """Clear both sides when ``project_conn`` is the trusted owner store."""
    path = kb.board_metadata_path(board)
    before = path.read_bytes() if path.exists() else None
    try:
        meta = kb.read_board_metadata(board)
        if meta.get("project_id") == project.id:
            kb.write_board_metadata(board, clear_project_binding=True)
        if not pdb.update_project(project_conn, project.id, board_slug=""):
            raise ValueError(f"project {project.slug!r} disappeared during unbind")
    except Exception as exc:
        with contextlib.suppress(OSError):
            _restore_metadata(path, before)
        if isinstance(exc, ValueError):
            raise
        raise ValueError("board unbind failed before reciprocal metadata was committed") from None
