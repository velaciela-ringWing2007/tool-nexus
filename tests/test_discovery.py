"""起動中ツールの検出・ポートからの引き直し（tool_nexus.process.control の探索部分）のテスト.

プロセス一覧は実機（LIST NEXUS を起動中）で観測した形を元にしたフェイクを使う。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tool_nexus.process.control import (
    ProcessInfo,
    Snapshot,
    detect_streamlit,
    find_port_owner,
    guess_directory,
    root_process,
)

WINDOWS_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="Windows のパス表記を使うテスト")

VENV_PY = r"E:\dev\list-nexus\.venv\Scripts\python.exe"
OWN_PID = 9000  # TOOL NEXUS 自身


def proc(pid: int, ppid: int, name: str, cmd: str, created: str = "2026-09-27T13:45:17+09:00"):
    return ProcessInfo(pid=pid, ppid=ppid, name=name, command_line=cmd, created_at=created)


def snapshot(processes: list[ProcessInfo], listeners: dict[int, set[int]]) -> Snapshot:
    return Snapshot({p.pid: p for p in processes}, {k: frozenset(v) for k, v in listeners.items()})


def base_processes() -> list[ProcessInfo]:
    return [
        proc(1, 0, "explorer.exe", "explorer.exe"),
        # TOOL NEXUS 自身（venv リダイレクタ → 本体）
        proc(8999, 1, "python.exe", r"E:\tn\.venv\Scripts\python.exe -m streamlit run app.py"),
        proc(OWN_PID, 8999, "python.exe", r"E:\tn\.venv\Scripts\python.exe -m streamlit run app.py"),
        # bash から起動された LIST NEXUS（実機と同じ形: 親と子のコマンドラインが同一）
        proc(27208, 1, "bash.exe", 'bash.exe -c "... streamlit run app.py"'),
        proc(42288, 27208, "python.exe", f"{VENV_PY} -m streamlit run app.py"),
        proc(12380, 42288, "python.exe", f"{VENV_PY} -m streamlit run app.py"),
    ]


class TestRootProcess:
    def test_venv_redirector_parent_is_root(self) -> None:
        snap = snapshot(base_processes(), {})
        assert root_process(snap, 12380).pid == 42288  # bash には登らない

    def test_uv_run_parent(self) -> None:
        snap = snapshot(
            [
                proc(50, 1, "uv.exe", "uv run python -m streamlit run app.py"),
                proc(51, 50, "python.exe", r"C:\p\.venv\Scripts\python.exe -m streamlit run app.py"),
            ],
            {},
        )
        assert root_process(snap, 51).pid == 50

    def test_entry_point_exe_parent(self) -> None:
        snap = snapshot(
            [
                proc(60, 1, "streamlit.exe", r"C:\p\.venv\Scripts\streamlit.exe run app.py"),
                proc(61, 60, "python.exe", r'C:\Python\python.exe "C:\p\.venv\Scripts\streamlit.exe" run app.py'),
            ],
            {},
        )
        assert root_process(snap, 61).pid == 60

    def test_unrelated_python_parent_is_not_climbed(self) -> None:
        snap = snapshot(
            [
                proc(70, 1, "python.exe", "python supervisor.py"),
                proc(71, 70, "python.exe", "python -m streamlit run app.py"),
            ],
            {},
        )
        assert root_process(snap, 71).pid == 71

    def test_parent_started_later_is_reused_pid(self) -> None:
        snap = snapshot(
            [
                proc(80, 1, "python.exe", "python -m streamlit run app.py", "2026-09-27T15:00:00+09:00"),
                proc(81, 80, "python.exe", "python -m streamlit run app.py", "2026-09-27T13:00:00+09:00"),
            ],
            {},
        )
        assert root_process(snap, 81).pid == 81

    def test_never_climbs_into_protected(self) -> None:
        snap = snapshot(base_processes(), {})
        assert root_process(snap, OWN_PID, stop_at={OWN_PID, 8999}).pid == OWN_PID

    def test_cycle_does_not_loop(self) -> None:
        snap = snapshot(
            [
                proc(90, 91, "python.exe", "python -m x"),
                proc(91, 90, "python.exe", "python -m x"),
            ],
            {},
        )
        assert root_process(snap, 90).pid in {90, 91}

    def test_missing(self) -> None:
        assert root_process(snapshot([], {}), 1) is None


class TestFindPortOwner:
    def test_owner_is_root(self) -> None:
        snap = snapshot(base_processes(), {8501: {12380}})
        assert find_port_owner(snap, 8501, own_pid=OWN_PID).pid == 42288

    def test_tool_nexus_itself_is_never_returned(self) -> None:
        snap = snapshot(base_processes(), {8499: {OWN_PID}})
        assert find_port_owner(snap, 8499, own_pid=OWN_PID) is None

    def test_nobody_listening(self) -> None:
        assert find_port_owner(snapshot(base_processes(), {}), 8501, own_pid=OWN_PID) is None


class TestDetectStreamlit:
    @WINDOWS_ONLY
    def test_detects_unregistered(self) -> None:
        snap = snapshot(base_processes(), {8499: {OWN_PID}, 8501: {12380}, 135: {1}})
        found = detect_streamlit(snap, exclude_ports={8499}, own_pid=OWN_PID)
        assert [(d.port, d.listener_pid, d.process.pid) for d in found] == [(8501, 12380, 42288)]
        assert found[0].name == "list-nexus"
        assert found[0].directory == r"E:\dev\list-nexus"
        assert found[0].process.command_line == f"{VENV_PY} -m streamlit run app.py"

    def test_excludes_registered_ports(self) -> None:
        snap = snapshot(base_processes(), {8501: {12380}})
        assert detect_streamlit(snap, exclude_ports={8501}, own_pid=OWN_PID) == []

    def test_excludes_tool_nexus_even_on_other_port(self) -> None:
        snap = snapshot(base_processes(), {8600: {OWN_PID}})
        assert detect_streamlit(snap, exclude_ports=set(), own_pid=OWN_PID) == []

    def test_ignores_non_streamlit_listeners(self) -> None:
        snap = snapshot(
            [proc(5, 1, "python.exe", "python -m http.server 8000")], {8000: {5}}
        )
        assert detect_streamlit(snap, exclude_ports=set(), own_pid=OWN_PID) == []


class TestGuessDirectory:
    @WINDOWS_ONLY
    def test_venv_root(self) -> None:
        assert guess_directory(f"{VENV_PY} -m streamlit run app.py") == r"E:\dev\list-nexus"

    @WINDOWS_ONLY
    def test_absolute_script(self) -> None:
        assert guess_directory(r'python -m streamlit run "C:\my tools\x\app.py"') == r"C:\my tools\x"

    def test_unknown(self) -> None:
        assert guess_directory("streamlit run app.py") == ""

    def test_venv_with_pyvenv_cfg_and_other_name(self, tmp_path: Path) -> None:
        venv = tmp_path / "proj" / "myenv"
        (venv / "Scripts").mkdir(parents=True)
        (venv / "pyvenv.cfg").write_text("", encoding="utf-8")
        exe = venv / "Scripts" / "python.exe"
        assert guess_directory(f'"{exe}" -m streamlit run app.py') == str(tmp_path / "proj")


class TestLinuxLayout:
    """Linux のプロセス（venv の python はシンボリックリンクで親子にならない）."""

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX のパス表記を使うテスト")
    def test_detects_linux_streamlit(self) -> None:
        venv_py = "/home/me/dev/list-nexus/.venv/bin/python"
        snap = snapshot(
            [
                proc(1, 0, "systemd", "/sbin/init"),
                proc(500, 1, "bash", "bash"),
                proc(600, 500, "python", f"{venv_py} -m streamlit run app.py"),
            ],
            {8501: {600}},
        )
        found = detect_streamlit(snap, exclude_ports=set(), own_pid=OWN_PID)
        assert [(d.port, d.process.pid) for d in found] == [(8501, 600)]
        assert found[0].directory == "/home/me/dev/list-nexus"
        assert found[0].name == "list-nexus"

    def test_uv_run_with_versioned_python(self) -> None:
        snap = snapshot(
            [
                proc(70, 1, "uv", "uv run python -m streamlit run app.py"),
                proc(71, 70, "python3.12", "/p/.venv/bin/python3.12 -m streamlit run app.py"),
            ],
            {8600: {71}},
        )
        assert find_port_owner(snap, 8600, own_pid=OWN_PID).pid == 70
