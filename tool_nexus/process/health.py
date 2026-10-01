"""死活監視と、表示用の状態の導出.

監視は画面を開いている間だけ行う（常駐監視はしない）。
状態はDBに保存せず、毎回ここで求める。DBの last_started_at / last_seen_at は
状態ではなく記録であり、「起動中…」「起動できていない可能性」の導出にだけ使う。
"""

from __future__ import annotations

import enum
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable

from tool_nexus.core.constants import HEALTH_HTTP, HEALTH_PROCESS, KIND_LINK, KIND_STREAMLIT
from tool_nexus.core.models import Tool, is_url
from tool_nexus.process.control import (
    CreationDateLookup,
    PidStatus,
    ProcessQueryError,
    get_creation_dates,
    verify_pid,
)

STREAMLIT_HEALTH_PATH = "/_stcore/health"

# 起動操作からこの時間はヘルスが通らなくても「起動中…」とみなす。
STARTING_GRACE = timedelta(seconds=30)
# これを過ぎても一度もヘルスが通っていなければ「起動できていない可能性」を出し続ける期間。
# 無言で回り続けるのを避けつつ、古い失敗をいつまでも出さないための上限。
FAILED_WINDOW = timedelta(minutes=10)

MAX_WORKERS = 8


class Status(enum.Enum):
    RUNNING = "running"    # 起動中（ヘルス通過）
    STARTING = "starting"  # 起動中…（起動操作から30秒以内）
    FAILED = "failed"      # 起動できていない可能性がある
    STOPPED = "stopped"    # 停止
    UNKNOWN = "unknown"    # 監視しない / 判定できない
    LINK = "link"          # リンク（起動しない。SPEC 6.10）
    MISSING = "missing"    # リンク先のファイル／フォルダが見つからない


STATUS_LABELS: dict[Status, str] = {
    Status.RUNNING: "起動中",
    Status.STARTING: "起動中…",
    Status.FAILED: "起動できていない可能性があります",
    Status.STOPPED: "停止",
    Status.UNKNOWN: "不明",
    Status.LINK: "リンク",
    Status.MISSING: "リンク先が見つかりません",
}


# ----------------------------------------------------------------------
# 個別の判定
# ----------------------------------------------------------------------
Opener = Callable[..., object]

# 社用PCではプロキシが設定されていることがあるため、127.0.0.1 への確認はプロキシを通さない。
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def health_url(port: int, kind: str) -> str:
    """ヘルスチェック先のURL。Streamlit は専用のエンドポイントを使う。"""
    path = STREAMLIT_HEALTH_PATH if kind == KIND_STREAMLIT else "/"
    return f"http://127.0.0.1:{int(port)}{path}"


def check_http(
    port: int | None, *, kind: str, timeout: float, opener: Opener = _NO_PROXY_OPENER.open
) -> bool:
    """HTTPで応答するかを返す。

    Streamlit は /_stcore/health が 200 を返すことを確認する（対象ツールの改造は不要）。
    それ以外は待ち受けていて何らかのHTTP応答（404等を含む）を返せば生存とみなす。
    """
    if not port:
        return False
    try:
        with opener(health_url(port, kind), timeout=timeout) as response:
            return kind != KIND_STREAMLIT or getattr(response, "status", 200) == 200
    except urllib.error.HTTPError:
        return kind != KIND_STREAMLIT
    except (urllib.error.URLError, OSError, ValueError):
        # 接続拒否・タイムアウトなど
        return False


def check_process(
    pid: int | None, created_at: str | None, *, lookup: CreationDateLookup
) -> bool | None:
    """記録済みのプロセスが生きているかを返す。照合できない場合は None。

    起動記録が無い場合は「起動していない」とみなして False を返す。
    """
    if not pid or not created_at:
        return False
    status = verify_pid(pid, created_at, lookup=lookup)
    if status is PidStatus.MATCH:
        return True
    if status is PidStatus.UNKNOWN:
        return None
    return False


def probe(
    tool: Tool,
    *,
    timeout: float,
    lookup: CreationDateLookup,
    opener: Opener = _NO_PROXY_OPENER.open,
) -> bool | None:
    """死活監視モードに応じて生存確認する。none モードは常に None（不明）。"""
    if tool.health_mode == HEALTH_HTTP:
        return check_http(tool.port, kind=tool.kind, timeout=timeout, opener=opener)
    if tool.health_mode == HEALTH_PROCESS:
        return check_process(tool.last_pid, tool.last_pid_created_at, lookup=lookup)
    return None


def probe_all(
    tools: Iterable[Tool],
    *,
    timeout: float,
    opener: Opener = _NO_PROXY_OPENER.open,
    batch_lookup: Callable[[list[int]], dict[int, str]] = get_creation_dates,
) -> dict[int, bool | None]:
    """全ツールを並列に確認し、{ツールID: 生存} を返す。

    process モードのPIDはPowerShell 1回でまとめて照会する。
    """
    targets = [tool for tool in tools if tool.id is not None]
    pids = [t.last_pid for t in targets if t.health_mode == HEALTH_PROCESS and t.last_pid]

    lookup: CreationDateLookup
    try:
        dates = batch_lookup(pids) if pids else {}
    except ProcessQueryError as exc:
        failure = exc

        def lookup(pid: int) -> str | None:
            raise ProcessQueryError(str(failure))

    else:
        lookup = dates.get

    if not targets:
        return {}
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(targets))) as pool:
        results = pool.map(
            lambda t: probe(t, timeout=timeout, lookup=lookup, opener=opener), targets
        )
        return {int(t.id): alive for t, alive in zip(targets, results)}


def is_check_due(
    *,
    last_checked: float | None,
    now: float,
    interval: float,
    any_starting: bool,
    forced: bool,
) -> bool:
    """実際にヘルスチェックを行うべきかを返す。

    画面の再描画は短い間隔で行い、ヘルスチェック自体は設定の間隔ごとにだけ行う。
    ただし起動中…のツールがある間と、再チェック・起動・停止の直後は毎回確認する。
    """
    if forced or any_starting or last_checked is None:
        return True
    return now - last_checked >= interval


# ----------------------------------------------------------------------
# 表示用の状態
# ----------------------------------------------------------------------
def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.astimezone()


def link_status(tool: Tool) -> Status:
    """リンクの状態。URL は常に LINK、ローカルはリンク先があれば LINK、無ければ MISSING。"""
    if not tool.target or is_url(tool.target) or Path(tool.target).exists():
        return Status.LINK
    return Status.MISSING


def expects_running(tool: Tool) -> bool:
    """起動の記録があり、その後に停止の記録が無いか（起動したはずのツールか）。"""
    started = _parse(tool.last_started_at)
    if started is None:
        return False
    stopped = _parse(tool.last_stopped_at)
    return stopped is None or stopped < started


def derive_status(tool: Tool, alive: bool | None, *, now: datetime | None = None) -> Status:
    """生存確認の結果と起動記録から、表示用の状態を求める。

    | 条件                                                   | 状態     |
    | ------------------------------------------------------ | -------- |
    | 生存                                                   | RUNNING  |
    | 判定できない（none モード、照会失敗）                  | UNKNOWN  |
    | 起動後に停止操作をした                                 | STOPPED  |
    | 起動操作から30秒以内                                   | STARTING |
    | 30秒超〜10分、その起動以降に一度もヘルスが通っていない | FAILED   |
    | それ以外（記録が古い・無い、起動後に一度は通った）     | STOPPED  |
    """
    if tool.kind == KIND_LINK:
        return link_status(tool)
    if alive is True:
        return Status.RUNNING
    if alive is None:
        return Status.UNKNOWN

    started = _parse(tool.last_started_at)
    if started is None:
        return Status.STOPPED
    stopped = _parse(tool.last_stopped_at)
    if stopped is not None and stopped >= started:
        return Status.STOPPED
    current = now or datetime.now().astimezone()
    elapsed = current - started
    if elapsed <= STARTING_GRACE:
        return Status.STARTING

    seen = _parse(tool.last_seen_at)
    seen_since_start = seen is not None and seen >= started
    if not seen_since_start and elapsed <= FAILED_WINDOW:
        return Status.FAILED
    return Status.STOPPED


@dataclass(frozen=True, slots=True)
class ToolHealth:
    tool: Tool
    alive: bool | None
    status: Status

    @property
    def label(self) -> str:
        return STATUS_LABELS[self.status]

    @property
    def can_stop(self) -> bool:
        """停止ボタンを出すか。none モードなど判定できない場合は起動記録の有無で決める。"""
        if self.status in (Status.RUNNING, Status.STARTING):
            return True
        if self.status is Status.UNKNOWN:
            return bool(self.tool.last_pid)
        return False
