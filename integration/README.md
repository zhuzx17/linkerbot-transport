# Hand and Arm integration tests

This suite runs two real `HandDevice` instances (L6) and two real `A7` instances
in one Python process, each on its own isolated `python-can virtual` channel.
Hand identity verification, Arm discovery and initial state reads, real Pinocchio,
protocol codecs, Transport, queues, polling, and worker threads are exercised.
Only the devices on the other end of each bus are simulated.

## Run locally

Use a clean Python 3.10–3.13 environment and sibling checkouts of the three
repositories. From `linkerbot-transport` on Linux:

```bash
python -m pip install -r requirements/ci-test.txt
python -m pip install '.[all]' ../linkerbot-hand-python-sdk '../linkerbot-arm-python-sdk[kinetix]'
python -m pip check
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p pytest_timeout --timeout=60 \
  integration/tests --junitxml=reports/integration.xml
```

On Windows, use the Arm SDK's Conda Forge installation route for real Pinocchio:

```powershell
micromamba create -y -n joint-ci -c conda-forge python=3.10 pinocchio=4.1.0 scipy pip
micromamba run -n joint-ci python -m pip install -r requirements/ci-test.txt
micromamba run -n joint-ci python -m pip install ".[all]" ../linkerbot-hand-python-sdk ../linkerbot-arm-python-sdk
micromamba run -n joint-ci python -m pip check
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"
micromamba run -n joint-ci python -m pytest -p pytest_timeout --timeout=60 integration/tests --junitxml=reports/integration.xml
```

Avoid mixing a ROS installation's `PYTHONPATH` or native library paths with an
unrelated Python environment. The integration suite is explicitly selected;
ordinary Transport `pytest` still runs only `tests/` and does not require the SDKs.
No CAN interfaces, physical devices, credentials, or privileged operations are
needed. Each case owns unique virtual channel names and closes its devices and
bus peers, then checks that its worker threads have exited.

## What is checked

- Normal left/right topology: two hands and two arms start through their normal
  constructors, then concurrently write distinct targets and read device-side
  feedback. Both the peer's received commands and the SDK's resulting state are
  checked.
- Duplicate addresses: a separate topology intentionally uses two same-side
  hands and two same-side arms on different channels. Their identical CAN IDs
  must retain different values in the production SDK caches.
- Missing replies: suppress one hand's replies to check a real read timeout, or
  one arm's replies to check that feedback becomes stale. The other three
  devices continue exchanging commands and feedback. Closing and rebuilding the
  affected instance must preserve their operation and restore all four devices.
  A device response gate holds reconstruction open until the other three finish
  another round, checking progress during the rebuild rather than only before
  and after it.
- Send backpressure: add a bounded virtual recipient to one arm's channel and
  leave its one-frame queue full. Before attaching it, stop polling and drain
  older SDK sends through checked FIFO writes, then silence that channel's
  device peer, including a deterministically held in-flight read response; the
  bounded recipient would otherwise block the simulator's replies as well.
  The real backend reports an SDK send timeout while the other three devices
  continue. Removing the blocked recipient and rebuilding the affected arm
  restores four-device traffic. This does not model the SDK's FIFO-full policy
  or physical bus-off.
- Device simulator failures propagate to pytest; teardown checks peer and SDK
  worker cleanup instead of leaving silent background failures.

Transport PRs and `main` updates run this suite and the independent Hand/Arm offline
suites on Linux and Windows with Python 3.10/3.13. Weekly runs and manual
`full_matrix=true` also cover 3.11/3.12. CI resolves all dependency refs to exact
commits once per workflow, checks out the same group in every consumer job, and
saves the source manifest alongside JUnit reports. Manual runs retain
`dependency_owner`/`dependency_ref` and accept separate `hand_ref`/`arm_ref` values.

## Shared CI entry point

The Transport, Hand, and Arm workflows use the composite action at
`.github/actions/four-devices/action.yml`. Check out the exact source revisions
before invoking it. Supply `python-version`, `hand-path`, `arm-path`, and a unique
`artifact-name`; `transport-path` defaults to `.`. All repository paths are
relative to the calling workflow's workspace. An SDK workflow checks out
Transport at `.ci/transport` and invokes
`./.ci/transport/.github/actions/four-devices` with `transport-path: .ci/transport`.
The selected Transport revision must contain this action.

Hand and Arm PRs also run this suite, using the current workflow commit (the PR
merge result) for the SDK under review. Transport and the other SDK are resolved
once to exact commits shared by every integration job. The existing
`dependency_owner`/`dependency_ref` manual inputs select those dependencies.

The action installs from those checkouts, runs pytest from the Transport
directory, and saves `reports/integration.xml` and a `transport`/`hand`/`arm` SHA
manifest in the workspace root. Reports are uploaded even if a test fails and
retained for 14 days. The action needs no secrets, write token, or physical bus.

## Limits

The wire models implement only the L6/A7 messages needed by these scenarios.
They do not model firmware or mechanics. The suite does not validate other
models, CAN FD, SocketCAN/vcan, vendor DLL/SO drivers, physical bus timing,
multi-channel USB adapter initialization, actual disconnection, or physical
movement. Missing replies are not a simulated bus-off event. Real adapter and
two-hand/two-arm acceptance still require a separate hardware test bench.

## 中文说明

此套件验证同一进程中的两只 L6 手、两条 A7 臂在四条独立虚拟 CAN 通道上同时工作。
保留真实 SDK 构造、协议、Transport、轮询、线程和 Pinocchio，只模拟总线另一端的
设备回复。测试覆盖并发命令与反馈、跨通道相同 CAN ID 隔离、单设备停止回复、
虚拟总线接收端队列背压引起的真实发送超时，以及关闭重建期间其余设备继续收发、
资源清理。它是软件联合回归，不能代替真实驱动、CAN FD、实际四路 CAN 适配器和
两手两臂的实机验收。
