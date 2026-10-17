"""Cross-SDK behavior through production runtime and four independent buses."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import can
import pytest
from linkerbot_arm import CANError as ArmCANError
from linkerbot_hand import TimeoutError as HandTimeoutError

from integration.devices import wait_until


def test_two_hands_two_arms_exchange_concurrently(four_devices):
    four_devices.command_and_observe()
    four_devices.command_and_observe(round_number=1)


@pytest.mark.parametrize(
    "four_devices", [True], indirect=True, ids=["duplicate-addresses"]
)
def test_identical_can_ids_on_separate_channels_never_mix_state(four_devices):
    peers = four_devices.peers
    assert (
        peers["left_hand"].wire.arbitration_id
        == peers["right_hand"].wire.arbitration_id
    )
    assert peers["left_arm"].wire.motor_ids == peers["right_arm"].wire.motor_ids
    # Each side accepts the same CAN IDs. Different device-side values must
    # remain distinct in both production SDK caches while all four run.
    four_devices.command_and_observe()
    four_devices.command_and_observe(round_number=2)


@pytest.mark.parametrize("affected", ["left_hand", "left_arm"])
def test_one_unresponsive_channel_can_close_and_rebuild_without_stopping_peers(
    four_devices, affected
):
    rig = four_devices
    survivors = tuple(name for name in rig.names if name != affected)
    rig.command_and_observe()
    device = rig.devices[affected]
    peer = rig.peers[affected]
    peer.set_responding(False)

    if affected.endswith("hand"):
        device.stop_polling()
        before = peer.dropped
        with ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="joint-timeout"
        ) as pool:
            failed_read = pool.submit(device.get_angles, timeout=0.3)
            wait_until(
                lambda: peer.dropped > before, description="a dropped hand request"
            )
            rig.command_and_observe(survivors, round_number=1)
            with pytest.raises(HandTimeoutError):
                failed_read.result(timeout=3)
    else:
        before = peer.dropped
        wait_until(lambda: peer.dropped > before, description="unanswered arm polling")
        # An unresponsive device is not a reported bus fault. Cached feedback
        # must age instead of becoming fresh merely because it was read.
        wait_until(
            lambda: not device.get_feedback().is_fresh(0.1),
            description="stale arm feedback",
        )
        old_samples = device.get_feedback()
        rig.command_and_observe(survivors, round_number=1)
        assert device.get_feedback() == old_samples

    rebuild_while_peers_progress(rig, affected)
    rig.command_and_observe(round_number=3)


def rebuild_while_peers_progress(rig, affected):
    """Keep live traffic running while the affected connection is rebuilt."""

    survivors = tuple(name for name in rig.names if name != affected)
    peer = rig.peers[affected]
    # Gate an initial position read rather than the Arm discovery handshake,
    # whose retry window is much shorter. The real constructor timeout stays
    # active; a blocked or failed rebuild must still fail this test.
    peer.hold_next_reply(b"\xc0" if affected.endswith("hand") else b"\x06")
    stop = Event()
    completed_rounds = []

    def traffic():
        while not stop.is_set():
            rig.command_and_observe(
                survivors, round_number=2 + len(completed_rounds) % 10
            )
            completed_rounds.append(len(completed_rounds))

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="joint-rebuild") as pool:
        flowing = pool.submit(traffic)
        rebuilding = None
        try:
            wait_until(
                lambda: completed_rounds or flowing.done(),
                description="ongoing peer traffic",
            )
            if flowing.done():
                flowing.result()
            rebuilding = pool.submit(rig.restart, affected)
            assert peer.reply_held.wait(3), (
                "Rebuild never reached the device response gate"
            )
            old_device = rig.devices[affected]
            if affected.endswith("hand"):
                assert not old_device.get_health().connected
            else:
                assert not old_device.get_feedback().connected
            before = len(completed_rounds)
            wait_until(
                lambda: (
                    len(completed_rounds) > before
                    or rebuilding.done()
                    or flowing.done()
                ),
                description="three peers progressing during the held rebuild",
            )
            if flowing.done():
                flowing.result()
            if rebuilding.done():
                rebuilding.result()
                pytest.fail("Rebuild returned before its held response was released")
            assert len(completed_rounds) > before
        finally:
            peer.reply_gate.set()
            stop.set()
            flowing.result(timeout=15)
            if rebuilding is not None:
                rebuilding.result(timeout=15)


def test_virtual_recipient_backpressure_is_local_to_one_arm_channel(four_devices):
    rig = four_devices
    affected = "left_arm"
    survivors = tuple(name for name in rig.names if name != affected)
    rig.command_and_observe()
    rig.devices[affected].stop_polling()
    # Checked writes use the polling FIFO and wait for physical dispatch. With
    # the producer stopped, they drain older SDK frames before a bounded
    # recipient could receive one and race with our deliberate queue fill.
    rig.devices[affected].set_velocities([0.6] * 7)
    peer = rig.peers[affected]
    with can.Bus(interface="virtual", channel=rig.channels[affected]) as request_bus:
        # Hold a real position request after the peer's first responding check.
        # Stopping SDK polling alone does not drain requests already in flight.
        peer.hold_next_reply(b"\x06")
        try:
            request = can.Message(
                arbitration_id=peer.wire.motor_ids[0],
                is_extended_id=False,
                data=b"\x06",
            )
            request_bus.send(request, timeout=0.2)
            assert peer.reply_held.wait(3), "The in-flight response was never held"
            peer.set_responding(False)
            # A bounded virtual recipient blocks SDK sends and device replies
            # alike. Silence the peer before attaching it, including the held
            # response, so this fault targets SDK sending rather than the model.
            with can.Bus(
                interface="virtual", channel=rig.channels[affected], rx_queue_size=1
            ) as blocked:
                request_bus.send(request, timeout=0.2)
                before = peer.dropped
                peer.reply_gate.set()
                wait_until(
                    lambda: peer.dropped > before or peer.errors,
                    description="the held response suppressed before backpressure",
                )
                assert not peer.errors, peer.errors
                # The queue is full before the real SDK writer starts. This
                # models receiver backpressure, not SDK FIFO-full or bus-off.
                with ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="joint-backpressure"
                ) as pool:
                    writing = pool.submit(
                        rig.devices[affected].set_velocities, [0.61] * 7
                    )
                    rig.command_and_observe(survivors, round_number=1)
                    with pytest.raises(
                        ArmCANError,
                        match="Could not send message to one or more recipients",
                    ):
                        writing.result(timeout=5)
                rig.command_and_observe(survivors, round_number=2)
                assert blocked.recv(timeout=0) is not None, (
                    "The bounded recipient was never filled"
                )
        finally:
            peer.reply_gate.set()
    rig.restart(affected)
    rig.command_and_observe(round_number=3)
