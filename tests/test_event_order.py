"""Claim boundaries remain unambiguous when every timestamp is equal."""

import threading

from linkerbot_transport import SendOutcome, SendReceipt, SendResult
from linkerbot_transport.routing import ReceiveRouter, RoutedMessage
from linkerbot_transport.testing import FakeTransport


def test_equal_timestamps_preserve_preclaim_and_synchronous_reply_order(monkeypatch):
    transport = FakeTransport[int](monotonic=lambda: 10.0)
    transport.open()
    route = transport.subscribe_route(2)

    def send_with_reply(_message):
        transport.emit(2)

    monkeypatch.setattr(transport.current_dispatcher, "send_now", send_with_reply)
    try:
        transport.emit(2)
        receipt = transport.send(1)
        old, fresh = route.receive(timeout_s=0), route.receive(timeout_s=0)
        result = receipt.result()
        assert old.received_at_s == result.claimed_at_s == fresh.received_at_s == 10.0
        assert old.received_order < result.claimed_order < fresh.received_order
        assert receipt.claimed_order == result.claimed_order
        assert result.completed_at_s == 10.0
    finally:
        transport.close()


def test_claim_order_is_stable_and_missing_for_superseded_work():
    receipt = SendReceipt(monotonic=lambda: 1.0)
    assert receipt.claimed_order is None
    assert receipt._claim()
    first = receipt.claimed_order
    assert first is not None
    assert receipt._claim()
    assert receipt.claimed_order == first
    assert receipt._dispatch()
    assert receipt.result().claimed_order == first

    superseded = SendReceipt(monotonic=lambda: 1.0)
    assert superseded._supersede()
    assert superseded.claimed_order is None
    assert superseded.result().claimed_order is None


def test_legacy_metadata_constructors_keep_optional_order_defaults():
    result = SendResult(SendOutcome.DISPATCHED, 1, 0.0, 0.0, 0.0)
    routed = RoutedMessage("reply", "route", 1, 1, 0.0)
    assert result.claimed_order is None
    assert routed.received_order is None


def test_receive_order_is_recorded_before_route_extraction_can_pause():
    extracting, release = threading.Event(), threading.Event()

    def key_of(_message):
        extracting.set()
        assert release.wait(1)
        return "reply"

    router = ReceiveRouter(key_of, monotonic=lambda: 1.0)
    router.open(generation=1)
    route = router.subscribe("reply")
    receiver = threading.Thread(target=lambda: router.route("old", generation=1))
    receiver.start()
    try:
        assert extracting.wait(1)
        receipt = SendReceipt.completed(monotonic=lambda: 1.0)
        release.set()
        receiver.join(1)
        assert not receiver.is_alive()
        received = route.receive(timeout_s=0)
        assert received.received_order < receipt.claimed_order
        assert received.received_at_s == receipt.claimed_at_s
    finally:
        release.set()
        receiver.join(1)
        router.close()
