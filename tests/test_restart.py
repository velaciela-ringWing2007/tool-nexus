"""再起動（SPEC 6.3）の停止段階の判断のテスト."""

from __future__ import annotations

import pytest

from tool_nexus.core.models import Tool
from tool_nexus.process.base import PidStatus, ProcessNotIdentifiedError, StopError
from tool_nexus.ui.actions import RESTART_RELEASE_TIMEOUT, RestartOutcome, stop_for_restart

STARTED = "2026-09-30T10:00:00+09:00"


def tool(port: int | None = 8600, pid: int | None = 100) -> Tool:
    return Tool(id=1, name="t", directory="/x", command="c", port=port, last_pid=pid, last_pid_created_at=STARTED)


class Calls:
    def __init__(self, *, error: Exception | None = None, free: bool = True, released: bool = True) -> None:
        self.error, self.free, self.released = error, free, released
        self.stopped: list[tuple] = []
        self.waited: list[tuple] = []

    def stopper(self, pid, created_at):
        self.stopped.append((pid, created_at))
        if self.error:
            raise self.error

    def port_free(self, port):
        return self.free

    def wait_released(self, port, timeout):
        self.waited.append((port, timeout))
        return self.released


def run(t: Tool, calls: Calls):
    return stop_for_restart(t, stopper=calls.stopper, port_free=calls.port_free, wait_released=calls.wait_released)


def test_stopped_then_waits_for_port() -> None:
    calls = Calls()
    assert run(tool(), calls) == (RestartOutcome.STOPPED, "")
    assert calls.stopped == [(100, STARTED)]  # 起動時刻つきで照合して止める
    assert calls.waited == [(8600, RESTART_RELEASE_TIMEOUT)]


def test_no_port_does_not_wait() -> None:
    calls = Calls()
    assert run(tool(port=None), calls)[0] is RestartOutcome.STOPPED
    assert calls.waited == []


def test_port_not_released_does_not_start() -> None:
    assert run(tool(), Calls(released=False))[0] is RestartOutcome.PORT_BUSY


@pytest.mark.parametrize("status", [PidStatus.NOT_FOUND, PidStatus.MISMATCH])
def test_already_gone_and_port_free_starts(status: PidStatus) -> None:
    assert run(tool(), Calls(error=ProcessNotIdentifiedError(status)))[0] is RestartOutcome.NOT_RUNNING


def test_already_gone_but_port_taken_is_not_identified() -> None:
    # 記録のプロセスは無いが、誰かがポートを使っている（外で起動された）→ 止めも起動もしない
    calls = Calls(error=ProcessNotIdentifiedError(PidStatus.NOT_FOUND), free=False)
    assert run(tool(), calls)[0] is RestartOutcome.NOT_IDENTIFIED


def test_unknown_is_not_identified() -> None:
    # 起動記録が無い（TOOL NEXUS の外で起動された）
    calls = Calls(error=ProcessNotIdentifiedError(PidStatus.UNKNOWN))
    assert run(tool(pid=None), calls)[0] is RestartOutcome.NOT_IDENTIFIED


def test_stop_failure() -> None:
    assert run(tool(), Calls(error=StopError("権限がありません"))) == (RestartOutcome.STOP_FAILED, "権限がありません")
