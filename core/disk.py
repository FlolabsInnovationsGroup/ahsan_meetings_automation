"""Making room on the disk before a job gives up for lack of it.

The service (api.main) registers how: delete the oldest finished jobs, with
the same care as the low-disk cleanup (never a running job, an upload, or
one the autopilot still posts). Without a registered cleaner, as in a script
or a test, nothing is deleted.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

from core.logging import get_logger

logger = get_logger(__name__)

_cleaner: Callable[[int], int] | None = None


def set_cleaner(fn: Callable[[int], int] | None) -> None:
    """`fn(want_free_bytes)` deletes old finished jobs until that much is free
    (or none are left); returns how many it deleted."""
    global _cleaner
    _cleaner = fn


def free_bytes(folder: Path, want: int) -> int:
    """Free space under `folder`; when it is below `want`, old finished jobs
    are deleted first to get there."""
    free = shutil.disk_usage(folder).free
    if free < want and _cleaner is not None:
        try:
            _cleaner(want)
        except Exception:  # a failed cleanup must not hide the real shortage
            logger.exception("could not make room on the disk")
        free = shutil.disk_usage(folder).free
    return free
