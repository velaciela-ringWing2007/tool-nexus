"""Windows 用のOS依存機能（SPEC 3.2）.

psutil は使わず、プロセス情報は PowerShell（Get-CimInstance / Get-NetTCPConnection）で取得する。
このモジュールはどのOSでも import できる（テストで PowerShell をフェイクに差し替えて検証する）。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Iterable

from tool_nexus.process.base import (
    ProcessInfo,
    ProcessQueryError,
    Runner,
    Snapshot,
    StopError,
    normalize_creation_date,
)

NAME = "windows"

POWERSHELL_TIMEOUT: float = 15.0
TASKKILL_TIMEOUT: float = 15.0
SNAPSHOT_TIMEOUT: float = 60.0
DIALOG_TIMEOUT: float = 600.0  # ファイル選択はユーザーの操作を待つ

# subprocess.CREATE_NO_WINDOW などは Windows でのみ定義される。
_CREATE_NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_CREATE_NEW_PROCESS_GROUP: int = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

# 起動時に Popen へ渡す引数。DETACHED_PROCESS は使わない。
# DETACHED_PROCESS と CREATE_NO_WINDOW を併用すると CREATE_NO_WINDOW が無視され、
# 親（venv の python.exe はリダイレクタ）がコンソール無しになる。その子の python.exe は
# 自分用のコンソールを新規に作ってウィンドウを表示し、それを閉じるとツールが落ちる（実機で確認）。
# CREATE_NO_WINDOW だけなら非表示のコンソールが作られて子に引き継がれ、
# TOOL NEXUS のコンソールとも切り離されるため、こちらを閉じてもツールは動き続ける。
LAUNCH_KWARGS: dict = {"creationflags": _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP}

# コマンドの分解: posix=True は C:\dev\x のバックスラッシュを消すため posix=False を使う
POSIX_SPLIT = False

# venv が無いときに使う Python（python はストアのエイリアスのことがあるため py を優先）
DEFAULT_PYTHONS: tuple[str, ...] = ("py", "python")

# ファイル選択で選べる実行ファイルの説明
EXECUTABLE_LABEL = ".py / .exe / .html"


def is_executable_file(path: Path) -> bool:
    """exe 種別として登録できるファイルか。"""
    return path.is_file() and path.suffix.lower() == ".exe"


def track_child(proc: subprocess.Popen) -> None:
    """Windows では終了したプロセスがゾンビとして残らないため、回収は不要。"""


# ----------------------------------------------------------------------
# PowerShell
# ----------------------------------------------------------------------
def run_powershell(
    script: str, *, runner: Runner = subprocess.run, timeout: float = POWERSHELL_TIMEOUT
) -> str:
    """PowerShell を shell=False で実行し、標準出力を返す。"""
    argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-STA", "-Command", script]
    try:
        result = runner(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
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


# ----------------------------------------------------------------------
# 停止
# ----------------------------------------------------------------------
def kill_tree(pid: int, *, runner: Runner = subprocess.run) -> None:
    """プロセスを子ごと強制終了する（taskkill /T /F）。照合は呼び出し側で済ませておくこと。

    Popen.pid は親（venv の python.exe はリダイレクタ）で、ポートを持つのは子なので /T が必要。
    """
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


# ----------------------------------------------------------------------
# プロセス一覧とLISTEN中のポート
# ----------------------------------------------------------------------
_SNAPSHOT_SCRIPT = (
    "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
    "$procs = foreach ($p in Get-CimInstance Win32_Process) { [pscustomobject]@{ "
    "pid=$p.ProcessId; ppid=$p.ParentProcessId; name=$p.Name; cmd=$p.CommandLine; "
    "created=$(if ($p.CreationDate) { $p.CreationDate.ToString('o') } else { $null }) } }; "
    "$listen = foreach ($c in Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue) { "
    "[pscustomobject]@{ port=$c.LocalPort; pid=$c.OwningProcess } }; "
    "[pscustomobject]@{ processes=@($procs); listeners=@($listen) } | ConvertTo-Json -Depth 3 -Compress"
)


def parse_snapshot(text: str) -> Snapshot:
    """PowerShell の JSON 出力を Snapshot に変換する。"""
    try:
        data = json.loads(text or "{}")
    except json.JSONDecodeError as exc:
        raise ProcessQueryError("プロセス一覧を解釈できませんでした。") from exc

    processes: dict[int, ProcessInfo] = {}
    for item in data.get("processes") or []:
        try:
            pid = int(item["pid"])
        except (KeyError, TypeError, ValueError):
            continue
        processes[pid] = ProcessInfo(
            pid=pid,
            ppid=int(item.get("ppid") or 0),
            name=str(item.get("name") or ""),
            command_line=str(item.get("cmd") or ""),
            created_at=normalize_creation_date(item.get("created")),
        )

    listeners: dict[int, set[int]] = {}
    for item in data.get("listeners") or []:
        try:
            port, pid = int(item["port"]), int(item["pid"])
        except (KeyError, TypeError, ValueError):
            continue
        listeners.setdefault(port, set()).add(pid)
    return Snapshot(processes, {port: frozenset(pids) for port, pids in listeners.items()})


def take_snapshot(*, runner: Runner = subprocess.run) -> Snapshot:
    """現在のプロセス一覧とLISTEN中のポートを PowerShell 1回で取得する（実測 約1秒）。"""
    return parse_snapshot(run_powershell(_SNAPSHOT_SCRIPT, runner=runner, timeout=SNAPSHOT_TIMEOUT))


# ----------------------------------------------------------------------
# ファイル選択（Windows Forms）
# ----------------------------------------------------------------------
def _ps_literal(value: str) -> str:
    """PowerShell の単一引用符文字列にする（' は '' に）。"""
    return "'" + value.replace("'", "''") + "'"


_DIALOG_PRELUDE = (
    "Add-Type -AssemblyName System.Windows.Forms; "
    "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
    # ブラウザの裏に隠れないよう、最前面のフォームを親にして開く
    "$owner = New-Object System.Windows.Forms.Form; "
    "$owner.TopMost = $true; $owner.ShowInTaskbar = $false; "
)


def pick_file(initial_dir: str | None = None, *, runner: Runner = subprocess.run) -> Path | None:
    """ファイル選択ダイアログを開く。キャンセルされたら None。"""
    script = _DIALOG_PRELUDE + (
        "$d = New-Object System.Windows.Forms.OpenFileDialog; "
        "$d.Title = '起動するファイルを選択'; "
        "$d.Filter = 'Python / 実行ファイル / HTML / PDF (*.py;*.exe;*.html;*.htm;*.pdf;*.svg)|"
        "*.py;*.exe;*.html;*.htm;*.pdf;*.svg'; "
    )
    if initial_dir:
        script += f"$d.InitialDirectory = {_ps_literal(initial_dir)}; "
    script += "if ($d.ShowDialog($owner) -eq 'OK') { $d.FileName }; $owner.Dispose()"
    output = run_powershell(script, runner=runner, timeout=DIALOG_TIMEOUT).strip()
    return Path(output) if output else None


def pick_folder(initial_dir: str | None = None, *, runner: Runner = subprocess.run) -> Path | None:
    """フォルダ選択ダイアログを開く。キャンセルされたら None。"""
    script = _DIALOG_PRELUDE + (
        "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
        "$d.Description = '作業ディレクトリを選択'; "
        "$d.ShowNewFolderButton = $false; "
    )
    if initial_dir:
        script += f"$d.SelectedPath = {_ps_literal(initial_dir)}; "
    script += "if ($d.ShowDialog($owner) -eq 'OK') { $d.SelectedPath }; $owner.Dispose()"
    output = run_powershell(script, runner=runner, timeout=DIALOG_TIMEOUT).strip()
    return Path(output) if output else None
