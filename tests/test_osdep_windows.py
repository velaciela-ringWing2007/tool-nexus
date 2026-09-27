"""os_windows のテスト（PowerShell / taskkill はフェイクに差し替えるため、どのOSでも実行できる）."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tool_nexus.osdep import windows as os_windows
from tool_nexus.process.base import ProcessQueryError, Snapshot, StopError, normalize_creation_date


class FakeRunner:
    """subprocess.run の代わりに呼び出し内容を記録するフェイク."""

    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv: list[str], **kwargs) -> subprocess.CompletedProcess:
        self.calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


class TestGetProcessCreationDate:
    def test_parses_output_and_uses_list_argv(self) -> None:
        runner = FakeRunner(stdout="2026-09-27T19:30:31.3057490+09:00\r\n")
        result = os_windows.get_process_creation_date(1234, runner=runner)
        assert result == normalize_creation_date("2026-09-27T19:30:31+09:00")
        argv, kwargs = runner.calls[0]
        assert argv[0] == "powershell.exe"
        assert "ProcessId=1234" in argv[-1]
        assert kwargs.get("shell") in (None, False)

    def test_not_found_returns_none(self) -> None:
        assert os_windows.get_process_creation_date(1234, runner=FakeRunner(stdout="\r\n")) is None

    def test_failure_raises(self) -> None:
        with pytest.raises(ProcessQueryError):
            os_windows.get_process_creation_date(1234, runner=FakeRunner(returncode=1, stderr="boom"))

    def test_unparseable_output_raises(self) -> None:
        with pytest.raises(ProcessQueryError):
            os_windows.get_process_creation_date(1234, runner=FakeRunner(stdout="???"))


class TestGetCreationDates:
    def test_batches_into_one_call(self) -> None:
        runner = FakeRunner(
            stdout="10\t2026-09-27T19:30:31.3057490+09:00\r\n20\t20260927193032.000000+540\r\n"
        )
        result = os_windows.get_creation_dates([20, 10, 10, 30], runner=runner)
        assert result == {
            10: normalize_creation_date("2026-09-27T19:30:31+09:00"),
            20: normalize_creation_date("2026-09-27T19:30:32+09:00"),
        }
        assert len(runner.calls) == 1
        assert "ProcessId=10 OR ProcessId=20 OR ProcessId=30" in runner.calls[0][0][-1]

    def test_empty_does_not_call_powershell(self) -> None:
        runner = FakeRunner()
        assert os_windows.get_creation_dates([], runner=runner) == {}
        assert os_windows.get_creation_dates([0, None], runner=runner) == {}
        assert runner.calls == []

    def test_failure_raises(self) -> None:
        with pytest.raises(ProcessQueryError):
            os_windows.get_creation_dates([1], runner=FakeRunner(returncode=1))


class TestKillTree:
    def test_taskkill_tree_force(self) -> None:
        runner = FakeRunner()
        os_windows.kill_tree(4321, runner=runner)
        argv, kwargs = runner.calls[0]
        assert argv == ["taskkill", "/PID", "4321", "/T", "/F"]
        assert kwargs.get("shell") in (None, False)

    def test_failure(self) -> None:
        with pytest.raises(StopError, match="not found"):
            os_windows.kill_tree(4321, runner=FakeRunner(returncode=128, stderr="ERROR: not found"))


class TestSnapshot:
    def test_parse(self) -> None:
        text = json.dumps(
            {
                "processes": [
                    {"pid": 10, "ppid": 1, "name": "python.exe", "cmd": "python a.py",
                     "created": "2026-09-27T19:30:31.3057490+09:00"},
                    {"pid": 4, "ppid": 0, "name": "System", "cmd": None, "created": None},
                ],
                "listeners": [{"port": 8501, "pid": 10}, {"port": 8501, "pid": 10}, {"port": 135, "pid": 4}],
            }
        )
        snap = os_windows.parse_snapshot(text)
        assert snap.processes[10].created_at is not None
        assert snap.processes[4].command_line == ""
        assert snap.listeners == {8501: frozenset({10}), 135: frozenset({4})}

    def test_broken_json(self) -> None:
        with pytest.raises(ProcessQueryError):
            os_windows.parse_snapshot("{oops")

    def test_take_snapshot_uses_powershell_once(self) -> None:
        runner = FakeRunner(stdout='{"processes": [], "listeners": []}')
        assert os_windows.take_snapshot(runner=runner) == Snapshot({}, {})
        assert len(runner.calls) == 1 and "Get-NetTCPConnection" in runner.calls[0][0][-1]


class TestDialogs:
    def test_pick_file(self, tmp_path: Path) -> None:
        runner = FakeRunner(stdout=f"{tmp_path}\\app.py\r\n")
        assert os_windows.pick_file(str(tmp_path), runner=runner) == Path(f"{tmp_path}\\app.py")
        argv, kwargs = runner.calls[0]
        assert "-STA" in argv
        assert "OpenFileDialog" in argv[-1]
        assert kwargs["timeout"] >= 60

    def test_cancel(self) -> None:
        assert os_windows.pick_file(runner=FakeRunner(stdout="\r\n")) is None
        assert os_windows.pick_folder(runner=FakeRunner(stdout="")) is None

    def test_initial_dir_is_escaped(self) -> None:
        runner = FakeRunner()
        os_windows.pick_folder("C:\\it's", runner=runner)
        assert "'C:\\it''s'" in runner.calls[0][0][-1]

    def test_failure(self) -> None:
        with pytest.raises(ProcessQueryError):
            os_windows.pick_file(runner=FakeRunner(returncode=1))


def test_launch_flags_never_use_detached_process() -> None:
    # DETACHED_PROCESS を併用すると孫プロセスがコンソールウィンドウを出してしまう（実機で確認）
    flags = os_windows.LAUNCH_KWARGS["creationflags"]
    assert not flags & getattr(subprocess, "DETACHED_PROCESS", 0x8)
