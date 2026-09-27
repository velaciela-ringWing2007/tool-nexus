"""health のテスト（HTTP・PowerShell はフェイクに差し替える）."""

from __future__ import annotations

import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

from constants import HEALTH_HTTP, HEALTH_NONE, HEALTH_PROCESS, KIND_EXE, KIND_STREAMLIT
from health import (
    Status,
    ToolHealth,
    check_http,
    check_process,
    derive_status,
    health_url,
    probe,
    probe_all,
)
from models import Tool
from process_utils import ProcessQueryError

JST = timezone(timedelta(hours=9))
NOW = datetime(2026, 9, 27, 14, 0, 0, tzinfo=JST)
STARTED = "2026-09-27T13:45:01+09:00"


def make_tool(**overrides) -> Tool:
    values = {
        "id": 1,
        "name": "t",
        "directory": "C:\\dev",
        "command": "python -m streamlit run app.py",
        "kind": KIND_STREAMLIT,
        "port": 8502,
        "health_mode": HEALTH_HTTP,
    }
    return Tool(**(values | overrides))


class FakeResponse:
    def __init__(self, status: int = 200) -> None:
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


class FakeOpener:
    def __init__(self, result) -> None:
        self.result = result
        self.urls: list[str] = []
        self.timeouts: list[float] = []

    def __call__(self, url: str, timeout: float):
        self.urls.append(url)
        self.timeouts.append(timeout)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x", code, "err", {}, None)


# ----------------------------------------------------------------------
# HTTPモード
# ----------------------------------------------------------------------
class TestCheckHttp:
    def test_responds(self) -> None:
        opener = FakeOpener(FakeResponse(200))
        assert check_http(8502, kind=KIND_STREAMLIT, timeout=2.0, opener=opener) is True
        assert opener.urls == ["http://127.0.0.1:8502/_stcore/health"]
        assert opener.timeouts == [2.0]

    def test_not_listening(self) -> None:
        opener = FakeOpener(urllib.error.URLError(ConnectionRefusedError()))
        assert check_http(8502, kind=KIND_STREAMLIT, timeout=2.0, opener=opener) is False

    def test_timeout(self) -> None:
        opener = FakeOpener(TimeoutError("timed out"))
        assert check_http(8502, kind=KIND_STREAMLIT, timeout=0.1, opener=opener) is False

    def test_streamlit_error_status_is_not_alive(self) -> None:
        opener = FakeOpener(http_error(503))
        assert check_http(8502, kind=KIND_STREAMLIT, timeout=2.0, opener=opener) is False

    def test_exe_any_http_response_is_alive(self) -> None:
        opener = FakeOpener(http_error(404))
        assert check_http(9000, kind=KIND_EXE, timeout=2.0, opener=opener) is True
        assert opener.urls == ["http://127.0.0.1:9000/"]

    def test_no_port(self) -> None:
        opener = FakeOpener(FakeResponse())
        assert check_http(None, kind=KIND_STREAMLIT, timeout=2.0, opener=opener) is False
        assert opener.urls == []

    def test_health_url_is_loopback(self) -> None:
        assert health_url(8502, KIND_STREAMLIT).startswith("http://127.0.0.1:8502/")


# ----------------------------------------------------------------------
# processモード（照合ロジック自体のテストは test_process_utils の verify_pid に集約）
# ----------------------------------------------------------------------
class TestCheckProcess:
    def test_alive(self) -> None:
        assert check_process(10, STARTED, lookup=lambda pid: STARTED) is True

    def test_gone(self) -> None:
        assert check_process(10, STARTED, lookup=lambda pid: None) is False

    def test_pid_reused(self) -> None:
        assert check_process(10, STARTED, lookup=lambda pid: "2026-09-27T15:00:00+09:00") is False

    def test_query_failure_is_unknown(self) -> None:
        def lookup(pid: int):
            raise ProcessQueryError("x")

        assert check_process(10, STARTED, lookup=lookup) is None

    def test_no_record_is_not_running(self) -> None:
        assert check_process(None, None, lookup=lambda pid: STARTED) is False


class TestProbe:
    def test_none_mode_is_always_unknown(self) -> None:
        tool = make_tool(health_mode=HEALTH_NONE, last_pid=1, last_pid_created_at=STARTED)
        opener = FakeOpener(FakeResponse())
        assert probe(tool, timeout=1, lookup=lambda pid: STARTED, opener=opener) is None
        assert opener.urls == []

    def test_process_mode_uses_record(self) -> None:
        tool = make_tool(health_mode=HEALTH_PROCESS, last_pid=7, last_pid_created_at=STARTED)
        assert probe(tool, timeout=1, lookup={7: STARTED}.get) is True

    def test_probe_all(self) -> None:
        tools = [
            make_tool(id=1, port=8501),
            make_tool(id=2, health_mode=HEALTH_PROCESS, last_pid=7, last_pid_created_at=STARTED),
            make_tool(id=3, health_mode=HEALTH_PROCESS, last_pid=8, last_pid_created_at=STARTED),
            make_tool(id=4, health_mode=HEALTH_NONE),
        ]
        calls: list[list[int]] = []

        def batch(pids: list[int]) -> dict[int, str]:
            calls.append(sorted(pids))
            return {7: STARTED}

        result = probe_all(
            tools, timeout=1, opener=FakeOpener(FakeResponse()), batch_lookup=batch
        )
        assert result == {1: True, 2: True, 3: False, 4: None}
        assert calls == [[7, 8]]  # PowerShell はまとめて1回

    def test_probe_all_batch_failure_is_unknown(self) -> None:
        tools = [make_tool(id=2, health_mode=HEALTH_PROCESS, last_pid=7, last_pid_created_at=STARTED)]

        def batch(pids: list[int]) -> dict[int, str]:
            raise ProcessQueryError("powershell failed")

        assert probe_all(tools, timeout=1, batch_lookup=batch) == {2: None}

    def test_probe_all_skips_powershell_without_pids(self) -> None:
        def batch(pids: list[int]) -> dict[int, str]:  # pragma: no cover
            raise AssertionError("should not be called")

        result = probe_all(
            [make_tool(id=1)], timeout=1, opener=FakeOpener(FakeResponse()), batch_lookup=batch
        )
        assert result == {1: True}


# ----------------------------------------------------------------------
# 「起動中…」の導出
# ----------------------------------------------------------------------
def at(seconds_after_start: float) -> datetime:
    return datetime.fromisoformat(STARTED) + timedelta(seconds=seconds_after_start)


class TestDeriveStatus:
    def test_alive_is_running(self) -> None:
        assert derive_status(make_tool(), True, now=NOW) is Status.RUNNING

    def test_unknown(self) -> None:
        assert derive_status(make_tool(), None, now=NOW) is Status.UNKNOWN

    def test_no_start_record_is_stopped(self) -> None:
        assert derive_status(make_tool(), False, now=NOW) is Status.STOPPED

    @pytest.mark.parametrize("seconds", [0, 10, 30])
    def test_within_30_seconds_is_starting(self, seconds: float) -> None:
        tool = make_tool(last_started_at=STARTED)
        assert derive_status(tool, False, now=at(seconds)) is Status.STARTING

    def test_over_30_seconds_without_health_is_failed(self) -> None:
        tool = make_tool(last_started_at=STARTED)
        assert derive_status(tool, False, now=at(31)) is Status.FAILED
        assert derive_status(tool, False, now=at(600)) is Status.FAILED

    def test_failed_before_start_seen_is_still_failed(self) -> None:
        # 前回の起動で一度通っていても、今回の起動以降に通っていなければ失敗扱い
        tool = make_tool(last_started_at=STARTED, last_seen_at="2026-09-27T12:00:00+09:00")
        assert derive_status(tool, False, now=at(60)) is Status.FAILED

    def test_old_record_is_stopped(self) -> None:
        tool = make_tool(last_started_at=STARTED)
        assert derive_status(tool, False, now=at(601)) is Status.STOPPED

    def test_seen_after_start_then_down_is_stopped(self) -> None:
        tool = make_tool(last_started_at=STARTED, last_seen_at="2026-09-27T13:45:20+09:00")
        assert derive_status(tool, False, now=at(60)) is Status.STOPPED


class TestToolHealth:
    @pytest.mark.parametrize(
        ("status", "last_pid", "expected"),
        [
            (Status.RUNNING, None, True),
            (Status.STARTING, 5, True),
            (Status.FAILED, 5, False),
            (Status.STOPPED, None, False),
            (Status.UNKNOWN, 5, True),
            (Status.UNKNOWN, None, False),
        ],
    )
    def test_can_stop(self, status: Status, last_pid, expected: bool) -> None:
        health = ToolHealth(make_tool(last_pid=last_pid), None, status)
        assert health.can_stop is expected
