"""Worker queue that cancels obsolete reads while draining accepted writes."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from threading import RLock
from typing import TypeVar, cast

_T = TypeVar("_T")


class BackgroundExecutor:
    """Keep the existing serial execution order, with explicit read cancellation.

    ``prepare_shutdown`` never waits and is safe on the GUI thread. ``shutdown``
    joins workers and belongs on a closing worker once preparation has finished.
    Tasks default to required, so unclassified operations cannot lose writes.
    """

    def __init__(self, *, max_workers: int = 1, thread_name_prefix: str = "") -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix=thread_name_prefix
        )
        self._lock = RLock()
        self._reads: set[Future[object]] = set()
        self._closing = False

    def submit(self, work: Callable[[], _T], *, read_only: bool = False) -> Future[_T]:
        with self._lock:
            if self._closing:
                raise RuntimeError("Cannot submit work after shutdown has started")
            future = self._executor.submit(work)
            if read_only:
                tracked = cast(Future[object], future)
                self._reads.add(tracked)
                tracked.add_done_callback(self._read_done)
            return future

    def _read_done(self, future: Future[object]) -> None:
        with self._lock:
            self._reads.discard(future)

    def prepare_shutdown(self) -> None:
        with self._lock:
            self._closing = True
            reads = tuple(self._reads)
        for future in reads:
            future.cancel()

    def shutdown(self) -> None:
        self.prepare_shutdown()
        self._executor.shutdown(wait=True, cancel_futures=False)
