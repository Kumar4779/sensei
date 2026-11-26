"""Owner-only files and directories (Slice 33, DESIGN.md Threat 4).

Everything content-bearing that Sensei writes under its home is created
owner-only, whatever the process umask: directories 0700 and files 0600. The
mode is set explicitly at creation (os.open with the mode, then fchmod or
chmod), never left to the umask. Opening an existing store tightens only the
directories and files that store owns, and only if this user owns them.

The containment probe (Slice 32) checks the trace store from outside. This
module is the fix behind that check.
"""

from __future__ import annotations

import os
from pathlib import Path

DIR_MODE = 0o700
FILE_MODE = 0o600


def _tighten(path: Path, mode: int) -> None:
    try:
        if path.stat().st_uid == os.getuid() and (path.stat().st_mode & 0o777) != mode:
            os.chmod(path, mode)
    except (FileNotFoundError, PermissionError):
        pass  # not ours to change; the containment probe reports what is left


def private_dir(path: str | Path) -> Path:
    """Create (or tighten) one store directory to 0700. Parents are created but never changed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.mkdir(mode=DIR_MODE)
    except FileExistsError:
        pass
    _tighten(path, DIR_MODE)
    return path


def tighten_file(path: str | Path) -> None:
    """Make an existing file this store owns 0600; a missing file is left missing."""
    _tighten(Path(path), FILE_MODE)


def _open_private(path: Path, flags: int) -> int:
    fd = os.open(path, flags | os.O_CREAT, FILE_MODE)
    try:
        os.fchmod(fd, FILE_MODE)  # exact, whatever the umask or the file's old mode
    except BaseException:
        os.close(fd)
        raise
    return fd


def append_private(path: str | Path, text: str) -> None:
    """Append text to a 0600 file, creating it owner-only."""
    fd = _open_private(Path(path), os.O_WRONLY | os.O_APPEND)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(text)


def write_private(path: str | Path, text: str) -> None:
    """Replace a file atomically with a 0600 one: the temporary file is created owner-only too."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    fd = _open_private(tmp, os.O_WRONLY | os.O_TRUNC)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
