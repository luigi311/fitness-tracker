"""Permission helpers for files containing private fitness data."""

import errno
import os
import stat
from pathlib import Path

PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def secure_directory(path: Path) -> None:
    """Create a directory if needed and restrict it to the current user."""
    path.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIRECTORY_MODE)
    path.chmod(PRIVATE_DIRECTORY_MODE)


def secure_file(path: Path) -> None:
    """Restrict an existing private file to the current user."""
    try:
        descriptor = _open_private_file(path)
    except FileNotFoundError:
        return

    os.close(descriptor)


def read_private_file(path: Path) -> bytes:
    """Read a regular private file through one protected descriptor."""
    descriptor = _open_private_file(path)
    with os.fdopen(descriptor, "rb") as stream:
        return stream.read()


def _open_private_file(path: Path) -> int:
    """Open and secure a regular file without following symbolic links."""
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
    )

    try:
        _validate_regular_file(descriptor, path)
        os.fchmod(descriptor, PRIVATE_FILE_MODE)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _validate_regular_file(descriptor: int, path: Path) -> None:
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        raise OSError(errno.EINVAL, "Private file path is not a regular file", path)
