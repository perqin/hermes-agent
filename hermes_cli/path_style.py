"""Host-independent path-style classification shared by Project and session state."""

from __future__ import annotations

import re

_WSL_UNC_PREFIX = re.compile(r"^//wsl(?:\.localhost|\$)/", re.IGNORECASE)
_WINDOWS_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:[\\/]")


def is_windows_path_style(path: str) -> bool:
    """Recognize drive/backslash paths and only the supported slash-form WSL UNC paths."""
    value = str(path)
    return bool(
        _WINDOWS_DRIVE_PREFIX.match(value)
        or value.startswith("\\")
        or _WSL_UNC_PREFIX.match(value)
    )
