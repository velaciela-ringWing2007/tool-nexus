"""TOOL NEXUS が起動したプロセスの探し直し（SPEC 6.3・6.4）のテスト."""

from __future__ import annotations

import shlex
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from tool_nexus.core.constants import DEFAULT_SETTINGS, HEALTH_HTTP, HEALTH_PROCESS
from tool_nexus.core.models import Tool, build_tool
from tool_nexus.core.repositories import ToolRepository
from tool_nexus.process.base import PidStatus, ProcessInfo, ProcessNotIdentifiedError, ProcessQueryError
from tool_nexus.process.control import find_relayed, parse_relay_command, relay_argv
from tool_nexus.process.health import Status, ToolHealth, expects_running
from tool_nexus.ui.actions import (
    RestartOutcome,
    expected_launch,
    find_launched,
    rediscover_alive,
    stop_for_restart,
    stop_tools,
    stop_with_rediscovery,
)

CREATED = "2026-10-01T13:00:00+09:00"
STARTED = "2026-10-01T13:00:00.100000+09:00"


def command_line(argv: list[str]) -> str:
    """OS がプロセス一覧に出すのと同じ形のコマンドライン。"""
    return subprocess.list2cmdline(argv) if sys.platform == "win32" else shlex.join(argv)


def relay(pid: int, argv: list[str], log: Path, *, ppid: int = 1, created: str | None = CREATED) -> ProcessInfo:
    return ProcessInfo(pid=pid, ppid=ppid, name="python", command_line=command_line(relay_argv(argv, log)),
                       created_at=created)


TOOL_ARGV = [sys.executable, "app.py", "--name", "a b"]


class TestParse:
    def test_relay_command_line(self, tmp_path: Path) -> None:
        log = tmp_path / "my logs" / "t.log"
        assert parse_relay_command(command_line(relay_argv(TOOL_ARGV, log))) == (str(log), TOOL_ARGV)

    @pytest.mark.parametrize("line", ["python app.py", "python -m streamlit run app.py -- --log x", ""])
    def test_not_relay(self, line: str) -> None:
        assert parse_relay_command(line) is None

    def test_broken_relay_arguments(self) -> None:
        assert parse_relay_command("python log_relay.py --log") is None


class TestFindRelayed:
    def test_log_and_argv_must_both_match(self, tmp_path: Path) -> None:
        log = tmp_path / "t.log"
        processes = [relay(10, TOOL_ARGV, log)]
        assert find_relayed(processes, log_path=log, argv=TOOL_ARGV).pid == 10
        assert find_relayed(processes, log_path=tmp_path / "other.log", argv=TOOL_ARGV) is None
        assert find_relayed(processes, log_path=log, argv=[*TOOL_ARGV, "--x"]) is None
        assert find_relayed(processes, log_path=log, argv=TOOL_ARGV[:-1]) is None

    def test_parent_and_child_relay_keep_the_top(self, tmp_path: Path) -> None:
        # venv のリダイレクタ越しに中継プロセスが動くと、同じコマンドラインの親子になる
        log = tmp_path / "t.log"
        processes = [relay(10, TOOL_ARGV, log), relay(11, TOOL_ARGV, log, ppid=10)]
        assert find_relayed(processes, log_path=log, argv=TOOL_ARGV).pid == 10

    def test_two_independent_matches_are_ambiguous(self, tmp_path: Path) -> None:
        log = tmp_path / "t.log"
        processes = [relay(10, TOOL_ARGV, log), relay(20, TOOL_ARGV, log)]
        assert find_relayed(processes, log_path=log, argv=TOOL_ARGV) is None

    def test_without_creation_date_is_ignored(self, tmp_path: Path) -> None:
        log = tmp_path / "t.log"
        assert find_relayed([relay(10, TOOL_ARGV, log, created=None)], log_path=log, argv=TOOL_ARGV) is None

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows は大文字小文字を区別しない")
    def test_windows_ignores_case(self, tmp_path: Path) -> None:
        log = tmp_path / "t.log"
        processes = [relay(10, [a.upper() for a in TOOL_ARGV], Path(str(log).upper()))]
        assert find_relayed(processes, log_path=log, argv=TOOL_ARGV).pid == 10


# ----------------------------------------------------------------------
# 画面から呼ぶ操作
# ----------------------------------------------------------------------
@pytest.fixture()
def repo(tmp_path: Path) -> ToolRepository:
    repository = ToolRepository(tmp_path / "t.sqlite3")
    repository.initialize()
    return repository


@pytest.fixture()
def settings(tmp_path: Path) -> dict[str, str]:
    return DEFAULT_SETTINGS | {"default_log_dir": str(tmp_path / "logs")}


def make_tool(repo: ToolRepository, tmp_path: Path, name: str = "t", *, created_at: str | None = None) -> Tool:
    tool = repo.create(build_tool(name=name, directory=str(tmp_path), kind="python", health_mode=HEALTH_PROCESS,
                                  command=subprocess.list2cmdline(TOOL_ARGV) if sys.platform == "win32"
                                  else shlex.join(TOOL_ARGV)))
    repo.record_start(tool.id, pid=999, created_at=created_at)  # 起動時刻を取れなかった起動
    return repo.get_by_id(tool.id)


def relay_for(tool: Tool, settings: dict[str, str], pid: int = 50) -> ProcessInfo:
    log, argv = expected_launch(tool, settings)
    return relay(pid, argv, log)


class Finder:
    def __init__(self, processes: list[ProcessInfo] | None = None, error: Exception | None = None) -> None:
        self.processes, self.error, self.calls = processes or [], error, 0

    def __call__(self) -> list[ProcessInfo]:
        self.calls += 1
        if self.error:
            raise self.error
        return self.processes


class TestExpectedLaunch:
    def test_same_as_launch(self, repo, settings, tmp_path) -> None:
        tool = make_tool(repo, tmp_path)
        log, argv = expected_launch(tool, settings)
        assert log == tmp_path / "logs" / f"tool-{tool.id}.log"
        assert argv == TOOL_ARGV

    def test_cannot_build(self, repo, settings, tmp_path) -> None:
        tool = make_tool(repo, tmp_path)
        assert expected_launch(replace(tool, directory=str(tmp_path / "gone")), settings) is None
        assert expected_launch(replace(tool, command='python "unclosed'), settings) is None
        assert expected_launch(replace(tool, kind="link"), settings) is None


class TestFindLaunched:
    def test_found(self, repo, settings, tmp_path) -> None:
        tool = make_tool(repo, tmp_path)
        finder = Finder([relay_for(tool, settings)])
        assert find_launched([tool], settings, finder=finder)[tool.id].pid == 50
        assert finder.calls == 1

    def test_query_failure_is_not_found(self, repo, settings, tmp_path) -> None:
        tool = make_tool(repo, tmp_path)
        assert find_launched([tool], settings, finder=Finder(error=ProcessQueryError("x"))) == {}

    def test_nothing_to_search_does_not_query(self, settings) -> None:
        finder = Finder()
        assert find_launched([], settings, finder=finder) == {}
        assert finder.calls == 0


class TestStopWithRediscovery:
    def test_record_works(self) -> None:
        stopped = []
        tool = Tool(id=1, name="t", directory="/x", command="c", last_pid=5, last_pid_created_at=CREATED)
        stop_with_rediscovery(tool, stopper=lambda pid, c: stopped.append(pid), rediscover=lambda: pytest.fail())
        assert stopped == [5]

    def test_rediscovered(self) -> None:
        stopped = []

        def stopper(pid, created_at):
            if pid is None:
                raise ProcessNotIdentifiedError(PidStatus.UNKNOWN)
            stopped.append((pid, created_at))

        found = ProcessInfo(pid=50, ppid=1, name="python", command_line="", created_at=CREATED)
        stop_with_rediscovery(Tool(id=1, name="t", directory="/x", command="c"), stopper=stopper,
                              rediscover=lambda: found)
        assert stopped == [(50, CREATED)]  # 探し直したものも起動時刻つきで照合して止める

    def test_not_found_raises_original(self) -> None:
        def stopper(pid, created_at):
            raise ProcessNotIdentifiedError(PidStatus.NOT_FOUND)

        with pytest.raises(ProcessNotIdentifiedError) as info:
            stop_with_rediscovery(Tool(id=1, name="t", directory="/x", command="c"), stopper=stopper)
        assert info.value.status is PidStatus.NOT_FOUND


class TestRediscoverAlive:
    def test_adopts_and_marks_alive(self, repo, settings, tmp_path) -> None:
        tool = make_tool(repo, tmp_path)
        alive = {tool.id: False}
        searched: set = set()
        adopted = rediscover_alive(repo, settings, [tool], alive, searched=searched,
                                   finder=Finder([relay_for(tool, settings)]))
        assert alive[tool.id] is True
        assert adopted[tool.id].last_pid == 50
        saved = repo.get_by_id(tool.id)
        assert (saved.last_pid, saved.last_pid_created_at) == (50, CREATED)
        assert saved.last_started_at == tool.last_started_at  # 起動の記録は変えない

    def test_same_start_is_searched_once(self, repo, settings, tmp_path) -> None:
        tool = make_tool(repo, tmp_path)
        finder, searched = Finder(), set()
        for _ in range(3):
            rediscover_alive(repo, settings, [tool], {tool.id: False}, searched=searched, finder=finder)
        assert finder.calls == 1
        searched.clear()  # 再チェック・起動・停止の直後
        rediscover_alive(repo, settings, [tool], {tool.id: False}, searched=searched, finder=finder)
        assert finder.calls == 2

    @pytest.mark.parametrize("case", ["alive", "unknown", "http", "stopped", "never"])
    def test_not_candidates(self, repo, settings, tmp_path, case) -> None:
        tool = make_tool(repo, tmp_path)
        if case == "http":
            tool = replace(tool, health_mode=HEALTH_HTTP, port=8600)
        if case == "stopped":
            repo.record_stop(tool.id)
            tool = repo.get_by_id(tool.id)
        if case == "never":
            tool = replace(tool, last_started_at=None)
        alive = {tool.id: {"alive": True, "unknown": None}.get(case, False)}
        finder = Finder([relay_for(tool, settings)])
        assert rediscover_alive(repo, settings, [tool], alive, finder=finder) == {}
        assert finder.calls == 0


class TestExpectsRunning:
    def test_cases(self) -> None:
        base = Tool(id=1, name="t", directory="/x", command="c")
        assert not expects_running(base)
        assert expects_running(replace(base, last_started_at=STARTED))
        assert not expects_running(replace(base, last_started_at=STARTED, last_stopped_at=STARTED))
        assert expects_running(replace(base, last_started_at=STARTED,
                                       last_stopped_at="2026-10-01T13:00:00.050000+09:00"))


class TestStopPaths:
    def test_restart_uses_rediscovery(self) -> None:
        stopped = []

        def stopper(pid, created_at):
            if pid is None:
                raise ProcessNotIdentifiedError(PidStatus.UNKNOWN)
            stopped.append(pid)

        found = ProcessInfo(pid=50, ppid=1, name="python", command_line="", created_at=CREATED)
        outcome = stop_for_restart(Tool(id=1, name="t", directory="/x", command="c"), stopper=stopper,
                                   rediscover=lambda: found, wait_released=lambda port, timeout: True)
        assert outcome == (RestartOutcome.STOPPED, "")
        assert stopped == [50]

    def test_group_stop_uses_rediscovery(self, repo, settings, tmp_path) -> None:
        tool = make_tool(repo, tmp_path)
        stopped = []

        def stopper(pid, created_at):
            if pid != 50:
                raise ProcessNotIdentifiedError(PidStatus.NOT_FOUND)
            stopped.append(pid)

        summary = stop_tools(repo, settings, [ToolHealth(tool, True, Status.RUNNING)], stopper=stopper,
                             finder=Finder([relay_for(tool, settings)]))
        assert summary.stopped == ["t"] and summary.not_identified == []
        assert stopped == [50]
        assert repo.get_by_id(tool.id).last_stopped_at


class TestRealProcess:
    """実際に中継プロセス越しにツールを起動し、PID の記録なしで見つけ直して止める."""

    def test_find_and_stop(self, repo, settings, tmp_path) -> None:
        from tool_nexus.process.control import launch, stop

        (tmp_path / "app.py").write_text("import time\nwhile True:\n    time.sleep(0.2)\n", encoding="utf-8")
        tool = make_tool(repo, tmp_path)
        log, _ = expected_launch(tool, settings)
        result = launch(command=tool.command, kind=tool.kind, port=None, directory=tool.directory, log_path=log)
        try:
            found = find_launched([tool], settings).get(tool.id)
            assert found is not None and found.pid == result.pid
            stop_with_rediscovery(tool, rediscover=lambda: found)  # 記録（PID なし）では特定できない → 探し直して止める
            assert find_launched([tool], settings) == {}
        finally:
            if result.created_at:
                try:
                    stop(result.pid, result.created_at)
                except ProcessNotIdentifiedError:
                    pass
