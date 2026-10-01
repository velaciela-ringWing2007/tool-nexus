"""左ナビの凡例（SPEC 8.1）のテスト."""

from __future__ import annotations

from tool_nexus.process.health import Status
from tool_nexus.ui.layout import STATUS_LEGEND


def test_legend_covers_every_status() -> None:
    # 状態を増やしたときに凡例の書き漏れが無いようにする
    statuses = [status for status, _, _ in STATUS_LEGEND]
    assert sorted(statuses, key=lambda s: s.value) == sorted(Status, key=lambda s: s.value)
    assert len(statuses) == len(set(statuses))


def test_legend_texts_are_short_and_explained() -> None:
    # 左ナビは狭いので文言は短く（折り返さない長さ）、説明はマウスを載せたときに出す
    for _, text, detail in STATUS_LEGEND:
        assert 0 < len(text) <= 6
        assert detail.strip()
