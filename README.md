# linkerbot-transport

`linkerbot-transport` is the physical communication layer shared by Linkerbot
device SDKs. It provides classic CAN, CAN FD, exact receive routing, observable
send results, shared connection leases, and bounded transmit scheduling.

[简体中文](README_zh.md)

The package is independent of ROS, hand models, arm models, and kinematics.
This README and the [guides](docs/README.md) describe the current `main` source.

## Install the current source

Use Python 3.10 or newer. The examples assume the checkout is under `~/src`
and use an environment named `py-linkerbot`. Editable installation uses the
checkout's code.

First clone the repository; skip this step if you already have a local checkout:

```bash
mkdir -p ~/src
cd ~/src
git clone https://github.com/linker-bot/linkerbot-transport.git
```

### uv

```bash
cd ~/src
uv venv py-linkerbot --python 3.12
source py-linkerbot/bin/activate
uv pip install -e "./linkerbot-transport[all]"
```

### pip

```bash
cd ~/src
python3.12 -m venv py-linkerbot
source py-linkerbot/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e "./linkerbot-transport[all]"
```

On Windows PowerShell, activate with `./py-linkerbot/Scripts/Activate.ps1`.
Replace `[all]` with `[can]` for classic python-can backends or `[canfd]` for
python-can CAN FD and serial support; omit extras for the base package.
Vendor drivers and libraries are installed separately; see the backend guides below.

Verify that imports resolve to the checkout:

```bash
python -c "import linkerbot_transport; print(linkerbot_transport.__file__)"
```

## Select a backend

| Bus | Entry point | Typical channel | Platform |
| --- | --- | --- | --- |
| Classic CAN | `CANTransport` | `can0`, `PCAN_USBBUS1` | Linux SocketCAN, Windows PCAN |
| Vendor CAN FD | `CANFDTransport` | MiniCANFD device/channel | Windows and Linux |
| python-can CAN FD | `PythonCANFDTransport` | `COM5`, `/dev/ttyACM0` | SLCAN FD and python-can backends |
| Linux SocketCAN FD | `SocketCANFDTransport` | `can0` | Linux |

Backend configuration, adapter prerequisites, frame conversion and validation
are documented in [Backend configuration](docs/python-can.md),
[MiniCANFD](docs/minicanfd.md), and [Adapter enumeration](docs/adapter-enumeration.md).

## Minimal lifecycle

Construction is side-effect free. `open()` acquires the backend and starts the
transport; always close it in `finally`:

```python
from linkerbot_transport.can import CANTransport

transport = CANTransport("can0")
try:
    transport.open()
    health = transport.get_health()
    print(health.state, health.generation)
finally:
    transport.close()
```

PCAN and SLCAN use the same lifecycle with their backend-specific channel and
options. MiniCANFD setup and channel enumeration use the APIs in the linked
backend guides.

## Public API at a glance

Import transports from `linkerbot_transport.can` and
`linkerbot_transport.canfd`; shared contracts, errors, routes, receipts,
`SharedTransportPool`, and `TransmitArbiter` are exported from
`linkerbot_transport`. A send receipt confirms backend acceptance; it does not
confirm device execution. Receive routes are exact, bounded, and tied to one
connection generation.

Read [Transport runtime](docs/runtime.md) for route subscriptions, health
metrics, receipts, latest-wins/FIFO arbitration, batch admission, and caller
draining. The [documentation index](docs/README.md) lists all guides.

## Contributing and license

See [Development and CI](DEVELOPMENT.md) for local checks and the test matrix.

English and Chinese contributions are welcome. Follow the
[Linkerbot contribution guide](https://github.com/linker-bot/.github/blob/main/CONTRIBUTING.md).
Report vulnerabilities privately using the
[organization security policy](https://github.com/linker-bot/.github/blob/main/SECURITY.md).

Licensed under the [MIT License](LICENSE).
