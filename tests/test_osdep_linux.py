"""os_linux のテスト.

一時ディレクトリに疑似 /proc を作って読ませるため、Windows 上でも実行できる。
シグナル送信はフェイクに差し替える。実プロセスでの確認は test_linux_integration にある。
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from tool_nexus.osdep import linux as os_linux
from tool_nexus.process.base import ProcessQueryError, StopError, normalize_creation_date

BOOT = 1_790_000_000  # /proc/stat の btime（エポック秒）
CLK = 100


def stat_line(pid: int, comm: str, state: str, ppid: int, starttime: int) -> str:
    # 3番目以降: state ppid pgrp session tty_nr tpgid flags minflt cminflt majflt cmajflt
    #            utime stime cutime cstime priority nice num_threads itrealvalue starttime ...
    rest = [state, str(ppid)] + ["0"] * 17 + [str(starttime), "0", "0"]
    return f"{pid} ({comm}) " + " ".join(rest) + "\n"


class FakeProc:
    """疑似 /proc を組み立てる."""

    def __init__(self, root: Path) -> None:
        self.root = root
        (root / "net").mkdir(parents=True)
        (root / "stat").write_text(f"cpu  1 2 3\nbtime {BOOT}\nprocesses 10\n", encoding="ascii")
        (root / "net" / "tcp").write_text(
            "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n",
            encoding="ascii",
        )
        (root / "net" / "tcp6").write_text((root / "net" / "tcp").read_text(), encoding="ascii")

    def add(self, pid: int, ppid: int, argv: list[str], *, starttime: int = 500,
            comm: str | None = None, state: str = "S") -> None:
        folder = self.root / str(pid)
        (folder / "fd").mkdir(parents=True)
        name = comm or (os.path.basename(argv[0]) if argv else "kthread")[:15]
        (folder / "stat").write_text(stat_line(pid, name, state, ppid, starttime), encoding="utf-8")
        (folder / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + (b"\0" if argv else b""))

    def listen(self, pid: int, port: int, inode: int, *, v6: bool = False) -> None:
        name = "tcp6" if v6 else "tcp"
        local = ("00000000000000000000000000000000" if v6 else "0100007F") + f":{port:04X}"
        line = (
            f"   0: {local} 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 {inode} "
            "1 0000000000000000 100 0 0 10 0\n"
        )
        path = self.root / "net" / name
        path.write_text(path.read_text() + line, encoding="ascii")
        # /proc/<pid>/fd/N -> socket:[inode] を再現する（シンボリックリンクが作れない環境では readlink を差し替える）
        (self.root / str(pid) / "fd" / "3").write_text(f"socket:[{inode}]", encoding="ascii")


@pytest.fixture()
def proc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeProc:
    fake = FakeProc(tmp_path / "proc")
    # 疑似 fd はシンボリックリンクではなく中身に宛先を書いたファイルにしている
    real_readlink = os.readlink

    def readlink(path):
        p = Path(path)
        if str(p).startswith(str(fake.root)):
            return p.read_text(encoding="ascii")
        return real_readlink(path)

    monkeypatch.setattr(os_linux.os, "readlink", readlink)
    return fake


def created(starttime: int) -> str:
    return normalize_creation_date(datetime.fromtimestamp(BOOT + starttime / CLK).astimezone())


class TestParseStat:
    def test_comm_with_spaces_and_parens(self) -> None:
        text = stat_line(42, "my (weird) name", "S", 7, 12345)
        assert os_linux.parse_stat(text) == ("my (weird) name", "S", 7, 12345)


class TestCreationDate:
    def test_from_starttime_and_btime(self, proc: FakeProc) -> None:
        proc.add(10, 1, ["python3", "app.py"], starttime=12345)
        got = os_linux.get_process_creation_date(10, proc_root=proc.root, clk_tck=CLK)
        assert got == created(12345)

    def test_missing(self, proc: FakeProc) -> None:
        assert os_linux.get_process_creation_date(99, proc_root=proc.root, clk_tck=CLK) is None

    def test_zombie_is_treated_as_gone(self, proc: FakeProc) -> None:
        # 親が wait するまでゾンビは /proc に残り、起動時刻も同じ。生きている扱いにしない
        proc.add(11, 1, [], state="Z")
        assert os_linux.get_process_creation_date(11, proc_root=proc.root, clk_tck=CLK) is None

    def test_batch(self, proc: FakeProc) -> None:
        proc.add(10, 1, ["a"], starttime=100)
        proc.add(20, 1, ["b"], starttime=200)
        got = os_linux.get_creation_dates([20, 10, 30, 0], proc_root=proc.root, clk_tck=CLK)
        assert got == {10: created(100), 20: created(200)}

    def test_missing_btime(self, tmp_path: Path) -> None:
        fake = FakeProc(tmp_path / "p")
        (fake.root / "stat").write_text("cpu 1\n", encoding="ascii")
        fake.add(10, 1, ["a"])
        with pytest.raises(ProcessQueryError):
            os_linux.get_process_creation_date(10, proc_root=fake.root, clk_tck=CLK)


class TestSnapshot:
    def test_processes_and_listeners(self, proc: FakeProc) -> None:
        proc.add(1, 0, ["/sbin/init"])
        proc.add(600, 1, ["/home/me/p/.venv/bin/python", "-m", "streamlit", "run", "my app.py"])
        proc.add(700, 1, ["/usr/bin/python3", "-m", "http.server"])
        proc.add(2, 0, [], comm="kthreadd")
        proc.listen(600, 8501, inode=1111)
        proc.listen(600, 8501, inode=2222, v6=True)
        proc.listen(700, 8000, inode=3333)

        snap = os_linux.take_snapshot(proc_root=proc.root, clk_tck=CLK)
        info = snap.processes[600]
        assert info.name == "python"
        assert info.ppid == 1
        assert info.command_line == "/home/me/p/.venv/bin/python -m streamlit run 'my app.py'"
        assert info.created_at == created(500)
        assert snap.processes[2].name == "kthreadd" and snap.processes[2].command_line == ""
        assert snap.listeners == {8501: frozenset({600}), 8000: frozenset({700})}

    def test_non_listen_sockets_are_ignored(self) -> None:
        text = (
            "  sl  local_address rem_address   st ...\n"
            "   0: 0100007F:1F90 0100007F:D431 01 00000000:00000000 00:00000000 00000000  1000 0 4444 1\n"
            "   1: 0100007F:2135 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000 0 5555 1\n"
        )
        assert os_linux.parse_net_tcp(text) == {0x2135: {5555}}


class TestKillTree:
    def test_term_then_kill_remaining(self, proc: FakeProc) -> None:
        proc.add(100, 1, ["python", "-m", "streamlit"])
        proc.add(101, 100, ["python", "child"])
        proc.add(102, 101, ["python", "grandchild"])
        proc.add(200, 1, ["other"])
        sent: list[tuple[int, int]] = []

        def kill(pid: int, sig: int) -> None:
            sent.append((pid, sig))
            if sig == 15 and pid != 102:  # 102 だけ SIGTERM を無視する
                (proc.root / str(pid) / "stat").unlink()

        os_linux.kill_tree(100, grace=0.0, proc_root=proc.root, kill=kill, sleep=lambda s: None)
        assert sorted(pid for pid, sig in sent if sig == 15) == [100, 101, 102]
        assert [(pid, sig) for pid, sig in sent if sig != 15] == [(102, os_linux._SIGKILL)]
        assert all(pid != 200 for pid, _ in sent)

    def test_already_gone_is_fine(self, proc: FakeProc) -> None:
        proc.add(100, 1, ["x"])

        def kill(pid: int, sig: int) -> None:
            raise ProcessLookupError

        os_linux.kill_tree(100, grace=0.0, proc_root=proc.root, kill=kill, sleep=lambda s: None)

    def test_permission_error(self, proc: FakeProc) -> None:
        proc.add(100, 1, ["x"])

        def kill(pid: int, sig: int) -> None:
            raise PermissionError

        with pytest.raises(StopError, match="権限"):
            os_linux.kill_tree(100, grace=0.0, proc_root=proc.root, kill=kill, sleep=lambda s: None)


class FakeRunner:
    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = "") -> None:
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


class TestDialogs:
    def test_zenity(self) -> None:
        runner = FakeRunner(stdout="/home/me/app.py\n")
        got = os_linux.pick_file("/home/me", runner=runner, which=lambda n: n == "zenity" or None)
        assert got == Path("/home/me/app.py")
        assert runner.calls[0][:2] == ["zenity", "--file-selection"]
        assert "--filename=/home/me/" in runner.calls[0]

    def test_zenity_folder(self) -> None:
        runner = FakeRunner(stdout="/home/me/proj\n")
        os_linux.pick_folder(None, runner=runner, which=lambda n: n == "zenity" or None)
        assert "--directory" in runner.calls[0]

    def test_kdialog_fallback(self) -> None:
        runner = FakeRunner(stdout="/tmp/x\n")
        os_linux.pick_folder("/tmp", runner=runner, which=lambda n: n == "kdialog" or None)
        assert runner.calls[0] == ["kdialog", "--getexistingdirectory", "/tmp"]

    def test_cancel(self) -> None:
        assert os_linux.pick_file(runner=FakeRunner(returncode=1), which=lambda n: "zenity") is None

    def test_no_dialog_tool(self) -> None:
        with pytest.raises(ProcessQueryError, match="zenity"):
            os_linux.pick_file(runner=FakeRunner(), which=lambda n: None)

    def test_display_error(self) -> None:
        with pytest.raises(ProcessQueryError, match="cannot open display"):
            os_linux.pick_file(
                runner=FakeRunner(returncode=255, stderr="cannot open display"), which=lambda n: "zenity"
            )


@pytest.mark.skipif(sys.platform == "win32", reason="実行権限は POSIX のみ")
def test_is_executable_file(tmp_path: Path) -> None:
    tool = tmp_path / "tool"
    tool.write_text("#!/bin/sh\n", encoding="ascii")
    assert not os_linux.is_executable_file(tool)
    os.chmod(tool, 0o755)
    assert os_linux.is_executable_file(tool)
    script = tmp_path / "a.py"
    script.write_text("", encoding="ascii")
    os.chmod(script, 0o755)
    assert not os_linux.is_executable_file(script)


def test_launch_uses_new_session() -> None:
    assert os_linux.LAUNCH_KWARGS == {"start_new_session": True}
