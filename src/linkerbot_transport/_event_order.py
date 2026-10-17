"""Process-local ordering for send claims and received frames."""

from itertools import count
from threading import Lock

_events = count(1)
_lock = Lock()


def next_event_order() -> int:
    """Linearize an event without depending on clock resolution or epoch."""

    with _lock:
        return next(_events)
