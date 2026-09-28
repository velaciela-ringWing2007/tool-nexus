"""tool_nexus.ui.log_filters のテスト.

asyncio 自身の exception handler を通して、実際と同じ形のログを出して確かめる。
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from tool_nexus.ui import log_filters

MESSAGE = "Exception in callback _ProactorBasePipeTransport._call_connection_lost(None)"


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture()
def captured():
    logger = logging.getLogger("asyncio")
    saved_filters = list(logger.filters)
    handler = Capture()
    logger.addHandler(handler)
    log_filters.install()
    yield handler.records
    logger.removeHandler(handler)
    logger.filters[:] = saved_filters


def report(message: str, exc: BaseException) -> None:
    """asyncio がコールバック中の例外を報告するのと同じ経路でログを出す。"""
    loop = asyncio.new_event_loop()
    try:
        try:
            raise exc
        except BaseException as raised:  # noqa: BLE001 - 報告用に捕まえる
            loop.call_exception_handler({"message": message, "exception": raised})
    finally:
        loop.close()


def test_browser_close_reset_is_dropped(captured) -> None:
    report(MESSAGE, ConnectionResetError(10054, "既存の接続はリモート ホストに強制的に切断されました。"))
    report(MESSAGE, ConnectionAbortedError(10053, "aborted"))
    assert captured == []


@pytest.mark.parametrize(
    ("message", "exc"),
    [
        (MESSAGE, ValueError("本物のエラー")),                       # 例外の種類が違う
        ("Exception in callback something_else()", ConnectionResetError()),  # 場所が違う
    ],
)
def test_other_errors_are_kept(captured, message: str, exc: BaseException) -> None:
    report(message, exc)
    assert len(captured) == 1


def test_install_is_idempotent(captured) -> None:
    log_filters.install()
    log_filters.install()
    names = [type(f).__name__ for f in logging.getLogger("asyncio").filters]
    assert names.count("ClientDisconnectFilter") == 1
