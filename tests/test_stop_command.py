"""停止コマンド（SPEC 6.3）のテスト."""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tool_nexus.core.constants import DEFAULT_SETTINGS, HEALTH_NONE
from tool_nexus.core.models import Tool, build_tool
from tool_nexus.core.repositories import ToolRepository
from tool_nexus.process.base import PidStatus, ProcessNotIdentifiedError, StopError
from tool_nexus.process.control import run_stop_command
from tool_nexus.process.health import Status, ToolHealth
from tool_nexus.process.launch_assist import quote
from tool_nexus.ui.actions import RestartOutcome, stop_by_command, stop_for_restart, stop_tools
from tool_nexus.ui.grouping import should_start, should_stop

PYTHON = quote(sys.executable)


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


class TestRunStopCommand:
    """実際に中継プロセス越しに停止コマンドを動かす."""

    def test_output_and_exit_code_go_to_log(self, tmp_path: Path) -> None:
        write(tmp_path / "stop.py", "import sys\nprint('コンテナを止めました ✅')\nsys.exit(3)\n")
        log = tmp_path / "logs" / "t.log"
        code = run_stop_command(command=f"{PYTHON} stop.py", port=None, directory=tmp_path, log_path=log)
        assert code == 3
        lines = log.read_text(encoding="utf-8").splitlines()
        assert re.match(r"^===== .* 停止コマンド \(PID \d+\) =====$", lines[0])
        assert "stop.py" in lines[1]
        assert lines[2].endswith("| コンテナを止めました ✅")
        assert lines[-1].endswith("終了（終了コード 3） =====")

    def test_port_placeholder(self, tmp_path: Path) -> None:
        write(tmp_path / "stop.py", "import sys\nprint('port=' + sys.argv[1])\n")
        log = tmp_path / "t.log"
        assert run_stop_command(command=f"{PYTHON} stop.py {{port}}", port=8642, directory=tmp_path, log_path=log) == 0
        assert "| port=8642" in log.read_text(encoding="utf-8")

    def test_timeout_kills_the_command(self, tmp_path: Path) -> None:
        write(tmp_path / "stop.py", "import time\ntime.sleep(60)\n")
        started: list[subprocess.Popen] = []

        def popen(*args, **kwargs):
            proc = subprocess.Popen(*args, **kwargs)
            started.append(proc)
            return proc

        with pytest.raises(StopError, match="1 秒で終わらなかった"):
            run_stop_command(command=f"{PYTHON} stop.py", port=None, directory=tmp_path,
                             log_path=tmp_path / "t.log", timeout=1, popen=popen)
        deadline = time.monotonic() + 10
        while started[0].poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        assert started[0].poll() is not None  # 子ごと止まっている

    def test_cannot_run(self, tmp_path: Path) -> None:
        with pytest.raises(StopError, match="停止コマンドを実行できません"):
            run_stop_command(command="no-such-command-xyz", port=None, directory=tmp_path, log_path=tmp_path / "t.log")
        with pytest.raises(StopError):
            run_stop_command(command=f"{PYTHON} x.py", port=None, directory=tmp_path / "gone",
                             log_path=tmp_path / "t.log")


def tool(**kw) -> Tool:
    values = dict(id=1, name="t", directory="/x", command="bash up.sh", kind="script",
                  stop_command="docker compose down")
    values.update(kw)
    return Tool(**values)


class Calls:
    def __init__(self, *, command_error: Exception | None = None, stop_error: Exception | None = None) -> None:
        self.command_error, self.stop_error = command_error, stop_error
        self.log: list[str] = []

    def command_runner(self, tool, settings) -> None:
        self.log.append("command")
        if self.command_error:
            raise self.command_error

    def stopper(self, pid, created_at) -> None:
        self.log.append(f"stop {pid}")
        if self.stop_error:
            raise self.stop_error


class TestStopByCommand:
    def test_command_then_remaining_process(self) -> None:
        calls = Calls()
        stop_by_command(tool(last_pid=5, last_pid_created_at="c"), {}, command_runner=calls.command_runner,
                        stopper=calls.stopper, rediscover=lambda: None)
        assert calls.log == ["command", "stop 5"]

    def test_nothing_remaining_is_fine(self) -> None:
        calls = Calls(stop_error=ProcessNotIdentifiedError(PidStatus.NOT_FOUND))
        stop_by_command(tool(), {}, command_runner=calls.command_runner, stopper=calls.stopper,
                        rediscover=lambda: None)
        assert calls.log == ["command", "stop None"]

    def test_command_failure_does_not_touch_processes(self) -> None:
        calls = Calls(command_error=StopError("停止コマンドが終了コード 1 で終わりました。"))
        with pytest.raises(StopError):
            stop_by_command(tool(last_pid=5), {}, command_runner=calls.command_runner, stopper=calls.stopper)
        assert calls.log == ["command"]


class TestRestart:
    def test_uses_command_stopper(self) -> None:
        log = []
        outcome = stop_for_restart(tool(), stopper=lambda *a: log.append("pid"), command_stopper=lambda: log.append("cmd"),
                                   wait_released=lambda port, timeout: True)
        assert outcome == (RestartOutcome.STOPPED, "")
        assert log == ["cmd"]

    def test_command_failure_does_not_start(self) -> None:
        def fail() -> None:
            raise StopError("停止コマンドが終了コード 1 で終わりました。")

        outcome = stop_for_restart(tool(), command_stopper=fail)
        assert outcome == (RestartOutcome.STOP_FAILED, "停止コマンドが終了コード 1 で終わりました。")


class TestGroupStop:
    def test_stop_command_tools(self, tmp_path: Path) -> None:
        repo = ToolRepository(tmp_path / "t.sqlite3")
        repo.initialize()
        settings = DEFAULT_SETTINGS | {"default_log_dir": str(tmp_path / "logs")}
        tools = {}
        for name in ["ok", "失敗"]:
            created = repo.create(build_tool(name=name, directory="/x", command="bash up.sh", kind="script",
                                             health_mode=HEALTH_NONE, stop_command=f"bash stop-{name}.sh",
                                             check_directory=False))
            tools[name] = repo.get_by_id(created.id)

        def command_runner(t, s) -> None:
            if t.name == "失敗":
                raise StopError("停止コマンドが終了コード 2 で終わりました。")

        def stopper(pid, created_at) -> None:
            raise ProcessNotIdentifiedError(PidStatus.UNKNOWN)  # 起動したプロセスは残っていない

        healths = [ToolHealth(t, None, Status.UNKNOWN) for t in tools.values()]
        summary = stop_tools(repo, settings, healths, stopper=stopper, finder=lambda: [],
                             command_runner=command_runner)
        assert summary.stopped == ["ok"]
        assert summary.failed == ["失敗: 停止コマンドが終了コード 2 で終わりました。"]
        assert repo.get_by_id(tools["ok"].id).last_stopped_at
        assert repo.get_by_id(tools["失敗"].id).last_stopped_at is None  # 失敗したら停止の記録を残さない


class TestButtons:
    @pytest.mark.parametrize(
        ("status", "stop_command", "last_pid", "both", "can_stop"),
        [
            (Status.UNKNOWN, "x", None, True, True),     # 監視しない＋停止コマンド → 起動と停止の両方
            (Status.UNKNOWN, "", 5, False, True),        # 従来どおり（記録があれば停止）
            (Status.UNKNOWN, "", None, False, False),
            (Status.RUNNING, "x", 5, False, True),       # 起動中は 停止＋⟳
            (Status.STOPPED, "x", None, False, False),   # 止まっているのが分かっていれば起動だけ
        ],
    )
    def test_offers(self, status, stop_command, last_pid, both, can_stop) -> None:
        health = ToolHealth(tool(stop_command=stop_command, last_pid=last_pid), None, status)
        assert health.offers_start_and_stop is both
        assert health.can_stop is can_stop
        assert should_stop(health) is can_stop

    def test_group_start_still_skips_unknown_with_record(self) -> None:
        assert not should_start(ToolHealth(tool(last_pid=5), None, Status.UNKNOWN))
        assert should_start(ToolHealth(tool(), None, Status.UNKNOWN))
