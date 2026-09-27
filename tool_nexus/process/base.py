"""プロセス操作で共有する例外・データ型・起動時刻の正規化（OSに依存しない土台）.

OS別の実装（osdep.windows / osdep.linux）と process.control の両方から参照される。
循環 import を避けるため、このモジュールは他のプロジェクト内モジュールに依存しない。
"""

from __future__ import annotations

import enum
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

Runner = Callable[..., subprocess.CompletedProcess]
CreationDateLookup = Callable[[int], "str | None"]


class ProcessError(RuntimeError):
    """プロセス操作に失敗した場合に送出する例外."""


class LaunchError(ProcessError):
    """起動前の確認、または起動そのものに失敗した場合に送出する例外."""


class StopError(ProcessError):
    """停止（taskkill / シグナル送信）が失敗した場合に送出する例外."""


class ProcessQueryError(ProcessError):
    """プロセス情報の取得（PowerShell / /proc）に失敗した場合に送出する例外."""


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


@dataclass(frozen=True, slots=True)
class ProcessInfo:
    pid: int
    ppid: int
    name: str
    command_line: str
    created_at: str | None


@dataclass(frozen=True, slots=True)
class Snapshot:
    """ある時点のプロセス一覧と、LISTEN中のポート → PID の対応."""

    processes: dict[int, ProcessInfo]
    listeners: dict[int, frozenset[int]]


# ----------------------------------------------------------------------
# 起動時刻の正規化
# ----------------------------------------------------------------------
_CIM_DATETIME = re.compile(r"^(\d{14})(?:\.(\d{1,6}))?([+-])(\d{3})$")
_DOTNET_JSON_DATE = re.compile(r"^/Date\((-?\d+)([+-]\d{4})?\)/$")


def normalize_creation_date(value: str | datetime | None) -> str | None:
    """起動時刻を秒精度・ローカルタイムゾーンのISO 8601へ正規化する。

    Windows の PowerShell は取得方法によって表現が揺れるため、次の形式を受け付ける：

    * CIM datetime（Get-WmiObject）: ``20260927193031.305749+540``（末尾は分単位のオフセット）
    * .NET JSON（ConvertTo-Json）: ``/Date(1790505031305)/``（エポックミリ秒, UTC）
    * ISO 8601（DateTime.ToString("o") など）: ``2026-09-27T19:30:31.3057490+09:00``

    Linux は /proc から求めた datetime をそのまま渡す。
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
