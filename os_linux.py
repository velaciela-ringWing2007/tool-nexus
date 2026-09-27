"""Linux 用のOS依存機能（SPEC 3.2）.

psutil は使わず、プロセス情報は /proc から読む。
このモジュールはどのOSでも import できる。/proc の場所（proc_root）を差し替えられるので、
テストでは一時ディレクトリに作った疑似 /proc を読ませて Windows 上でも検証する。
"""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from process_types import (
    ProcessInfo,
    ProcessQueryError,
    Runner,
    Snapshot,
    StopError,
    normalize_creation_date,
)

NAME = "linux"

PROC = Path("/proc")
DIALOG_TIMEOUT: float = 600.0
# 停止時、SIGTERM のあと SIGKILL に切り替えるまで待つ秒数
STOP_GRACE: float = 5.0

# 新しいセッション（プロセスグループ）で起動し、TOOL NEXUS の端末やシグナルから切り離す
LAUNCH_KWARGS: dict = {"start_new_session": True}

POSIX_SPLIT = True
DEFAULT_PYTHONS: tuple[str, ...] = ("python3", "python")
EXECUTABLE_LABEL = ".py / 実行ファイル"

_SIGKILL: int = getattr(signal, "SIGKILL", 9)
_LISTEN = "0A"  # /proc/net/tcp の st 列で LISTEN を表す値


def is_executable_file(path: Path) -> bool:
    """exe 種別として登録できるファイルか（実行権限があり、.py 以外）。"""
    return path.is_file() and path.suffix.lower() != ".py" and os.access(path, os.X_OK)


# ----------------------------------------------------------------------
# 起動した子プロセスの回収
# ----------------------------------------------------------------------
# Linux では、終了した子プロセスは親が wait するまでゾンビとして /proc に残り、
# 起動時刻も変わらないため「生きている」と誤判定される。起動した Popen を覚えておき、
# 状態を確認するたびに poll() して回収する（Streamlit のサーバープロセス内で共有される）。
_children: dict[int, subprocess.Popen] = {}


def track_child(proc: subprocess.Popen) -> None:
    _children[proc.pid] = proc


def reap_children() -> None:
    for pid, proc in list(_children.items()):
        try:
            finished = proc.poll() is not None
        except OSError:
            finished = True
        if finished:
            _children.pop(pid, None)


# ----------------------------------------------------------------------
# /proc の読み取り
# ----------------------------------------------------------------------
def _clk_tck() -> int:
    try:
        return int(os.sysconf("SC_CLK_TCK"))
    except (AttributeError, ValueError, OSError):
        return 100


def _boot_time(proc_root: Path) -> float:
    try:
        for line in (proc_root / "stat").read_text(encoding="ascii", errors="replace").splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except OSError as exc:
        raise ProcessQueryError("起動時刻の基準（/proc/stat）を読めませんでした。") from exc
    raise ProcessQueryError("/proc/stat に btime がありません。")


def parse_stat(text: str) -> tuple[str, str, int, int]:
    """/proc/<pid>/stat を (comm, state, ppid, starttime) に分解する。

    comm は括弧で囲まれ、空白や括弧を含みうるので、最後の ')' で区切る。
    starttime は22番目の項目（起動からのクロック数）。
    """
    left, right = text.index("("), text.rindex(")")
    comm = text[left + 1 : right]
    fields = text[right + 2 :].split()
    # fields[0] が3番目の項目（state）
    return comm, fields[0], int(fields[1]), int(fields[19])


def _created_at(starttime: int, boot_time: float, clk_tck: int) -> str | None:
    return normalize_creation_date(datetime.fromtimestamp(boot_time + starttime / clk_tck).astimezone())


def _read_stat(pid: int, proc_root: Path) -> tuple[str, str, int, int] | None:
    """stat を読む。プロセスが無い・ゾンビなら None。"""
    try:
        text = (proc_root / str(int(pid)) / "stat").read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, ProcessLookupError, NotADirectoryError):
        return None
    except OSError as exc:
        raise ProcessQueryError(f"/proc/{pid}/stat を読めませんでした。") from exc
    try:
        parsed = parse_stat(text)
    except (ValueError, IndexError) as exc:
        raise ProcessQueryError(f"/proc/{pid}/stat を解釈できませんでした。") from exc
    # ゾンビ（Z）・終了済み（X）は存在しないものとして扱う
    return None if parsed[1] in ("Z", "X") else parsed


def get_process_creation_date(
    pid: int, *, proc_root: Path = PROC, clk_tck: int | None = None
) -> str | None:
    """PIDの起動時刻を正規化して返す。プロセスが存在しなければ None。"""
    if proc_root == PROC:
        reap_children()
    stat = _read_stat(pid, proc_root)
    if stat is None:
        return None
    return _created_at(stat[3], _boot_time(proc_root), clk_tck or _clk_tck())


def get_creation_dates(
    pids: Iterable[int], *, proc_root: Path = PROC, clk_tck: int | None = None
) -> dict[int, str]:
    """複数PIDの起動時刻を返す。存在しないPIDは結果に含まれない。"""
    result: dict[int, str] = {}
    for pid in sorted({int(p) for p in pids if p}):
        created = get_process_creation_date(pid, proc_root=proc_root, clk_tck=clk_tck)
        if created:
            result[pid] = created
    return result


def _read_cmdline(pid: int, proc_root: Path) -> list[str]:
    try:
        raw = (proc_root / str(pid) / "cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part]


def _pid_dirs(proc_root: Path) -> list[int]:
    try:
        return sorted(int(entry.name) for entry in proc_root.iterdir() if entry.name.isdigit())
    except OSError as exc:
        raise ProcessQueryError("/proc を読めませんでした。") from exc


def read_processes(proc_root: Path = PROC, *, clk_tck: int | None = None) -> dict[int, ProcessInfo]:
    """全プロセスを読む。読めないもの（権限・競合で消えた）は飛ばす。"""
    boot_time = _boot_time(proc_root)
    ticks = clk_tck or _clk_tck()
    processes: dict[int, ProcessInfo] = {}
    for pid in _pid_dirs(proc_root):
        try:
            stat = _read_stat(pid, proc_root)
        except ProcessQueryError:
            continue
        if stat is None:
            continue
        comm, _state, ppid, starttime = stat
        argv = _read_cmdline(pid, proc_root)
        processes[pid] = ProcessInfo(
            pid=pid,
            ppid=ppid,
            # comm は15文字で切れるため、分かれば実行ファイル名を使う
            name=os.path.basename(argv[0]) if argv else comm,
            command_line=shlex.join(argv),
            created_at=_created_at(starttime, boot_time, ticks),
        )
    return processes


def parse_net_tcp(text: str) -> dict[int, set[int]]:
    """/proc/net/tcp(6) から LISTEN 中の {ポート: {inode}} を返す。"""
    result: dict[int, set[int]] = {}
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 10 or fields[3] != _LISTEN:
            continue
        try:
            port = int(fields[1].rsplit(":", 1)[1], 16)
            inode = int(fields[9])
        except (ValueError, IndexError):
            continue
        if inode:
            result.setdefault(port, set()).add(inode)
    return result


def read_listeners(proc_root: Path = PROC) -> dict[int, frozenset[int]]:
    """LISTEN 中の {ポート: {PID}}。ソケットの inode と /proc/<pid>/fd を突き合わせる。

    他ユーザーのプロセスの fd は読めないため、自分のプロセスだけが対象になる（単一ユーザー前提）。
    """
    inodes_by_port: dict[int, set[int]] = {}
    for name in ("tcp", "tcp6"):
        try:
            text = (proc_root / "net" / name).read_text(encoding="ascii", errors="replace")
        except OSError:
            continue
        for port, inodes in parse_net_tcp(text).items():
            inodes_by_port.setdefault(port, set()).update(inodes)
    if not inodes_by_port:
        return {}

    wanted = {inode for inodes in inodes_by_port.values() for inode in inodes}
    pid_by_inode: dict[int, int] = {}
    for pid in _pid_dirs(proc_root):
        fd_dir = proc_root / str(pid) / "fd"
        try:
            entries = list(fd_dir.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                target = os.readlink(entry)
            except OSError:
                continue
            if target.startswith("socket:[") and target.endswith("]"):
                inode = int(target[8:-1])
                if inode in wanted:
                    pid_by_inode.setdefault(inode, pid)

    listeners: dict[int, frozenset[int]] = {}
    for port, inodes in inodes_by_port.items():
        pids = frozenset(pid_by_inode[i] for i in inodes if i in pid_by_inode)
        if pids:
            listeners[port] = pids
    return listeners


def take_snapshot(*, proc_root: Path = PROC, clk_tck: int | None = None) -> Snapshot:
    """現在のプロセス一覧とLISTEN中のポートを取得する。"""
    if proc_root == PROC:
        reap_children()
    return Snapshot(read_processes(proc_root, clk_tck=clk_tck), read_listeners(proc_root))


# ----------------------------------------------------------------------
# 停止
# ----------------------------------------------------------------------
def descendants(processes: dict[int, ProcessInfo], pid: int) -> list[int]:
    """pid の子孫（子・孫…）のPID。"""
    children: dict[int, list[int]] = {}
    for info in processes.values():
        children.setdefault(info.ppid, []).append(info.pid)
    found: list[int] = []
    stack = [pid]
    while stack:
        for child in children.get(stack.pop(), []):
            if child not in found and child != pid:
                found.append(child)
                stack.append(child)
    return found


def kill_tree(
    pid: int,
    *,
    grace: float = STOP_GRACE,
    proc_root: Path = PROC,
    kill: Callable[[int, int], None] = os.kill,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """プロセスを子孫ごと停止する。照合は呼び出し側で済ませておくこと。

    Windows の taskkill /F と違い、まず SIGTERM で終了処理の機会を与え、
    grace 秒たっても残っているものだけ SIGKILL する。
    子孫は停止前に列挙しておく（親が先に消えると子の親子関係が付け替わるため）。
    """
    try:
        processes = read_processes(proc_root)
    except ProcessQueryError as exc:
        raise StopError("停止対象の子プロセスを調べられませんでした。") from exc
    targets = [int(pid), *descendants(processes, int(pid))]

    def send(target: int, sig: int) -> None:
        try:
            kill(target, sig)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            raise StopError(f"PID {target} を停止する権限がありません。") from exc

    for target in targets:
        send(target, signal.SIGTERM)

    deadline = time.monotonic() + grace
    remaining = targets
    while True:
        if proc_root == PROC:
            reap_children()
        remaining = [t for t in remaining if _is_alive(t, proc_root)]
        if not remaining or time.monotonic() >= deadline:
            break
        sleep(0.2)
    for target in remaining:
        send(target, _SIGKILL)
    if proc_root == PROC:
        reap_children()


def _is_alive(pid: int, proc_root: Path) -> bool:
    try:
        return _read_stat(pid, proc_root) is not None
    except ProcessQueryError:
        return True


# ----------------------------------------------------------------------
# ファイル選択（zenity / kdialog）
# ----------------------------------------------------------------------
def _dialog_argv(
    kind: str, initial_dir: str | None, which: Callable[[str], str | None]
) -> list[str]:
    start = initial_dir or str(Path.home())
    if which("zenity"):
        argv = ["zenity", "--file-selection"]
        if kind == "folder":
            argv += ["--directory", "--title=作業ディレクトリを選択"]
        else:
            argv += ["--title=起動するファイルを選択"]
        return argv + [f"--filename={start.rstrip('/')}/"]
    if which("kdialog"):
        flag = "--getexistingdirectory" if kind == "folder" else "--getopenfilename"
        return ["kdialog", flag, start]
    raise ProcessQueryError(
        "ファイル選択には zenity または kdialog が必要です（例: sudo apt install zenity）。"
    )


def _run_dialog(argv: list[str], runner: Runner) -> Path | None:
    try:
        result = runner(argv, capture_output=True, text=True, errors="replace", timeout=DIALOG_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProcessQueryError("ファイル選択のダイアログを開けませんでした。") from exc
    output = (result.stdout or "").strip()
    if result.returncode == 0 and output:
        return Path(output)
    if result.returncode == 1:  # キャンセル（zenity / kdialog 共通）
        return None
    raise ProcessQueryError(
        f"ファイル選択のダイアログを開けませんでした: {(result.stderr or '').strip()}"
    )


def pick_file(
    initial_dir: str | None = None,
    *,
    runner: Runner = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
) -> Path | None:
    return _run_dialog(_dialog_argv("file", initial_dir, which), runner)


def pick_folder(
    initial_dir: str | None = None,
    *,
    runner: Runner = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
) -> Path | None:
    return _run_dialog(_dialog_argv("folder", initial_dir, which), runner)
