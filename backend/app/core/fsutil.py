"""Cross-platform file helpers.

On Windows ``os.replace`` fails with PermissionError while another process
(antivirus, indexer, a reader) briefly holds the target file open. Retry a few
times before giving up; on macOS/Linux the first attempt always succeeds.
"""
from __future__ import annotations

import os
import time


def replace_with_retry(src: str | os.PathLike, dst: str | os.PathLike, attempts: int = 20, delay: float = 0.05) -> None:
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay * (i + 1))


def safe_chmod(path: str | os.PathLike, mode: int) -> None:
    """POSIX permissions; on Windows only the read-only bit exists, so ignore failures."""
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def display_name(filename: str | None, fallback: str = "file") -> str:
    """Basename for both '/' and '\\' separators (old browsers send C:\\fakepath\\x.pdf)."""
    name = (filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    return name or fallback
