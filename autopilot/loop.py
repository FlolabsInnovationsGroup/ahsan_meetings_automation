"""Runs the autopilot's step every minute in a thread of its own, and at once
when Zoom's webhook calls or someone switches it on. Started by the API at
startup; a step does nothing while the switch is off, and a step that fails
is logged and the next one runs as usual."""

from __future__ import annotations

import threading

from core.logging import get_logger

from .engine import Engine

logger = get_logger(__name__)

STEP_EVERY_S = 60.0

_lock = threading.Lock()
_engine: Engine | None = None
_wake = threading.Event()
_stop = threading.Event()
_thread: threading.Thread | None = None


def start(engine: Engine) -> None:
    global _engine, _thread
    with _lock:
        _engine = engine
        if _thread is not None and _thread.is_alive():
            return
        _stop.clear()
        _thread = threading.Thread(target=_run, name="autopilot", daemon=True)
        _thread.start()
    logger.info("autopilot started")


def stop() -> None:
    _stop.set()
    _wake.set()


def engine() -> Engine | None:
    return _engine


def set_engine(engine: Engine | None) -> None:
    """For the API routes when the loop isn't running (autopilot off) and for tests."""
    global _engine
    with _lock:
        _engine = engine


def wake() -> None:
    _wake.set()


def _run() -> None:
    while not _stop.is_set():
        _wake.clear()
        current = _engine
        if current is not None:
            try:
                current.step()
            except Exception:  # the autopilot must outlive any one bad step
                logger.exception("autopilot step failed")
        _wake.wait(STEP_EVERY_S)
