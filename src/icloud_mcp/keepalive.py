"""One background thread that keeps pooled connections warm between tool calls.

Services register a tick(now) method. It runs every few seconds and decides for itself whether anything is due: ping a pooled
connection before the server's idle timeout closes it, rebuild one that is too old, or drop everything once the server has been
unused for a while. Registration holds only a weak reference, so a service that goes away simply stops being ticked, and a
failing tick is logged and never stops the others.
"""
from __future__ import annotations

import logging
import threading
import time
import weakref
from typing import Callable

log = logging.getLogger("icloud_mcp.keepalive")

INTERVAL_SECONDS = 3.0


class Ticker:
    def __init__(self, interval: float = INTERVAL_SECONDS):
        self.interval = interval
        self._lock = threading.Lock()
        self._jobs: list[weakref.WeakMethod] = []
        self._thread: threading.Thread | None = None

    def add(self, bound_method: Callable[[float], None]) -> None:
        with self._lock:
            if any(ref() == bound_method for ref in self._jobs):
                return
            self._jobs.append(weakref.WeakMethod(bound_method))
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="icloud-mcp-keepalive", daemon=True)
                self._thread.start()

    def tick(self, now: float | None = None) -> None:
        """One round over every live job (also callable directly, which is how the tests drive it)."""
        now = time.monotonic() if now is None else now
        with self._lock:
            self._jobs = [ref for ref in self._jobs if ref() is not None]
            jobs = [ref() for ref in self._jobs]
        for job in jobs:
            if job is None:
                continue
            try:
                job(now)
            except Exception:  # noqa: BLE001 - a keep-alive failure only means the next call connects afresh
                log.debug("keep-alive tick failed", exc_info=True)

    def _run(self) -> None:
        while True:
            time.sleep(self.interval)
            self.tick()


TICKER = Ticker()
