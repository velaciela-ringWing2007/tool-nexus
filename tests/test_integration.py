"""実プロセスを使う結合テスト（OSを問わず実行する）.

OS依存部分（起動フラグ・起動時刻の取得・LISTEN中のポート・子ごとの停止）を、
フェイクではなく実際のプロセスで確かめる（SPEC 3.2 の検証）。
遅いので `pytest -m "not integration"` で外せる。
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from typing import Callable

import pytest

import platform_ops
import process_utils as pu
from constants import KIND_STREAMLIT, KIND_WEB
from health import check_http
from launch_assist import quote
from port_utils import is_port_free, pick_free_port

pytestmark = pytest.mark.integration


def wait_until(predicate: Callable[[], bool], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.3)
    return predicate()


def cleanup(result: pu.LaunchResult) -> None:
    """テストが途中で失敗しても、起動したプロセスを残さない。"""
    if pu.verify_pid(result.pid, result.created_at) is pu.PidStatus.MATCH:
        platform_ops.kill_tree(result.pid)


def test_web_tool_full_cycle(tmp_path: Path) -> None:
    port = pick_free_port(set(), 20000, 20999)
    result = pu.launch(
        command=f"{quote(sys.executable)} -m http.server {{port}} --bind 127.0.0.1",
        kind=KIND_WEB,
        port=port,
        directory=tmp_path,
        log_path=tmp_path / "tool.log",
    )
    try:
        assert result.created_at, "起動時刻を取得できること"
        assert wait_until(lambda: check_http(port, kind=KIND_WEB, timeout=1.0), 20), (
            (tmp_path / "tool.log").read_text(errors="replace")
        )
        assert pu.verify_pid(result.pid, result.created_at) is pu.PidStatus.MATCH

        # ポートを持つのが子（Windows の venv リダイレクタ）でも、起動したPIDまで登れること
        owner = pu.find_port_owner(pu.take_snapshot(), port)
        assert owner is not None and owner.pid == result.pid

        # 起動時刻が違えば止めない
        with pytest.raises(pu.ProcessNotIdentifiedError):
            pu.stop(result.pid, "2000-01-01T00:00:00+09:00")
        assert check_http(port, kind=KIND_WEB, timeout=1.0)

        pu.stop(result.pid, result.created_at)
        assert wait_until(lambda: is_port_free(port), 15), "子プロセスまで止まりポートが解放されること"
        assert wait_until(
            lambda: pu.verify_pid(result.pid, result.created_at) is pu.PidStatus.NOT_FOUND, 10
        )
    finally:
        cleanup(result)


@pytest.mark.skipif(importlib.util.find_spec("streamlit") is None, reason="streamlit が無い")
def test_streamlit_is_detected(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("import streamlit as st\nst.write('hi')\n", encoding="utf-8")
    port = pick_free_port(set(), 21000, 21999)
    result = pu.launch(
        command=f"{quote(sys.executable)} -m streamlit run app.py",
        kind=KIND_STREAMLIT,
        port=port,
        directory=tmp_path,
        log_path=tmp_path / "tool.log",
    )
    try:
        assert wait_until(lambda: check_http(port, kind=KIND_STREAMLIT, timeout=1.0), 60), (
            (tmp_path / "tool.log").read_text(errors="replace")
        )
        found = pu.detect_streamlit(pu.take_snapshot(), exclude_ports=set())
        mine = [d for d in found if d.port == port]
        assert mine and mine[0].process.pid == result.pid
        assert "streamlit" in mine[0].process.command_line

        pu.stop(result.pid, result.created_at)
        assert wait_until(lambda: is_port_free(port), 15)
    finally:
        cleanup(result)
