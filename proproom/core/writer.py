"""The single write queue (handover §5.3): every write to db.sqlite goes through one thread.

Callers submit a function that receives the writer's connection; it runs inside BEGIN IMMEDIATE … COMMIT,
so redundant search columns (vec0 metadata, search_doc) are updated in the same transaction as the source row.
"""
from __future__ import annotations

import queue
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable

from . import db

WriteFn = Callable[[Any], Any]


class Writer:
    def __init__(self, path: Path):
        self._path = path
        self._q: queue.Queue[tuple[WriteFn, Future] | None] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="propd-writer", daemon=True)
        self._thread.start()

    def submit(self, fn: WriteFn) -> Future:
        fut: Future = Future()
        self._q.put((fn, fut))
        return fut

    def write(self, fn: WriteFn, timeout: float | None = None) -> Any:
        return self.submit(fn).result(timeout)

    def close(self) -> None:
        self._q.put(None)
        self._thread.join()

    def _run(self) -> None:
        conn = db.connect(self._path)
        try:
            while (item := self._q.get()) is not None:
                fn, fut = item
                if not fut.set_running_or_notify_cancel():
                    continue
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    result = fn(conn)
                    conn.execute("COMMIT")
                    fut.set_result(result)
                except BaseException as e:  # noqa: BLE001 - surfaced through the future
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    fut.set_exception(e)
        finally:
            conn.close()
