"""tool_nexus.ui.grouping のテスト（グループ分け・並び替え・起動／停止の対象）."""

from __future__ import annotations

import pytest

from tool_nexus.core.constants import HEALTH_NONE, KIND_LINK, KIND_STREAMLIT
from tool_nexus.core.models import Tool
from tool_nexus.process.health import Status, ToolHealth
from tool_nexus.ui.grouping import (
    SORT_MANUAL,
    SORT_NAME,
    SORT_PORT,
    SORT_STARTED,
    SORT_STATUS,
    group_healths,
    should_start,
    should_stop,
    sort_healths,
)


def health(name: str, status: Status = Status.STOPPED, **tool_values) -> ToolHealth:
    values = {"id": abs(hash(name)) % 10000, "name": name, "directory": "/x", "command": "c",
              "kind": KIND_STREAMLIT, "port": None} | tool_values
    return ToolHealth(Tool(**values), None, status)


def names(healths) -> list[str]:
    return [h.tool.name for h in healths]


class TestGroup:
    def test_named_groups_first_by_name_then_ungrouped(self) -> None:
        items = [
            health("a", group_name=""),
            health("b", group_name="在庫"),
            health("c", group_name="Docs"),
            health("d", group_name="在庫"),
        ]
        grouped = group_healths(items)
        assert [g for g, _ in grouped] == ["Docs", "在庫", ""]
        assert names(grouped[1][1]) == ["b", "d"]  # 中の順番は保つ

    def test_case_insensitive_group_order(self) -> None:
        grouped = group_healths([health("x", group_name="beta"), health("y", group_name="Alpha")])
        assert [g for g, _ in grouped] == ["Alpha", "beta"]

    def test_empty(self) -> None:
        assert group_healths([]) == []


class TestSort:
    def test_manual(self) -> None:
        items = [health("b", sort_order=1), health("c", sort_order=0), health("a", sort_order=1)]
        assert names(sort_healths(items, SORT_MANUAL)) == ["c", "a", "b"]

    def test_name(self) -> None:
        items = [health("b"), health("C"), health("a")]
        assert names(sort_healths(items, SORT_NAME)) == ["a", "b", "C"]

    def test_status(self) -> None:
        items = [
            health("stopped", Status.STOPPED),
            health("link", Status.LINK),
            health("failed", Status.FAILED),
            health("running", Status.RUNNING),
            health("starting", Status.STARTING),
        ]
        assert names(sort_healths(items, SORT_STATUS)) == ["running", "starting", "failed", "link", "stopped"]

    def test_port_missing_last(self) -> None:
        items = [health("none"), health("8600", port=8600), health("8502", port=8502)]
        assert names(sort_healths(items, SORT_PORT)) == ["8502", "8600", "none"]

    def test_started_newest_first_and_never_last(self) -> None:
        items = [
            health("never"),
            health("old", last_started_at="2026-09-27T10:00:00+09:00"),
            health("new", last_started_at="2026-09-28T09:00:00+09:00"),
        ]
        assert names(sort_healths(items, SORT_STARTED)) == ["new", "old", "never"]


class TestTargets:
    @pytest.mark.parametrize(
        ("status", "last_pid", "expected"),
        [
            (Status.STOPPED, None, True),
            (Status.FAILED, None, True),
            (Status.RUNNING, 1, False),
            (Status.STARTING, 1, False),
            (Status.UNKNOWN, 5, False),   # 監視しない設定で起動記録あり → 動いているかもしれない
            (Status.UNKNOWN, None, True),
        ],
    )
    def test_should_start(self, status: Status, last_pid, expected: bool) -> None:
        assert should_start(health("t", status, last_pid=last_pid)) is expected

    def test_links_are_never_started_or_stopped(self) -> None:
        link = health("l", Status.LINK, kind=KIND_LINK, health_mode=HEALTH_NONE, target="https://x")
        assert should_start(link) is False
        assert should_stop(link) is False

    @pytest.mark.parametrize(
        ("status", "last_pid", "expected"),
        [(Status.RUNNING, None, True), (Status.STARTING, 1, True), (Status.STOPPED, None, False), (Status.FAILED, 1, False)],
    )
    def test_should_stop(self, status: Status, last_pid, expected: bool) -> None:
        assert should_stop(health("t", status, last_pid=last_pid)) is expected


class TestStopTools:
    """グループの停止: 特定できないものは止めずに知らせる（SPEC 6.11）."""

    def test_split_results(self, tmp_path) -> None:
        from tool_nexus.core.models import build_tool
        from tool_nexus.core.repositories import ToolRepository
        from tool_nexus.process.base import PidStatus, ProcessNotIdentifiedError, StopError
        from tool_nexus.ui.actions import stop_tools

        repo = ToolRepository(tmp_path / "t.sqlite3")
        repo.initialize()
        created = {}
        for index, name in enumerate(["ok", "外部", "再利用", "失敗", "停止中"]):
            tool = repo.create(build_tool(name=name, directory="/x", command="c", port=8600 + index,
                                          group_name="G", check_directory=False))
            repo.record_start(tool.id, pid=100 + index, created_at="2026-09-28T10:00:00+09:00")
            created[name] = repo.get_by_id(tool.id)

        def stopper(pid, created_at):
            if pid == 101:
                raise ProcessNotIdentifiedError(PidStatus.UNKNOWN)
            if pid == 102:
                raise ProcessNotIdentifiedError(PidStatus.MISMATCH)
            if pid == 103:
                raise StopError("権限がありません")

        healths = [ToolHealth(created[n], True, Status.RUNNING) for n in ["ok", "外部", "再利用", "失敗"]]
        healths.append(ToolHealth(created["停止中"], False, Status.STOPPED))
        summary = stop_tools(repo, healths, stopper=stopper)

        assert summary.stopped == ["ok"]
        assert summary.not_identified == ["外部", "再利用"]
        assert summary.failed == ["失敗: 権限がありません"]
        assert repo.get_by_id(created["ok"].id).last_stopped_at        # 停止を記録
        assert repo.get_by_id(created["再利用"].id).last_pid is None   # 古い記録は消す
        assert repo.get_by_id(created["外部"].id).last_pid == 101      # 分からないものは触らない
        assert repo.get_by_id(created["停止中"].id).last_stopped_at is None  # 停止中は対象外
