"""プロセスの起動・停止・照合・探索（OSに依存しない部分）.

OSに依存する処理（プロセス情報の取得、子ごとの停止、起動フラグ、ファイル選択）は
osdep 経由で osdep.windows / osdep.linux に委ねる（SPEC 3.2）。
subprocess は常にリスト形式・shell=False で実行する。

注意: Popen.pid は親プロセスであり、ポートを持つのは子プロセスである（Windows の venv）。
そのため停止は子を含めて行う。また、PIDはOSに再利用されるため、停止・死活判定の前に必ず
起動時刻を照合する（verify_pid）。
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from tool_nexus import osdep
from tool_nexus.core.constants import DEFAULT_LOG_FILENAME, KIND_STREAMLIT, PORT_PLACEHOLDER
from tool_nexus.process.base import (
    CreationDateLookup,
    LaunchError,
    PidStatus,
    ProcessInfo,
    ProcessNotIdentifiedError,
    ProcessQueryError,
    Snapshot,
    StopError,
    normalize_creation_date,
)

# process.base の例外・型も、呼び出し側はこのモジュールから import できる
__all__ = [
    "CreationDateLookup", "DetectedTool", "LaunchError", "LaunchResult", "PidStatus",
    "ProcessInfo", "ProcessNotIdentifiedError", "ProcessQueryError", "Snapshot", "StopError",
    "ancestors", "build_argv", "detect_streamlit", "find_port_owner", "get_creation_dates",
    "get_process_creation_date", "guess_directory", "is_launcher_name", "launch",
    "normalize_creation_date", "prepare_launch", "read_log_tail", "resolve_executable",
    "resolve_log_path", "root_process", "split_command", "stop", "take_snapshot", "verify_pid",
]

# OS別の実装（呼び出し側はこの名前で使う）
get_process_creation_date = osdep.get_process_creation_date
get_creation_dates = osdep.get_creation_dates
take_snapshot = osdep.take_snapshot


# ----------------------------------------------------------------------
# コマンドの組み立て
# ----------------------------------------------------------------------
def split_command(command: str, *, posix: bool | None = None) -> list[str]:
    """コマンド文字列をargvへ分解する。閉じていないクォートがあると ValueError を送出する。

    Windows は posix=False を使う（posix=True は C:\\dev\\x のバックスラッシュを消すため）。
    posix=False はトークン両端のクォートを残すので、それを外す。
    Linux は通常の posix=True で分解する。
    """
    if posix is None:
        posix = osdep.POSIX_SPLIT
    if posix:
        return shlex.split(command, posix=True)
    tokens = shlex.split(command, posix=False)
    return [t[1:-1] if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'" else t for t in tokens]


def build_argv(command: str, *, kind: str, port: int | str | None) -> list[str]:
    """登録内容から起動用のargvを組み立てる（実行ファイルの解決は行わない）。

    コマンド中の {port} は全種別で登録済みのポートに置き換える。
    streamlit のときだけ --server.* を付与し、コマンドに既に書かれていれば二重付与しない。
    port は画面のプレビュー用に文字列（「保存時に割当」など）も受け付ける。
    """
    try:
        argv = split_command(command)
    except ValueError as exc:  # 閉じていないクォートなど
        raise LaunchError(f"起動コマンドを解釈できません: {exc}") from exc
    if not argv:
        raise LaunchError("起動コマンドが空です。")
    if any(PORT_PLACEHOLDER in arg for arg in argv):
        if not port:
            raise LaunchError(f"起動コマンドに {PORT_PLACEHOLDER} がありますが、ポートが登録されていません。")
        argv = [arg.replace(PORT_PLACEHOLDER, str(port)) for arg in argv]
    if kind != KIND_STREAMLIT:
        return argv

    if port and not _has_option(argv, "--server.port"):
        argv += ["--server.port", str(port)]
    if not _has_option(argv, "--server.address"):
        argv += ["--server.address", "127.0.0.1"]
    if not _has_option(argv, "--server.headless"):
        argv += ["--server.headless", "true"]
    return argv


def _has_option(argv: list[str], name: str) -> bool:
    """`--name value` と `--name=value` のどちらの書き方も検出する。"""
    return any(arg == name or arg.startswith(name + "=") for arg in argv)


def resolve_executable(executable: str, directory: Path) -> Path:
    """argv[0] を絶対パスへ解決する。見つからなければ LaunchError。

    Windows の CreateProcess は相対パスの実行ファイルを cwd 引数ではなく
    呼び出し元のカレントディレクトリ基準で探すため、作業ディレクトリ基準で解決しておく。

    シンボリックリンクはたどらない（Path.resolve() は使わない）。Linux の venv の python は
    システムの python へのリンクで、たどると venv の外の python になり、venv のパッケージが
    見えなくなる（WSL の Ubuntu で確認）。
    """
    candidate = Path(executable)
    has_dir_part = candidate.is_absolute() or len(candidate.parts) > 1
    if has_dir_part:
        path = candidate if candidate.is_absolute() else directory / candidate
        if path.is_file():
            return Path(os.path.abspath(path))
        raise LaunchError(f"実行ファイルが見つかりません: {path}")

    found = shutil.which(executable, path=None)
    if found is None:
        local = directory / executable
        if local.is_file():
            return Path(os.path.abspath(local))
        raise LaunchError(f"実行ファイルが見つかりません: {executable}")
    return Path(found)


def resolve_log_path(
    *, directory: Path, log_path: str, default_log_dir: str, tool_id: int | None
) -> Path:
    """ログ出力先を決める。

    1. ツールに指定があればそれ（相対パスは作業ディレクトリ基準）
    2. 設定の既定ログディレクトリがあればその配下の tool-<id>.log
    3. どちらも無ければ作業ディレクトリ配下の tool-nexus.log
    """
    if log_path.strip():
        path = Path(log_path.strip())
        return path if path.is_absolute() else directory / path
    if default_log_dir.strip():
        suffix = tool_id if tool_id is not None else "new"
        return Path(default_log_dir.strip()) / f"tool-{suffix}.log"
    return directory / DEFAULT_LOG_FILENAME


def read_log_tail(path: Path, *, max_lines: int, max_bytes: int) -> str | None:
    """ログファイルの末尾を返す。ファイルが無ければ None。

    大きなログでも読み込み量を抑えるため、末尾 max_bytes だけを読む。
    子プロセスの出力はロケール依存（cp932）のこともあるため、UTF-8で読めなければ cp932 で読む。
    """
    try:
        with open(path, "rb") as file:
            file.seek(0, 2)
            size = file.tell()
            file.seek(max(0, size - max_bytes))
            data = file.read()
    except FileNotFoundError:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("cp932", errors="replace")
    lines = text.splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]  # 途中から読んだ先頭行は欠けているので捨てる
    return "\n".join(lines[-max_lines:])


# ----------------------------------------------------------------------
# 照合
# ----------------------------------------------------------------------
def verify_pid(
    pid: int | None,
    created_at: str | None,
    *,
    lookup: CreationDateLookup = get_process_creation_date,
) -> PidStatus:
    """記録済みの (PID, 起動時刻) が、今も同じプロセスを指しているか照合する。

    停止前の安全確認と process モードの死活監視の両方で使う。
    """
    if not pid or not created_at:
        return PidStatus.UNKNOWN
    expected = normalize_creation_date(created_at)
    if expected is None:
        return PidStatus.UNKNOWN
    try:
        actual = lookup(pid)
    except ProcessQueryError:
        return PidStatus.UNKNOWN
    if actual is None:
        return PidStatus.NOT_FOUND
    if normalize_creation_date(actual) != expected:
        return PidStatus.MISMATCH
    return PidStatus.MATCH


# ----------------------------------------------------------------------
# 起動・停止
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class LaunchResult:
    """起動結果。created_at が取れなかった場合は None。"""

    pid: int
    created_at: str | None
    argv: tuple[str, ...]
    log_path: Path


def prepare_launch(
    *, command: str, kind: str, port: int | None, directory: str | Path
) -> tuple[list[str], Path]:
    """起動前の確認を行い、(実行ファイルを絶対パスにしたargv, 作業ディレクトリ) を返す。"""
    workdir = Path(directory)
    if not workdir.is_dir():
        raise LaunchError(f"作業ディレクトリが見つかりません: {workdir}")
    argv = build_argv(command, kind=kind, port=port)
    argv[0] = str(resolve_executable(argv[0], workdir))
    return argv, workdir


def launch(
    *,
    command: str,
    kind: str,
    port: int | None,
    directory: str | Path,
    log_path: Path,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    lookup: CreationDateLookup = get_process_creation_date,
) -> LaunchResult:
    """ツールをデタッチ起動する。

    標準出力はログファイルへ流す（PIPE にすると誰も読まずに詰まり、子プロセスが止まる）。
    """
    argv, workdir = prepare_launch(command=command, kind=kind, port=port, directory=directory)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(log_path, "ab")
    except OSError as exc:
        raise LaunchError(f"ログファイルを開けませんでした: {log_path}") from exc

    try:
        with log_file:
            proc = popen(
                argv,
                cwd=workdir,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                **osdep.LAUNCH_KWARGS,
            )
    except OSError as exc:
        raise LaunchError(f"起動に失敗しました: {exc}") from exc
    if isinstance(proc, subprocess.Popen):
        osdep.track_child(proc)  # Linux: 終了後にゾンビとして残らないよう回収する

    try:
        created_at = lookup(proc.pid)
    except ProcessQueryError:
        created_at = None
    return LaunchResult(pid=proc.pid, created_at=created_at, argv=tuple(argv), log_path=log_path)


def stop(
    pid: int | None,
    created_at: str | None,
    *,
    lookup: CreationDateLookup = get_process_creation_date,
    kill: Callable[[int], None] = osdep.kill_tree,
) -> None:
    """記録済みのプロセスを子ごと停止する。

    起動時刻が一致しない限り停止しない（PID再利用による誤爆を防ぐ）。
    特定できない場合は ProcessNotIdentifiedError を送出する。
    停止の方法はOSによる（Windows: taskkill /T /F、Linux: SIGTERM → SIGKILL）。
    """
    status = verify_pid(pid, created_at, lookup=lookup)
    if status is not PidStatus.MATCH:
        raise ProcessNotIdentifiedError(status)
    kill(int(pid))


# ----------------------------------------------------------------------
# 探索（起動中ツールの検出・ポートからの引き直し）
# ----------------------------------------------------------------------
# 親をたどるときに「同じツールの起動役」とみなしてよい実行ファイル。
# venv の python.exe はリダイレクタで子が本体、streamlit.exe などは pip のエントリポイント、
# uv は uv run の親になる。シェルやエクスプローラはここに含めない。
_LAUNCHER_BASES: frozenset[str] = frozenset(
    {"python", "pythonw", "py", "uv", "streamlit", "flask", "uvicorn"}
)
_VERSIONED_PYTHON = re.compile(r"python\d+(\.\d+)?")


def is_launcher_name(name: str) -> bool:
    """起動役とみなせる実行ファイル名か（python.exe / python3.12 / uv など）。"""
    base = name.lower()
    if base.endswith(".exe"):
        base = base[:-4]
    return base in _LAUNCHER_BASES or bool(_VERSIONED_PYTHON.fullmatch(base))


@dataclass(frozen=True, slots=True)
class DetectedTool:
    """検出された未登録のツール（登録フォームの初期値に使う）."""

    port: int
    process: ProcessInfo  # 停止・登録の対象（最上位の起動役）
    listener_pid: int     # 実際にポートを持っているPID
    directory: str
    name: str


def _args(command_line: str) -> list[str]:
    """コマンドラインから実行ファイルを除いた引数を返す。解釈できなければ空。"""
    try:
        return split_command(command_line)[1:]
    except ValueError:
        return []


def _same_tool(parent: ProcessInfo, child: ProcessInfo) -> bool:
    """親が子を起動しただけの「起動役」かどうか。

    どちらかの引数がもう一方の末尾と一致すれば同じツールとみなす。
    * venv のリダイレクタ: 親と子の引数が同一（実機で確認）
    * uv run: 親 `uv run python -m streamlit run app.py` の末尾が子の引数
    * pip のエントリポイント（streamlit.exe）: 子の引数の末尾が親の引数
    """
    if not is_launcher_name(parent.name):
        return False
    parent_args, child_args = _args(parent.command_line), _args(child.command_line)
    if not parent_args or not child_args:
        return False
    shorter, longer = sorted((parent_args, child_args), key=len)
    return longer[len(longer) - len(shorter):] == shorter


def ancestors(snapshot: Snapshot, pid: int) -> set[int]:
    """pid 自身とその祖先のPID（循環に備えて一度見たPIDで止める）。"""
    seen: set[int] = {pid}
    current = snapshot.processes.get(pid)
    while current is not None:
        parent = snapshot.processes.get(current.ppid)
        if parent is None or parent.pid in seen:
            break
        seen.add(parent.pid)
        current = parent
    return seen


def root_process(
    snapshot: Snapshot, pid: int, *, stop_at: set[int] | frozenset[int] = frozenset()
) -> ProcessInfo | None:
    """ポートを持つプロセスから親をたどり、同じツールの最上位の起動役を返す。

    stop_at（TOOL NEXUS 自身とその祖先）には決して登らない。
    """
    current = snapshot.processes.get(pid)
    if current is None:
        return None
    seen = {current.pid}
    while True:
        parent = snapshot.processes.get(current.ppid)
        if (
            parent is None
            or parent.pid in seen
            or parent.pid in stop_at
            or not _same_tool(parent, current)
        ):
            return current
        # 親の方が後に起動しているなら、PIDが再利用された無関係なプロセス
        if parent.created_at and current.created_at and parent.created_at > current.created_at:
            return current
        seen.add(parent.pid)
        current = parent


def find_port_owner(
    snapshot: Snapshot, port: int, *, own_pid: int | None = None
) -> ProcessInfo | None:
    """ポートでLISTENしているツールの最上位の起動役を返す。TOOL NEXUS 自身なら None。"""
    protected = ancestors(snapshot, own_pid if own_pid is not None else os.getpid())
    for pid in sorted(snapshot.listeners.get(int(port), frozenset())):
        if pid in protected:
            continue
        owner = root_process(snapshot, pid, stop_at=protected)
        if owner is not None and owner.pid not in protected:
            return owner
    return None


def guess_directory(command_line: str) -> str:
    """コマンドラインから作業ディレクトリを推測する（WMI では取得できないため）。

    1. 実行ファイルが venv 配下（<root>/<venv>/Scripts/python.exe、Linux は bin/python）なら <root>
    2. スクリプト（.py）が絶対パスならその親
    3. どちらも無ければ空（ユーザーに入力させる）
    """
    try:
        tokens = split_command(command_line)
    except ValueError:
        return ""
    if not tokens:
        return ""
    exe = Path(tokens[0])
    if exe.is_absolute() and exe.parent.name.lower() in ("scripts", "bin") and len(exe.parents) >= 3:
        venv = exe.parent.parent
        if (venv / "pyvenv.cfg").is_file() or venv.name.lower() in {".venv", "venv", "env"}:
            return str(venv.parent)
    for token in tokens[1:]:
        script = Path(token)
        if script.suffix.lower() == ".py" and script.is_absolute():
            return str(script.parent)
    return ""


def detect_streamlit(
    snapshot: Snapshot, *, exclude_ports: Iterable[int], own_pid: int | None = None
) -> list[DetectedTool]:
    """このPCで動いている未登録の Streamlit を列挙する。

    ポートを持つ方（子）でポートを判定し、登録には最上位の起動役（親）のコマンドラインを使う。
    TOOL NEXUS 自身と、exclude_ports（登録済み・自身のポート）は除く。
    """
    protected = ancestors(snapshot, own_pid if own_pid is not None else os.getpid())
    excluded = set(exclude_ports)
    found: list[DetectedTool] = []
    for port in sorted(snapshot.listeners):
        if port in excluded:
            continue
        for pid in sorted(snapshot.listeners[port]):
            process = snapshot.processes.get(pid)
            if process is None or pid in protected:
                continue
            if "streamlit" not in process.command_line.lower():
                continue
            owner = root_process(snapshot, pid, stop_at=protected) or process
            directory = guess_directory(owner.command_line)
            found.append(
                DetectedTool(
                    port=port,
                    process=owner,
                    listener_pid=pid,
                    directory=directory,
                    name=Path(directory).name if directory else f"Streamlit {port}",
                )
            )
            break
    return found
