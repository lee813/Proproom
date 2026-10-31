"""ULIDs and UTC timestamps (handover §5.2, §5.3)."""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def ulid() -> str:
    t = int(time.time() * 1000)
    n = (t << 80) | int.from_bytes(os.urandom(10), "big")
    return "".join(_CROCKFORD[(n >> (5 * i)) & 31] for i in range(25, -1, -1))


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


_last = ""
_lock = threading.Lock()


def utcnow_monotonic() -> str:
    """Strictly increasing timestamp; rights_assessment.assessed_at relies on it (handover B, note 5)."""
    global _last
    with _lock:
        now = utcnow()
        while now <= _last:
            time.sleep(0.001)
            now = utcnow()
        _last = now
        return now
