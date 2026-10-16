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

Weekly and manual runs install this Transport with the latest Hand and Arm
Python SDKs from GitHub `main`, run both offline suites, and check the Arm ROS2
adapter contract. The Arm dependency requires Pinocchio on Linux. Source commits
are recorded in logs. Reports and distributions are retained as Actions
artifacts for 14 days. These jobs need no Gitea access or device credentials
and do not publish releases.

Tests replace physical adapters and vendor libraries with controlled fakes.
Native Windows event-handle cases run on Windows and skip on Linux. Real driver,
adapter, CAN timing, and device coexistence checks require separate hardware.

## 中文说明

使用固定的 CI 工具版本运行上述检查。普通 CI 覆盖 Linux/Windows 和 Python
3.10、3.13；每周补测 3.11、3.12，并验证最新 Hand、Arm SDK 的共存和 ROS2
adapter 契约。查看 Actions artifacts 中的测试、覆盖率与打包结果。
离线测试通过不能代替厂商驱动、真实适配器和设备共存验证。

## 在个人 fork 验证 CI

Actions 的手动运行入口支持 `dependency_owner` 和 `dependency_ref`：默认仍使用
`linker-bot` 依赖，以及原有的 main/配对提交规则。跨仓库改动可先将 Transport、
Hand/Arm Python SDK 和两个 ROS 仓库推送到个人 fork 的同名测试分支，再统一指定
个人账号与该分支，避免混用上游尚未更新的测试脚本。日志记录实际使用的源码提交。

新 fork 的工作流需要启用 Actions，且默认分支上需要包含工作流文件，才能手动运行；
可以仅将个人 fork 的默认分支设为测试分支。上游默认分支不受影响。

手动运行时设置 `full_matrix=true`，会补齐 Python 3.11、3.12；ROS/SDK 消费者任务也会运行。
