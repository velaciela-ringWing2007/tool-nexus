"""グループ分けと並び替え、グループの起動・停止の対象の決め方（SPEC 6.11 / 8.1）.

画面に依存しない純粋な処理だけを置く（テストしやすくするため）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from tool_nexus.core.constants import KIND_LINK
from tool_nexus.process.health import Status, ToolHealth

SORT_MANUAL = "表示順"
SORT_NAME = "名前"
SORT_STATUS = "状態"
SORT_PORT = "ポート"
SORT_STARTED = "最終起動"
SORT_OPTIONS: tuple[str, ...] = (SORT_MANUAL, SORT_NAME, SORT_STATUS, SORT_PORT, SORT_STARTED)

# 並び順「状態」での順位（小さいほど上）
STATUS_RANK: dict[Status, int] = {
    Status.RUNNING: 0,
    Status.STARTING: 1,
    Status.FAILED: 2,
    Status.UNKNOWN: 2,
    Status.MISSING: 2,
    Status.LINK: 3,
    Status.STOPPED: 4,
}


def _name_key(health: ToolHealth) -> str:
    return health.tool.name.casefold()


def sort_healths(healths: Iterable[ToolHealth], sort: str) -> list[ToolHealth]:
    """並び順に従って並べる。値の無いもの（ポート・最終起動）は最後に回す。"""
    items = list(healths)
    if sort == SORT_NAME:
        return sorted(items, key=lambda h: (_name_key(h), h.tool.id or 0))
    if sort == SORT_STATUS:
        return sorted(items, key=lambda h: (STATUS_RANK.get(h.status, 9), h.tool.sort_order, _name_key(h)))
    if sort == SORT_PORT:
        return sorted(items, key=lambda h: (h.tool.port is None, h.tool.port or 0, _name_key(h)))
    if sort == SORT_STARTED:
        # 新しい順。ISO 8601 の文字列は同じ形式なら文字列の比較で時刻順になる
        started = [h for h in items if h.tool.last_started_at]
        never = [h for h in items if not h.tool.last_started_at]
        started.sort(key=lambda h: h.tool.last_started_at, reverse=True)
        never.sort(key=lambda h: (h.tool.sort_order, _name_key(h)))
        return started + never
    return sorted(items, key=lambda h: (h.tool.sort_order, _name_key(h), h.tool.id or 0))


def group_healths(healths: Iterable[ToolHealth]) -> list[tuple[str, list[ToolHealth]]]:
    """グループごとにまとめる。グループは名前順で「未分類」（空）は最後。中の順番は保つ。"""
    groups: dict[str, list[ToolHealth]] = {}
    for health in healths:
        groups.setdefault(health.tool.group_name, []).append(health)
    return sorted(groups.items(), key=lambda item: (item[0] == "", item[0].casefold()))


def should_start(health: ToolHealth) -> bool:
    """まとめて起動・グループの起動で起動するか。

    起動中・起動中…は飛ばす（二重起動しない）。監視しない設定で起動記録が残っているものも、
    動いている可能性があるので飛ばす。リンクは起動しない。
    """
    if health.tool.kind == KIND_LINK:
        return False
    if health.status in (Status.RUNNING, Status.STARTING):
        return False
    if health.status is Status.UNKNOWN and health.tool.last_pid:
        return False
    return True


def should_stop(health: ToolHealth) -> bool:
    """グループの停止で停止を試みるか（起動中のものだけ。リンクは対象外）。"""
    return health.tool.kind != KIND_LINK and health.can_stop


@dataclass(slots=True)
class StopSummary:
    """グループの停止の結果."""

    stopped: list[str] = field(default_factory=list)
    not_identified: list[str] = field(default_factory=list)  # 止めずに知らせるもの
    failed: list[str] = field(default_factory=list)  # 「名前: 理由」
