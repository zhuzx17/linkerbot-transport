# Development and CI

Follow the [organization contribution guide](https://github.com/linker-bot/.github/blob/main/CONTRIBUTING.md).
From this checkout in a virtual environment:

```bash
python -m pip install -r requirements/ci-lint.txt -r requirements/ci-test.txt
python -m pip install -e '.[all]'
ruff check .
ruff format --check .
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p pytest_timeout -p pytest_cov.plugin \
  --timeout=60 --cov=linkerbot_transport --cov-report=term-missing:skip-covered \
  --cov-report=xml:reports/coverage.xml --junitxml=reports/pytest.xml
python -m build
```

The example uses Bash; in PowerShell set `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"`
and run the pytest command on one line. Test plugins are enabled explicitly to
avoid unrelated plugins from the local environment. Coverage and JUnit reports
are written to `reports/`; coverage is recorded without a minimum threshold.

GitHub Actions runs Ruff lint/format, ShellCheck, and actionlint on pull requests
and pushes to `main`. Offline tests use Python 3.10 and 3.13 on Linux and Windows;
the weekly run also tests Python 3.11 and 3.12. The package job builds the sdist
and wheel, installs the wheel, and runs `tools/check_installed.py` outside the
checkout to verify imports, typing metadata, and the license.

Every pull request and push to `main` also tests this Transport against the Hand
and Arm Python SDKs in independent jobs on Linux and Windows. A source-resolution
job selects the SDK commits once, and all consumer and integration jobs check out
the same exact revisions. Each SDK suite can finish and publish its report even
if the other fails. The default SDK source is GitHub `main`; manual runs can
select a different owner and refs.

A separate [four-device integration suite](integration/README.md) runs two Hand
and two Arm instances together in one Python process over four isolated
`python-can virtual` channels. It uses production SDKs, protocols, Transport,
and workers, with simulated devices at the bus boundary. The suite checks
concurrent commands and feedback, duplicate CAN addresses across channels,
missing device replies, virtual-recipient send backpressure, progress during
independent close/reopen, and resource cleanup.
Both SDK compatibility and integration jobs use Python 3.10 and 3.13 on Linux
and Windows; weekly runs or `full_matrix=true` add 3.11 and 3.12.

Arm and integration jobs load real Pinocchio: Linux installs the SDK's
`[kinetix]` extra, while Windows uses the documented Conda Forge environment.
Weekly and manual runs additionally check the Arm ROS2 adapter contract.
Source revision manifests, JUnit reports, and distributions are retained as
Actions artifacts for 14 days. These jobs need no Gitea access or device
credentials and do not publish releases.

Offline tests replace physical adapters and vendor libraries with controlled fakes
or use in-memory virtual buses.
Native Windows event-handle cases run on Windows and skip on Linux. Real driver,
adapter, CAN timing, and device coexistence checks require separate hardware.

## 中文说明

使用固定的 CI 工具版本运行上述检查。每次 PR 和 `main` 更新都在 Linux/Windows、
Python 3.10/3.13 上独立运行 Hand、Arm 下游回归和两手两臂四通道联合测试；
每周或手动 `full_matrix=true` 补测 3.11、3.12。联合测试保留真实 SDK、协议、
Transport 和线程，只在总线另一端模拟设备回复；每周和手动任务另检查 ROS2 adapter。
查看 Actions artifacts 中固定的源码版本、测试、覆盖率与打包结果。
离线联合测试通过不能代替 SocketCAN、厂商驱动、真实适配器和两手两臂实机共存验证。

## 在个人 fork 验证 CI

Actions 的手动运行入口支持 `dependency_owner` 和 `dependency_ref`：默认仍使用
`linker-bot` 依赖，以及原有的 main/配对提交规则。跨仓库改动可先将 Transport、
Hand/Arm Python SDK 和两个 ROS 仓库推送到个人 fork 的同名测试分支，再统一指定
个人账号与该分支，避免混用上游尚未更新的测试脚本。也可以分别设置 `hand_ref`、
`arm_ref` 为分支、标签或完整提交 SHA；未指定时沿用 `dependency_ref`，再默认 `main`。
本次 Transport 始终来自被测试的工作流提交（PR 使用合并结果），不从依赖 ref 安装。
源码解析任务先固定依赖 SHA，所有下游与联合矩阵使用同一组提交并保存版本清单。

新 fork 的工作流需要启用 Actions，且默认分支上需要包含工作流文件，才能手动运行；
可以仅将个人 fork 的默认分支设为测试分支。上游默认分支不受影响。

手动运行时设置 `full_matrix=true`，会补齐 Python 3.11、3.12；SDK、联合测试及
ROS2 adapter 任务也会运行。普通 PR 使用所选上游 `main`；若所需 SDK 修复尚未合并，
先在个人 fork 手动指定配套分支验证，再安排上游各仓库的合入顺序。
