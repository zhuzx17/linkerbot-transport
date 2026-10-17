"""Device-side CAN peers; SDKs, codecs, transports and workers remain real.

This is a deliberately small L6/A7 wire model, not a device physics or firmware
simulator. The fixture only creates isolated virtual CAN channels.
"""

from __future__ import annotations

import struct
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import can
from linkerbot_arm import A7
from linkerbot_arm.arm.a7.motor import SensorType
from linkerbot_hand import CommandStage, DeviceConfig, HandDevice, PollingPlan

from linkerbot_transport.can import CANTransport


def wait_until(predicate, *, timeout=5.0, description="condition"):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(f"Timed out waiting for {description}")
        threading.Event().wait(0.005)


class HandWire:
    """L6 identity, angle read/write and a small set of optional telemetry."""

    def __init__(self, side, marker):
        self.side = side
        self.arbitration_id = 0x28 if side == "left" else 0x27
        self.angles = (marker,) * 6
        self.commands = []

    def respond(self, message):
        if (
            message.is_extended_id
            or message.is_fd
            or message.arbitration_id != self.arbitration_id
        ):
            return ()
        data = bytes(message.data)
        if data == b"\xc0":
            side_code = "L" if self.side == "left" else "R"
            product = f"LHL6-04-077-{side_code}-B-1-A".encode("ascii")
            payloads = [
                bytes((0xC0, offset // 6)) + product[offset : offset + 6]
                for offset in range(0, len(product), 6)
            ]
        elif data in (b"\xc1", b"\xc1\x01"):
            payloads = [b"\xc1\x01\x04\x00\x01"]
        elif data == b"\xc2":
            payloads = [b"\xc2\x02\x04\x00"]
        elif data == b"\xc4":
            payloads = [b"\xc4\x01\x00\x02"]
        elif data == b"\x01":
            payloads = [bytes((1, *self.angles))]
        elif data == b"\x35":
            payloads = [b"\x35" + bytes(6)]
        elif data == b"\x02":
            payloads = [b"\x02" + bytes((80,)) * 6]
        elif len(data) == 7 and data[0] == 1:
            self.angles = tuple(data[1:])
            self.commands.append(self.angles)
            payloads = [data]
        else:
            return ()
        return tuple(
            can.Message(
                arbitration_id=self.arbitration_id, is_extended_id=False, data=payload
            )
            for payload in payloads
        )


class ArmWire:
    """Seven A7 motor registers with device-side velocity write/read feedback."""

    def __init__(self, side, marker):
        self.motor_ids = tuple(range(61, 68) if side == "left" else range(51, 58))
        self.registers = {
            mid: {
                0x06: marker + index / 1000,
                0x05: 0.0,
                0x03: marker,
                0x5F: 25.0,
                0x1C: 0.5,
                0x1D: 1.0,
                0x19: 1.0,
                0x17: 1.0,
                0x18: 1.0,
            }
            for index, mid in enumerate(self.motor_ids)
        }
        self.enabled = dict.fromkeys(self.motor_ids, False)
        self.commands = []

    def respond(self, message):
        mid = message.arbitration_id
        if message.is_extended_id or message.is_fd or mid not in self.registers:
            return ()
        data = bytes(message.data)
        if len(data) == 1:
            opcode = data[0]
            if opcode == 0x01:
                payload = b"\x01\x01"
            elif opcode == 0x2B:
                payload = bytes((opcode, self.enabled[mid]))
            elif opcode in self.registers[mid]:
                payload = bytes((opcode,)) + struct.pack(
                    "<f", self.registers[mid][opcode]
                )
            else:
                return ()
            return (
                can.Message(arbitration_id=mid, is_extended_id=False, data=payload),
            )
        if len(data) == 5 and data[0] == 0x1F:
            velocity = struct.unpack("<f", data[1:])[0]
            self.registers[mid][0x1C] = velocity
            self.registers[mid][0x05] = velocity
            self.commands.append((mid, velocity))
        elif len(data) == 2 and data[0] == 0x2A:
            self.enabled[mid] = bool(data[1])
        return ()


class CANPeer:
    """An independently running device peer with observable response loss."""

    def __init__(self, backend, channel, wire):
        self.wire = wire
        self.lock = threading.RLock()
        self.responding = True
        self.requests = Counter()
        self.dropped = 0
        self.errors = []
        self.stop = threading.Event()
        self.reply_gate = threading.Event()
        self.reply_gate.set()
        self.reply_held = threading.Event()
        self._held_opcode = None
        self.bus = can.Bus(interface=backend, channel=channel)
        self.worker = threading.Thread(
            target=self._run, name=f"joint-device[{channel}]"
        )
        self.worker.start()

    def _run(self):
        try:
            while not self.stop.is_set():
                message = self.bus.recv(0.02)
                if message is None:
                    continue
                with self.lock:
                    self.requests[(message.arbitration_id, bytes(message.data))] += 1
                    if not self.responding:
                        self.dropped += 1
                        continue
                    hold_reply = bytes(message.data) == self._held_opcode
                    if hold_reply:
                        self._held_opcode = None
                if hold_reply:
                    # Wait outside the peer lock so cleanup and diagnostic
                    # readers can always release the deterministic delay.
                    self.reply_held.set()
                    while not self.reply_gate.wait(0.02):
                        if self.stop.is_set():
                            return
                with self.lock:
                    # A request may have passed the first check before a
                    # response gate or another thread made this peer silent.
                    # Share the setter's lock through the final check/send so
                    # set_responding(False) also excludes in-flight replies.
                    if not self.responding:
                        self.dropped += 1
                        continue
                    for response in self.wire.respond(message):
                        # Even the device-side model must fail with a bounded
                        # wait if a virtual recipient stops consuming.
                        self.bus.send(response, timeout=0.2)
        except BaseException as error:
            self.errors.append(error)

    def set_responding(self, value):
        with self.lock:
            self.responding = value

    def command_log(self):
        with self.lock:
            return tuple(self.wire.commands)

    def hold_next_reply(self, opcode):
        with self.lock:
            self.reply_held.clear()
            self.reply_gate.clear()
            self._held_opcode = opcode

    def close(self):
        self.stop.set()
        self.reply_gate.set()
        self.worker.join(3)
        self.bus.shutdown()
        assert not self.worker.is_alive(), (
            f"Device peer did not stop: {self.worker.name}"
        )
        assert not self.errors, self.errors


class FourDevices:
    """Two complete HandDevice objects and two complete A7 objects in one process."""

    names = ("left_hand", "right_hand", "left_arm", "right_arm")

    def __init__(self, backend, channels, *, duplicate_addresses=False):
        self.backend = backend
        self.channels = dict(zip(self.names, channels))
        second = "left" if duplicate_addresses else "right"
        self.sides = dict(zip(self.names, ("left", second, "left", second)))
        self.devices = {}
        self.peers = {}
        self.started_threads = set()
        self._baseline_threads = set(threading.enumerate())
        self._closed = False

    def open(self):
        for index, name in enumerate(self.names):
            wire = (
                HandWire(self.sides[name], 10 + index * 10)
                if name.endswith("hand")
                else ArmWire(self.sides[name], (index + 1) / 100)
            )
            self.peers[name] = CANPeer(self.backend, self.channels[name], wire)
        try:
            with ThreadPoolExecutor(
                max_workers=4, thread_name_prefix="joint-open"
            ) as pool:
                futures = {
                    name: pool.submit(self._construct, name) for name in self.names
                }
                errors = []
                for name, future in futures.items():
                    try:
                        self.devices[name] = future.result()
                    except BaseException as error:
                        errors.append(error)
                if errors:
                    raise errors[0]
        finally:
            self.started_threads.update(
                set(threading.enumerate()) - self._baseline_threads
            )

    def _construct(self, name):
        if name.endswith("hand"):
            device = HandDevice(
                model="L6",
                side=self.sides[name],
                transport=CANTransport(self.channels[name], self.backend),
                config=DeviceConfig(auto_reconnect=False, identity_timeout_s=3.0),
            )
            try:
                device.open()
                device.start_polling(PollingPlan(angles_hz=20))
            except BaseException:
                device.close()
                raise
            return device
        device = A7(self.sides[name], self.channels[name], self.backend)
        try:
            device.start_polling(
                {
                    SensorType.POSITION: 0.03,
                    SensorType.VELOCITY: 0.03,
                    SensorType.TORQUE: 0.03,
                }
            )
        except BaseException:
            device.close()
            raise
        return device

    def restart(self, name):
        self.devices[name].close()
        self.peers[name].set_responding(True)
        self.devices[name] = self._construct(name)
        self.started_threads.update(set(threading.enumerate()) - self._baseline_threads)

    def command_and_observe(self, names=None, *, round_number=0):
        selected = self.names if names is None else names
        barrier = threading.Barrier(len(selected))

        def exchange(name):
            index = self.names.index(name)
            device = self.devices[name]
            barrier.wait(timeout=5)
            if name.endswith("hand"):
                target = (30 + index * 20 + round_number,) * 6
                stage = device.set_angles(target).wait(timeout=3)
                assert stage in (CommandStage.DISPATCHED, CommandStage.ACKNOWLEDGED), (
                    stage
                )
                result = device.get_angles(timeout=3)
                assert result.meta.valid
                assert result.raw == target, (name, result.raw, target)
                wait_until(
                    lambda: target in self.peers[name].command_log(),
                    description=f"{name} device write",
                )
            else:
                target = [(index + 1) / 10 + round_number / 100] * 7
                device.set_velocities(target)
                wait_until(
                    lambda: all(
                        abs(joint.velocity - value) < 1e-5
                        for joint, value in zip(device.get_feedback().joints, target)
                    ),
                    description=f"{name} measured velocity feedback",
                )
                assert device.get_feedback().connected
                observed = dict(self.peers[name].command_log())
                assert set(observed) == set(self.peers[name].wire.motor_ids)
                assert all(abs(value - target[0]) < 1e-5 for value in observed.values())

        with ThreadPoolExecutor(
            max_workers=len(selected), thread_name_prefix="joint-exchange"
        ) as pool:
            futures = [pool.submit(exchange, name) for name in selected]
            for future in futures:
                future.result(timeout=15)
        for peer in self.peers.values():
            assert not peer.errors, peer.errors
        # Recheck after every concurrent writer finished; a later peer's
        # response must not silently overwrite a cache that passed earlier.
        for name in selected:
            index = self.names.index(name)
            device = self.devices[name]
            if name.endswith("hand"):
                expected = (30 + index * 20 + round_number,) * 6
                assert device.get_snapshot().angles.raw == expected
                assert self.peers[name].command_log()[-1] == expected
            else:
                expected = (index + 1) / 10 + round_number / 100
                assert all(
                    abs(joint.velocity - expected) < 1e-5
                    for joint in device.get_feedback().joints
                )

    def close(self):
        if self._closed:
            return
        self._closed = True
        errors = []
        for device in self.devices.values():
            try:
                device.close()
            except BaseException as error:
                errors.append(error)
        for peer in self.peers.values():
            try:
                peer.close()
            except BaseException as error:
                errors.append(error)
        self.started_threads.update(set(threading.enumerate()) - self._baseline_threads)
        for worker in self.started_threads:
            worker.join(3)
        alive = [worker.name for worker in self.started_threads if worker.is_alive()]
        assert not alive, f"Joint test leaked threads: {alive}"
        assert not errors, errors
