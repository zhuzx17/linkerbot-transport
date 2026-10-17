"""Explicitly selected, offline SDK integration tests on isolated CAN channels."""

from __future__ import annotations

import uuid

import pytest

from .devices import FourDevices


@pytest.fixture
def four_devices(request, monkeypatch):
    # Select the production shared Transport path explicitly, regardless of a
    # developer's shell setting for the legacy Arm backend.
    monkeypatch.setenv("LINKERBOT_ARM_TRANSPORT", "cantransport")
    prefix = "joint-" + uuid.uuid4().hex
    channels = tuple(f"{prefix}-{index}" for index in range(4))
    rig = FourDevices(
        "virtual", channels, duplicate_addresses=getattr(request, "param", False)
    )
    try:
        rig.open()
        yield rig
    finally:
        rig.close()
