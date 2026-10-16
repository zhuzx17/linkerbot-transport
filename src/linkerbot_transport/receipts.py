"""Observable completion of one accepted transport send."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from enum import Enum

from ._event_order import next_event_order


class SendOutcome(str, Enum):
    """Terminal outcome of an accepted transport send."""

    DISPATCHED = "dispatched"
    SUPERSEDED = "superseded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SendResult:
    """Immutable timing and generation metadata for a completed send."""

    outcome: SendOutcome
    generation: int
    accepted_at_s: float
    claimed_at_s: float | None
    completed_at_s: float
    claimed_order: int | None = None

    @property
    def queue_delay_s(self) -> float | None:
        """Return time from acceptance to atomic transport claim."""

        if self.claimed_at_s is None:
            return None
        return max(0.0, self.claimed_at_s - self.accepted_at_s)


class SendReceipt(Future[SendResult]):
    """Result completed only after the physical backend accepts one message.

    A receipt represents an already accepted send, so callers cannot cancel it.
    Replacement completes with ``SUPERSEDED``; lifecycle and transport failures
    complete with the corresponding stable exception.
    """

    def __init__(
        self,
        *,
        generation: int = 0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(generation, int) or isinstance(generation, bool):
            raise TypeError("generation must be an int")
        if generation < 0:
            raise ValueError("generation must be non-negative")
        if not callable(monotonic):
            raise TypeError("monotonic must be callable")
        super().__init__()
        self._monotonic = monotonic
        self._generation = generation
        self._accepted_at_s = monotonic()
        self._claimed_at_s: float | None = None
        self._claimed_order: int | None = None
        self._outcome: SendOutcome | None = None
        self._transition_lock = threading.RLock()
        self.set_running_or_notify_cancel()

    @property
    def generation(self) -> int:
        """Connection generation in which this send was accepted."""

        return self._generation

    @property
    def accepted_at_s(self) -> float:
        """Monotonic acceptance timestamp."""

        return self._accepted_at_s

    @property
    def claimed_at_s(self) -> float | None:
        """Monotonic atomic-claim timestamp, if the send was claimed."""

        with self._transition_lock:
            return self._claimed_at_s

    @property
    def outcome(self) -> SendOutcome | None:
        """Return the terminal outcome without raising a send failure."""

        with self._transition_lock:
            return self._outcome

    @property
    def claimed_order(self) -> int | None:
        """Process-local event order at claim, independent of clock precision."""

        with self._transition_lock:
            return self._claimed_order

    @classmethod
    def completed(
        cls,
        *,
        generation: int = 0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> SendReceipt:
        """Create a receipt for a message already sent successfully."""

        receipt = cls(generation=generation, monotonic=monotonic)
        receipt._claim()
        receipt._dispatch()
        return receipt

    @classmethod
    def failed(
        cls,
        error: BaseException,
        *,
        generation: int = 0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> SendReceipt:
        """Create a receipt completed with ``error``."""

        if not isinstance(error, BaseException):
            raise TypeError("error must be a BaseException")
        receipt = cls(generation=generation, monotonic=monotonic)
        receipt._fail(error)
        return receipt

    def _claim(self) -> bool:
        """Atomically record that replacement is no longer allowed."""

        with self._transition_lock:
            if self.done():
                return False
            if self._claimed_at_s is None:
                self._claimed_order = next_event_order()
                self._claimed_at_s = self._monotonic()
            return True

    def _dispatch(self) -> bool:
        with self._transition_lock:
            if self.done():
                return False
            now = self._monotonic()
            if self._claimed_at_s is None:
                self._claimed_order = next_event_order()
                self._claimed_at_s = now
            self._outcome = SendOutcome.DISPATCHED
            self.set_result(
                SendResult(
                    outcome=SendOutcome.DISPATCHED,
                    generation=self._generation,
                    accepted_at_s=self._accepted_at_s,
                    claimed_at_s=self._claimed_at_s,
                    completed_at_s=now,
                    claimed_order=self._claimed_order,
                )
            )
            return True

    def _supersede(self) -> bool:
        with self._transition_lock:
            if self.done() or self._claimed_at_s is not None:
                return False
            now = self._monotonic()
            self._outcome = SendOutcome.SUPERSEDED
            self.set_result(
                SendResult(
                    outcome=SendOutcome.SUPERSEDED,
                    generation=self._generation,
                    accepted_at_s=self._accepted_at_s,
                    claimed_at_s=None,
                    completed_at_s=now,
                )
            )
            return True

    def _fail(self, error: BaseException) -> bool:
        if not isinstance(error, BaseException):
            raise TypeError("error must be a BaseException")
        with self._transition_lock:
            if self.done():
                return False
            self._outcome = SendOutcome.FAILED
            self.set_exception(error)
            return True


class BatchSendReceipt(SendReceipt):
    """One atomically admitted FIFO batch, with observable physical progress.

    ``sent_frames`` includes only successful backend sends. A failure may leave
    a physically sent prefix; admission is atomic, hardware execution is not.
    """

    def __init__(self, *, frame_count: int, **kwargs) -> None:
        if type(frame_count) is not int or frame_count <= 0:
            raise ValueError("frame_count must be a positive int")
        super().__init__(**kwargs)
        self._frame_count = frame_count
        self._sent_frames = 0

    @property
    def frame_count(self) -> int:
        """Total number of frames atomically admitted in this batch."""
        return self._frame_count

    @property
    def sent_frames(self) -> int:
        with self._transition_lock:
            return self._sent_frames

    def _frame_dispatched(self) -> None:
        with self._transition_lock:
            self._sent_frames += 1


class _ReceiptTracker:
    """Thread-safe ownership of receipts not yet completed by a send loop."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: set[SendReceipt] = set()

    def register(self, receipt: SendReceipt) -> None:
        with self._lock:
            self._pending.add(receipt)

    def discard(self, receipt: SendReceipt) -> None:
        with self._lock:
            self._pending.discard(receipt)

    def succeed(self, receipt: SendReceipt) -> None:
        if self._take(receipt):
            receipt._dispatch()

    def fail(self, receipt: SendReceipt, error: BaseException) -> None:
        if self._take(receipt):
            receipt._fail(error)

    def fail_all(self, error: BaseException) -> None:
        with self._lock:
            receipts = tuple(self._pending)
            self._pending.clear()
        for receipt in receipts:
            receipt._fail(error)

    def _take(self, receipt: SendReceipt) -> bool:
        with self._lock:
            if receipt not in self._pending:
                return False
            self._pending.remove(receipt)
            return True


__all__ = ["BatchSendReceipt", "SendOutcome", "SendReceipt", "SendResult"]
