"""Bounded constant-time receive routing for datagram transports."""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from collections.abc import Callable, Hashable, Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Generic, TypeVar

from ._event_order import next_event_order
from .errors import (
    ClosedError,
    DeviceNotReadyError,
    DeviceStateError,
    QueueFullError,
    TimeoutError,
    ValidationError,
    normalize_error,
)

MessageT = TypeVar("MessageT")
RouteKeyT = TypeVar("RouteKeyT", bound=Hashable)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CANRouteKey:
    """Exact CAN/CAN FD receive route based on identifier and frame format."""

    arbitration_id: int
    is_extended_id: bool

    def __post_init__(self) -> None:
        if not isinstance(self.arbitration_id, int) or isinstance(
            self.arbitration_id, bool
        ):
            raise ValidationError("arbitration_id must be an int")
        if not isinstance(self.is_extended_id, bool):
            raise ValidationError("is_extended_id must be a bool")
        maximum = 0x1FFFFFFF if self.is_extended_id else 0x7FF
        if self.arbitration_id < 0 or self.arbitration_id > maximum:
            kind = "extended" if self.is_extended_id else "standard"
            raise ValidationError(f"{kind} arbitration_id must fit in 0x{maximum:X}")


@dataclass(frozen=True, slots=True)
class RoutedMessage(Generic[MessageT, RouteKeyT]):
    """One received message with immutable routing and connection metadata."""

    message: MessageT
    route_key: RouteKeyT
    generation: int
    sequence: int
    received_at_s: float
    received_order: int | None = None


@dataclass(frozen=True, slots=True)
class RouteHealth(Generic[RouteKeyT]):
    """Zero-I/O snapshot for one bounded route inbox."""

    route_key: RouteKeyT
    generation: int
    active: bool
    capacity: int
    queue_depth: int
    delivered: int
    overflows: int
    dropped_on_close: int
    route_keys: tuple[RouteKeyT, ...] = ()


@dataclass(frozen=True, slots=True)
class ReceiveRouterHealth:
    """Zero-I/O aggregate receive-router state and counters."""

    running: bool
    generation: int
    route_count: int
    queue_depth: int
    max_queue_depth: int
    received: int
    routed: int
    route_misses: int
    overflows: int
    stale_frames: int
    key_errors: int
    registration_rejections: int
    dropped_on_close: int


class _OfferResult(Enum):
    ACCEPTED = 1
    FULL = 2
    CLOSED = 3


class _RouteState(Generic[MessageT, RouteKeyT]):
    def __init__(
        self,
        route_keys: tuple[RouteKeyT, ...],
        generation: int,
        capacity: int,
        router_lock: threading.RLock,
        on_dequeue: Callable[[], None],
        on_available: Callable[[], None] | None,
    ) -> None:
        self.route_keys = route_keys
        self.generation = generation
        self.capacity = capacity
        self._on_dequeue = on_dequeue
        self._items: deque[RoutedMessage[MessageT, RouteKeyT]] = deque()
        self._condition = threading.Condition(router_lock)
        self._on_available = on_available
        self._notification_lock = threading.RLock()
        self._notifying = False
        self._terminal_notification_pending = False
        self._active = True
        self._close_error: BaseException = ClosedError("receive route is closed")
        self._delivered = 0
        self._overflows = 0
        self._dropped_on_close = 0

    @property
    def route_key(self) -> RouteKeyT:
        """Return the primary key retained for the single-route API."""

        return self.route_keys[0]

    def offer(
        self, message: RoutedMessage[MessageT, RouteKeyT]
    ) -> tuple[_OfferResult, bool]:
        with self._condition:
            if not self._active:
                return _OfferResult.CLOSED, False
            if len(self._items) >= self.capacity:
                self._overflows += 1
                return _OfferResult.FULL, False
            was_empty = not self._items
            self._items.append(message)
            self._delivered += 1
            self._condition.notify()
            return _OfferResult.ACCEPTED, was_empty

    def receive(self, timeout_s: float | None) -> RoutedMessage[MessageT, RouteKeyT]:
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        with self._condition:
            while not self._items:
                if not self._active:
                    raise self._close_error
                remaining = _remaining(deadline)
                if remaining == 0:
                    raise TimeoutError("no routed message received before timeout")
                self._condition.wait(remaining)
            message = self._items.popleft()
            self._on_dequeue()
            return message

    def close(self, error: BaseException) -> tuple[int, bool]:
        with self._condition:
            if not self._active:
                return 0, False
            self._active = False
            self._close_error = error
            dropped = len(self._items)
            self._items.clear()
            self._dropped_on_close += dropped
            self._condition.notify_all()
            return dropped, True

    def notify_available(self) -> None:
        """Run the edge-triggered notifier outside the receive-router lock."""

        with self._notification_lock:
            with self._condition:
                if not self._active:
                    return
            self._invoke_notification_locked()
            self._flush_terminal_notification_locked()

    def notify_closed(self) -> None:
        """Wake a consumer once when this route reaches a terminal state."""

        with self._notification_lock:
            if self._notifying:
                self._terminal_notification_pending = True
                return
            self._invoke_notification_locked()

    def _flush_terminal_notification_locked(self) -> None:
        if not self._terminal_notification_pending:
            return
        self._terminal_notification_pending = False
        self._invoke_notification_locked()

    def _invoke_notification_locked(self) -> None:
        callback = self._on_available
        if callback is None:
            return
        self._notifying = True
        try:
            callback()
        except Exception:  # noqa: BLE001 - notifier cannot terminate transport RX
            self._on_available = None
            _LOGGER.exception(
                "receive-route availability notifier failed; disabling it"
            )
        finally:
            self._notifying = False

    def get_health(self) -> RouteHealth[RouteKeyT]:
        with self._condition:
            return RouteHealth(
                route_key=self.route_key,
                generation=self.generation,
                active=self._active,
                capacity=self.capacity,
                queue_depth=len(self._items),
                delivered=self._delivered,
                overflows=self._overflows,
                dropped_on_close=self._dropped_on_close,
                route_keys=self.route_keys,
            )


class RouteSubscription(Generic[MessageT, RouteKeyT]):
    """Exclusive consumer handle for one route's bounded receive inbox."""

    def __init__(
        self,
        router: ReceiveRouter[MessageT, RouteKeyT],
        state: _RouteState[MessageT, RouteKeyT],
    ) -> None:
        self._router = router
        self._state = state
        self._close_lock = threading.RLock()
        self._closed = False

    @property
    def route_key(self) -> RouteKeyT:
        """Return the primary exact key owned by this subscription."""

        return self._state.route_key

    @property
    def route_keys(self) -> tuple[RouteKeyT, ...]:
        """Return every exact key feeding this subscription's shared inbox."""

        return self._state.route_keys

    @property
    def generation(self) -> int:
        """Return the connection generation that created this subscription."""

        return self._state.generation

    def receive(
        self, *, timeout_s: float | None = None
    ) -> RoutedMessage[MessageT, RouteKeyT]:
        """Wait for a matching message, optionally with a bounded timeout."""

        _validate_timeout(timeout_s)
        return self._state.receive(timeout_s)

    def try_receive(self) -> RoutedMessage[MessageT, RouteKeyT] | None:
        """Return the next matching message immediately, or ``None``."""

        try:
            return self._state.receive(0.0)
        except TimeoutError:
            return None

    def get_health(self) -> RouteHealth[RouteKeyT]:
        """Return current inbox counters without transport I/O."""

        return self._state.get_health()

    def close(self) -> None:
        """Unregister and close this route idempotently."""

        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._router._unregister(self._state)

    def __enter__(self) -> RouteSubscription[MessageT, RouteKeyT]:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()


class ReceiveRouter(Generic[MessageT, RouteKeyT]):
    """Route frames by one hash lookup into exclusive bounded inboxes."""

    def __init__(
        self,
        key_of: Callable[[MessageT], RouteKeyT],
        *,
        max_routes: int = 64,
        default_queue_capacity: int = 256,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not callable(key_of):
            raise ValidationError("key_of must be callable")
        _validate_positive_int(max_routes, "max_routes")
        _validate_positive_int(default_queue_capacity, "default_queue_capacity")
        if not callable(monotonic):
            raise ValidationError("monotonic must be callable")
        self._key_of = key_of
        self._max_routes = max_routes
        self._default_queue_capacity = default_queue_capacity
        self._monotonic = monotonic
        self._lock = threading.RLock()
        self._routes: dict[RouteKeyT, _RouteState[MessageT, RouteKeyT]] = {}
        self._running = False
        self._generation = 0
        self._sequence = 0
        self._queue_depth = 0
        self._max_queue_depth = 0
        self._received = 0
        self._routed = 0
        self._route_misses = 0
        self._overflows = 0
        self._stale_frames = 0
        self._key_errors = 0
        self._registration_rejections = 0
        self._dropped_on_close = 0

    def open(self, *, generation: int) -> None:
        """Open for a strictly newer connection generation."""

        _validate_generation(generation)
        with self._lock:
            if self._running:
                if self._generation == generation:
                    return
                raise DeviceNotReadyError(
                    "receive router is already open for another generation"
                )
            if generation <= self._generation:
                raise DeviceNotReadyError(
                    "receive router generation must increase after close"
                )
            if self._routes:
                raise DeviceStateError(
                    "closed receive router retained registered routes"
                )
            self._generation = generation
            self._sequence = 0
            self._running = True

    def close(self) -> None:
        """Close all routes, discard queued frames and wake consumers."""

        self._stop(ClosedError("receive router closed"))

    def abort(self, error: BaseException) -> None:
        """Close all routes with a stable transport failure."""

        if not isinstance(error, BaseException):
            raise ValidationError("error must be a BaseException")
        self._stop(normalize_error(error))

    def record_stale(self) -> None:
        """Count a frame rejected by an outer lifecycle generation gate."""

        with self._lock:
            self._received += 1
            self._stale_frames += 1

    def subscribe(
        self,
        route_key: RouteKeyT,
        *,
        queue_capacity: int | None = None,
        on_available: Callable[[], None] | None = None,
    ) -> RouteSubscription[MessageT, RouteKeyT]:
        """Register one exclusive bounded inbox for ``route_key``."""

        _validate_hashable(route_key, "route_key")
        return self._subscribe(
            (route_key,),
            queue_capacity=queue_capacity,
            on_available=on_available,
        )

    def subscribe_routes(
        self,
        route_keys: Iterable[RouteKeyT],
        *,
        queue_capacity: int | None = None,
        on_available: Callable[[], None] | None = None,
    ) -> RouteSubscription[MessageT, RouteKeyT]:
        """Atomically route several exact keys into one bounded inbox.

        Registration consumes one route-registry slot per exact key while the
        queue capacity applies to the combined stream. No key is registered if
        validation, exclusivity, or registry-capacity checks fail.
        """

        keys = tuple(route_keys)
        if not keys:
            raise ValidationError("route_keys must not be empty")
        for route_key in keys:
            _validate_hashable(route_key, "route_keys item")
        if len(set(keys)) != len(keys):
            raise ValidationError("route_keys must not contain duplicates")
        return self._subscribe(
            keys,
            queue_capacity=queue_capacity,
            on_available=on_available,
        )

    def _subscribe(
        self,
        route_keys: tuple[RouteKeyT, ...],
        *,
        queue_capacity: int | None,
        on_available: Callable[[], None] | None,
    ) -> RouteSubscription[MessageT, RouteKeyT]:
        capacity = self._default_queue_capacity
        if queue_capacity is not None:
            _validate_positive_int(queue_capacity, "queue_capacity")
            capacity = queue_capacity
        if on_available is not None and not callable(on_available):
            raise ValidationError("on_available must be callable")
        with self._lock:
            if not self._running:
                raise ClosedError("receive router is closed")
            if any(route_key in self._routes for route_key in route_keys):
                raise DeviceStateError(
                    "one or more receive routes are already registered"
                )
            if len(self._routes) + len(route_keys) > self._max_routes:
                self._registration_rejections += 1
                raise QueueFullError("receive route registry is full")
            state = _RouteState(
                route_keys,
                self._generation,
                capacity,
                self._lock,
                self._on_dequeue,
                on_available,
            )
            for route_key in route_keys:
                self._routes[route_key] = state
            return RouteSubscription(self, state)

    def route(self, message: MessageT, *, generation: int) -> bool:
        """Route one frame without scanning registered endpoints."""

        _validate_generation(generation)
        notify_state: _RouteState[MessageT, RouteKeyT] | None = None
        accepted = False
        with self._lock:
            self._received += 1
            if not self._running or generation != self._generation:
                self._stale_frames += 1
                return False
            received_order = next_event_order()
        try:
            route_key = self._key_of(message)
            _validate_hashable(route_key, "extracted route_key")
        except Exception:  # noqa: BLE001 - malformed frames must not kill RX
            with self._lock:
                self._route_misses += 1
                self._key_errors += 1
            return False

        with self._lock:
            if not self._running or generation != self._generation:
                self._stale_frames += 1
                return False
            state = self._routes.get(route_key)
            if state is None:
                self._route_misses += 1
                return False
            self._sequence += 1
            routed = RoutedMessage(
                message=message,
                route_key=route_key,
                generation=generation,
                sequence=self._sequence,
                received_at_s=self._monotonic(),
                received_order=received_order,
            )
            result, became_available = state.offer(routed)
            if result is _OfferResult.ACCEPTED:
                self._routed += 1
                self._queue_depth += 1
                self._max_queue_depth = max(self._max_queue_depth, self._queue_depth)
                accepted = True
                if became_available:
                    notify_state = state
            elif result is _OfferResult.FULL:
                self._overflows += 1
            else:
                self._route_misses += 1
        if notify_state is not None:
            notify_state.notify_available()
        return accepted

    def get_health(self) -> ReceiveRouterHealth:
        """Return aggregate counters without calling the transport backend."""

        with self._lock:
            return ReceiveRouterHealth(
                running=self._running,
                generation=self._generation,
                route_count=len(self._routes),
                queue_depth=self._queue_depth,
                max_queue_depth=self._max_queue_depth,
                received=self._received,
                routed=self._routed,
                route_misses=self._route_misses,
                overflows=self._overflows,
                stale_frames=self._stale_frames,
                key_errors=self._key_errors,
                registration_rejections=self._registration_rejections,
                dropped_on_close=self._dropped_on_close,
            )

    def _unregister(self, state: _RouteState[MessageT, RouteKeyT]) -> None:
        notify_closed = False
        with self._lock:
            for route_key in state.route_keys:
                if self._routes.get(route_key) is state:
                    del self._routes[route_key]
            dropped, notify_closed = state.close(ClosedError("receive route is closed"))
            self._queue_depth -= dropped
            self._dropped_on_close += dropped
        if notify_closed:
            state.notify_closed()

    def _on_dequeue(self) -> None:
        with self._lock:
            if self._queue_depth <= 0:
                raise DeviceStateError("receive router queue depth became inconsistent")
            self._queue_depth -= 1

    def _stop(self, error: BaseException) -> None:
        closed_states: list[_RouteState[MessageT, RouteKeyT]] = []
        with self._lock:
            if not self._running and not self._routes:
                return
            self._running = False
            states = tuple(dict.fromkeys(self._routes.values()))
            self._routes.clear()
            for state in states:
                dropped, notify_closed = state.close(error)
                self._queue_depth -= dropped
                self._dropped_on_close += dropped
                if notify_closed:
                    closed_states.append(state)
        for state in closed_states:
            state.notify_closed()


def _validate_hashable(value: Hashable, name: str) -> None:
    if value is None:
        raise ValidationError(f"{name} must not be None")
    try:
        hash(value)
    except TypeError as error:
        raise ValidationError(f"{name} must be hashable") from error


def _validate_generation(generation: int) -> None:
    if not isinstance(generation, int) or isinstance(generation, bool):
        raise ValidationError("generation must be an int")
    if generation < 0:
        raise ValidationError("generation must be non-negative")


def _validate_positive_int(value: int, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValidationError(f"{name} must be a positive int")


def _validate_timeout(timeout_s: float | None) -> None:
    if timeout_s is None:
        return
    if (
        not isinstance(timeout_s, (int, float))
        or isinstance(timeout_s, bool)
        or not math.isfinite(timeout_s)
        or timeout_s < 0
    ):
        raise ValidationError("timeout_s must be finite and non-negative")


def _remaining(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


__all__ = [
    "CANRouteKey",
    "ReceiveRouter",
    "ReceiveRouterHealth",
    "RouteHealth",
    "RouteSubscription",
    "RoutedMessage",
]
