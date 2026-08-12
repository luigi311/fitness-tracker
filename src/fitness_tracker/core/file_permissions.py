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
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
        )
    except FileNotFoundError:
        return

    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(errno.EINVAL, "Private file path is not a regular file", path)
        os.fchmod(descriptor, PRIVATE_FILE_MODE)
    finally:
        os.close(descriptor)
