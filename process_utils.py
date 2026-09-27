"""プロセスの起動・停止・照合.

psutil は使わず、プロセス情報は PowerShell（Get-CimInstance）で取得する。
subprocess は常にリスト形式・shell=False で実行する。

注意: Popen.pid は親プロセスであり、ポートを持つのは子プロセスである。
そのため停止は taskkill /T で子を含めて行う。
また、PIDはOSに再利用されるため、停止・死活判定の前に必ず
起動時刻（CreationDate）を照合する（verify_pid）。
"""

from __future__ import annotations

import enum
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

from constants import DEFAULT_LOG_FILENAME, KIND_STREAMLIT, PORT_PLACEHOLDER

POWERSHELL_TIMEOUT: float = 15.0
TASKKILL_TIMEOUT: float = 15.0

# subprocess.CREATE_NO_WINDOW などは Windows でのみ定義される。
_CREATE_NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_CREATE_NEW_PROCESS_GROUP: int = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

# 起動フラグ。DETACHED_PROCESS は使わない。
# DETACHED_PROCESS と CREATE_NO_WINDOW を併用すると CREATE_NO_WINDOW が無視され、
# 親（venv の python.exe はリダイレクタ）がコンソール無しになる。その子の python.exe は
# 自分用のコンソールを新規に作ってウィンドウを表示し、それを閉じるとツールが落ちる（実機で確認）。
# CREATE_NO_WINDOW だけなら非表示のコンソールが作られて子に引き継がれ、
# TOOL NEXUS のコンソールとも切り離されるため、こちらを閉じてもツールは動き続ける。
_LAUNCH_FLAGS: int = _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP


class ProcessError(RuntimeError):
    """プロセス操作に失敗した場合に送出する例外."""


class LaunchError(ProcessError):
    """起動前の確認、または起動そのものに失敗した場合に送出する例外."""


class StopError(ProcessError):
    """taskkill が失敗した場合に送出する例外."""


class ProcessQueryError(ProcessError):
    """PowerShell によるプロセス情報の取得に失敗した場合に送出する例外."""


class PidStatus(enum.Enum):
    """記録済みPIDと現在のプロセスの照合結果."""

    MATCH = "match"          # 同じプロセスが生きている
    NOT_FOUND = "not_found"  # そのPIDのプロセスは存在しない
    MISMATCH = "mismatch"    # PIDは存在するが起動時刻が違う（PIDが再利用された）
    UNKNOWN = "unknown"      # 照合に必要な情報が無い、または取得に失敗した


class ProcessNotIdentifiedError(ProcessError):
    """停止対象のプロセスを特定できない場合に送出する例外.

    PIDの再利用で無関係なプロセスを強制終了しないよう、照合が取れない限り停止しない。
    """

    def __init__(self, status: PidStatus) -> None:
        super().__init__("対象プロセスを特定できませんでした。")
        self.status = status


# ----------------------------------------------------------------------
# コマンドの組み立て
# ----------------------------------------------------------------------
def split_command(command: str) -> list[str]:
    """コマンド文字列をargvへ分解する。

    posix=True は C:\\dev\\x のバックスラッシュを消すため posix=False を使う。
    posix=False はトークン両端のクォートを残すので、それを外す。
    閉じていないクォートがあると ValueError を送出する。
    """
    tokens = shlex.split(command, posix=False)
    return [t[1:-1] if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'" else t for t in tokens]


def build_argv(command: str, *, kind: str, port: int | None) -> list[str]:
    """登録内容から起動用のargvを組み立てる（実行ファイルの解決は行わない）。

    コマンド中の {port} は全種別で登録済みのポートに置き換える。
    streamlit のときだけ --server.* を付与し、コマンドに既に書かれていれば二重付与しない。
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
        argv = [arg.replace(PORT_PLACEHOLDER, str(int(port))) for arg in argv]
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
    """
    candidate = Path(executable)
    has_dir_part = candidate.is_absolute() or len(candidate.parts) > 1
    if has_dir_part:
        path = candidate if candidate.is_absolute() else directory / candidate
        if path.is_file():
            return path.resolve()
        raise LaunchError(f"実行ファイルが見つかりません: {path}")

    found = shutil.which(executable, path=None)
    if found is None:
        local = directory / executable
        if local.is_file():
            return local.resolve()
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
# 起動時刻（CreationDate）の正規化
# ----------------------------------------------------------------------
_CIM_DATETIME = re.compile(r"^(\d{14})(?:\.(\d{1,6}))?([+-])(\d{3})$")
_DOTNET_JSON_DATE = re.compile(r"^/Date\((-?\d+)([+-]\d{4})?\)/$")


def normalize_creation_date(value: str | datetime | None) -> str | None:
    """CreationDate を秒精度・ローカルタイムゾーンのISO 8601へ正規化する。

    PowerShell の取得方法によって表現が揺れるため、次の形式を受け付ける：

    * CIM datetime（Get-WmiObject）: ``20260927193031.305749+540``（末尾は分単位のオフセット）
    * .NET JSON（ConvertTo-Json）: ``/Date(1790505031305)/``（エポックミリ秒, UTC）
    * ISO 8601（DateTime.ToString("o") など）: ``2026-09-27T19:30:31.3057490+09:00``

    解釈できない値は None を返す。秒未満は切り捨てる。
    """
    if value is None:
        return None
    parsed = value if isinstance(value, datetime) else _parse_creation_date(str(value).strip())
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()  # タイムゾーン無しはローカル時刻とみなす
    return parsed.astimezone().replace(microsecond=0).isoformat()


def _parse_creation_date(text: str) -> datetime | None:
    if not text:
        return None

    match = _CIM_DATETIME.match(text)
    if match:
        base, _fraction, sign, minutes = match.groups()
        offset = timedelta(minutes=int(minutes)) * (1 if sign == "+" else -1)
        try:
            return datetime.strptime(base, "%Y%m%d%H%M%S").replace(tzinfo=timezone(offset))
        except ValueError:
            return None

    match = _DOTNET_JSON_DATE.match(text.replace("\\/", "/"))
    if match:
        return datetime.fromtimestamp(int(match.group(1)) / 1000, tz=timezone.utc)

    # .NET の "o" 形式は秒未満が7桁あり fromisoformat が受け付けないことがあるため、
    # 秒未満はどのみち切り捨てるので取り除いてから解釈する。
    iso = re.sub(r"(\d{2}:\d{2}:\d{2})\.\d+", r"\1", text)
    if iso.endswith("Z"):
        iso = iso[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        return None


# ----------------------------------------------------------------------
# プロセス情報の取得（PowerShell）
# ----------------------------------------------------------------------
Runner = Callable[..., subprocess.CompletedProcess]


def run_powershell(script: str, *, runner: Runner = subprocess.run) -> str:
    """PowerShell を shell=False で実行し、標準出力を返す。"""
    argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script]
    try:
        result = runner(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=POWERSHELL_TIMEOUT,
            creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProcessQueryError("PowerShellの実行に失敗しました。") from exc
    if result.returncode != 0:
        raise ProcessQueryError(f"PowerShellの実行に失敗しました: {result.stderr.strip()}")
    return result.stdout


def get_process_creation_date(pid: int, *, runner: Runner = subprocess.run) -> str | None:
    """PIDの起動時刻を正規化して返す。プロセスが存在しなければ None。

    取得自体に失敗した場合は ProcessQueryError を送出する
    （「存在しない」と「分からない」を区別するため）。
    """
    # 出力形式を固定するため、DateTime を ToString("o") で文字列化して返させる。
    script = (
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
        f'$p = Get-CimInstance Win32_Process -Filter "ProcessId={int(pid)}"; '
        'if ($p) { $p.CreationDate.ToString("o") }'
    )
    output = run_powershell(script, runner=runner).strip()
    if not output:
        return None
    normalized = normalize_creation_date(output)
    if normalized is None:
        raise ProcessQueryError(f"起動時刻を解釈できませんでした: {output}")
    return normalized


def get_creation_dates(pids: Iterable[int], *, runner: Runner = subprocess.run) -> dict[int, str]:
    """複数PIDの起動時刻をPowerShell 1回でまとめて取得する。存在しないPIDは結果に含まれない。

    一覧の死活監視でツールごとにPowerShellを起動すると遅いため、こちらを使う。
    """
    unique = sorted({int(pid) for pid in pids if pid})
    if not unique:
        return {}
    condition = " OR ".join(f"ProcessId={pid}" for pid in unique)
    script = (
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
        f'Get-CimInstance Win32_Process -Filter "{condition}" | '
        'ForEach-Object { "$($_.ProcessId)`t$($_.CreationDate.ToString(\'o\'))" }'
    )
    result: dict[int, str] = {}
    for line in run_powershell(script, runner=runner).splitlines():
        pid_text, _, date_text = line.strip().partition("\t")
        normalized = normalize_creation_date(date_text)
        if pid_text.isdigit() and normalized:
            result[int(pid_text)] = normalized
    return result


CreationDateLookup = Callable[[int], "str | None"]


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
                creationflags=_LAUNCH_FLAGS,
            )
    except OSError as exc:
        raise LaunchError(f"起動に失敗しました: {exc}") from exc

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
    runner: Runner = subprocess.run,
) -> None:
    """記録済みのプロセスを子ごと強制終了する。

    起動時刻が一致しない限り taskkill を実行しない（PID再利用による誤爆を防ぐ）。
    特定できない場合は ProcessNotIdentifiedError を送出する。
    """
    status = verify_pid(pid, created_at, lookup=lookup)
    if status is not PidStatus.MATCH:
        raise ProcessNotIdentifiedError(status)

    argv = ["taskkill", "/PID", str(int(pid)), "/T", "/F"]
    try:
        result = runner(
            argv,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=TASKKILL_TIMEOUT,
            creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StopError("停止に失敗しました。") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise StopError(f"停止に失敗しました: {detail}")
