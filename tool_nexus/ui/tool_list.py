"""ツール一覧（フラグメントで定期的に描き直す）."""

from __future__ import annotations

import time
from datetime import datetime

import streamlit as st

from tool_nexus.core.constants import KIND_LINK, health_mode_label, kind_label
from tool_nexus.core.models import Tool, is_url
from tool_nexus.core.repositories import ToolRepository
from tool_nexus.core.settings import parse_interval
from tool_nexus.process.health import Status, ToolHealth, derive_status, is_check_due, probe_all
from tool_nexus.process.link_server import link_url
from tool_nexus.ui.actions import start_tool, stop_tool
from tool_nexus.ui.state import (
    KIND_ALL,
    STATUS_FILTERS,
    format_time,
    health_timeout,
    open_dialog,
    render_flash,
    request_check,
)
from tool_nexus.ui.styles import (
    escape_html,
    render_note,
    render_port_link,
    render_summary,
    render_tool_summary,
)

# 一覧フラグメントの再描画の間隔。ヘルスチェック自体は設定の間隔（既定60秒）ごとにだけ行い、
# それ以外の再描画では前回の結果を使う（health.is_check_due）。
# 起動中…のツールがある間は毎回チェックするため、ヘルスが通れば最長でもこの間隔で表示が切り替わる。
# run_every をその場で切り替える方式（フラグメントの外から登録し直す）は、
# 前回の描画が消えずに残ったため採用しない（実機で確認）。
LIST_TICK = "3s"


def open_url(tool: Tool) -> str | None:
    """「開く」のURL。

    ツールは 127.0.0.1 固定で、ユーザー入力は整数のポートのみ使う。
    リンクは URL をそのまま、ローカルのファイルは TOOL NEXUS の配信サーバーのURL（SPEC 6.10）。
    """
    if tool.kind == KIND_LINK:
        return link_url(tool)
    return f"http://127.0.0.1:{int(tool.port)}" if tool.port else None


NOTICES: dict[Status, str] = {
    Status.STARTING: "起動中…（応答を待っています）",
    Status.FAILED: "起動できていない可能性があります。ログを確認してください。",
    Status.UNKNOWN: "",
    Status.MISSING: "リンク先が見つかりません。移動・削除されていないか確認してください。",
}


def render_link_row(tool: Tool, health: ToolHealth) -> None:
    """リンクの行。起動・停止・ログは無く、「開く」と編集だけ（SPEC 6.10）。

    列の数と幅はツールの行と揃え、ボタンの位置がずれないようにする。
    """
    with st.container(key=f"tn-row-{tool.id}"):
        main_col, port_col, _action_col, _log_col, edit_col = st.columns(
            [6, 1.5, 0.9, 0.45, 0.45], vertical_alignment="center"
        )
        with main_col:
            render_tool_summary(
                name=tool.name,
                status=health.status.value,
                status_label=health.label,
                meta=[kind_label(tool.kind), "Web" if is_url(tool.target) else "ローカル"],
                path=tool.target,
                notice=NOTICES.get(health.status, ""),
            )
        with port_col:
            render_port_link(None, open_url(tool) if health.status is Status.LINK else None)
        with edit_col:
            if st.button(":material/edit:", key=f"edit_{tool.id}", help="編集・削除"):
                open_dialog("edit", tool)


def render_tool_row(
    repository: ToolRepository, settings: dict[str, str], health: ToolHealth
) -> None:
    """1件を1行で描画する。

    行あたりのウィジェットは 起動/停止・ログ・編集 の3つに抑える。
    状態・名前・パスは1つのHTML、「開く」は素のアンカーで描く（0ウィジェット）。
    """
    tool = health.tool
    if tool.kind == KIND_LINK:
        render_link_row(tool, health)
        return
    with st.container(key=f"tn-row-{tool.id}"):
        main_col, port_col, action_col, log_col, edit_col = st.columns(
            [6, 1.5, 0.9, 0.45, 0.45], vertical_alignment="center"
        )
        with main_col:
            notice = NOTICES.get(health.status, "")
            if health.status is Status.UNKNOWN:
                notice = "監視しない設定です" if tool.health_mode == "none" else "状態を確認できませんでした"
            started = format_time(tool.last_started_at)
            render_tool_summary(
                name=tool.name,
                status=health.status.value,
                status_label=health.label,
                meta=[
                    kind_label(tool.kind),
                    f"監視: {health_mode_label(tool.health_mode)}",
                    f"最終起動 {started}" if started else "",
                    "まとめて起動" if tool.autostart else "",
                ],
                path=tool.directory,
                notice=notice,
            )
        with port_col:
            render_port_link(tool.port, open_url(tool))
        with action_col:
            if health.can_stop:
                if st.button("停止", key=f"stop_{tool.id}", use_container_width=True):
                    with st.spinner("停止しています…"):
                        handled = stop_tool(repository, tool)
                    if not handled:
                        # 記録から特定できない → ポートから引き直して確認を取る
                        open_dialog("port_stop", tool)
                    request_check()
                    st.rerun(scope="fragment")
            elif st.button("起動", key=f"start_{tool.id}", type="primary", use_container_width=True):
                start_tool(repository, settings, tool)
                request_check()
                st.rerun(scope="fragment")
        with log_col:
            if st.button(":material/description:", key=f"log_{tool.id}", help="ログ"):
                open_dialog("log", tool)
        with edit_col:
            if st.button(":material/edit:", key=f"edit_{tool.id}", help="編集・削除"):
                open_dialog("edit", tool)


def matches_query(tool: Tool, query: str) -> bool:
    if not query:
        return True
    haystack = " ".join(
        [tool.name, tool.directory, tool.command, tool.target, tool.description, str(tool.port or "")]
    ).lower()
    return all(word in haystack for word in query.lower().split())


def check_tools(
    repository: ToolRepository, settings: dict[str, str], tools: list[Tool]
) -> list[ToolHealth]:
    """必要なときだけヘルスチェックを行い、それ以外は前回の結果から状態を求める。"""
    cache: dict[int, bool | None] = st.session_state["health_cache"]
    cached = [ToolHealth(t, cache.get(int(t.id)), derive_status(t, cache.get(int(t.id)))) for t in tools]
    due = is_check_due(
        last_checked=st.session_state["health_checked_at"],
        now=time.monotonic(),
        interval=parse_interval(settings.get("health_interval", "60s")),
        any_starting=any(h.status is Status.STARTING for h in cached),
        forced=st.session_state["force_check"] or any(int(t.id) not in cache for t in tools),
    )
    if not due:
        return cached

    alive_by_id = probe_all(tools, timeout=health_timeout(settings))
    st.session_state["force_check"] = False
    st.session_state["health_checked_at"] = time.monotonic()
    st.session_state["health_checked_label"] = f"{datetime.now():%H:%M:%S}"
    results: list[ToolHealth] = []
    for tool in tools:
        alive = alive_by_id.get(int(tool.id))
        cache[int(tool.id)] = alive
        if alive:
            repository.mark_seen(int(tool.id))
        results.append(ToolHealth(tool, alive, derive_status(tool, alive)))
    return results


@st.fragment(run_every=LIST_TICK)
def render_tool_list(repository: ToolRepository, settings: dict[str, str]) -> None:
    """状態表示の部分。st.fragment(run_every=LIST_TICK) で包んで定期的に再実行する。

    ページ全体は再実行しない（件数が増えると全体の再実行は重くなる）。
    """
    render_flash()

    kind = st.session_state["kind_filter"]
    tools = [
        tool
        for tool in repository.list_all()
        if (kind == KIND_ALL or tool.kind == kind)
        and matches_query(tool, st.session_state["search_query"].strip())
    ]
    healths = check_tools(repository, settings, tools)

    wanted = STATUS_FILTERS[st.session_state["status_filter"]]
    shown = [h for h in healths if h.status in wanted]
    running = sum(h.status is Status.RUNNING for h in healths)
    launchable = sum(h.tool.kind != KIND_LINK for h in healths)  # リンクは起動しないので数えない
    links = len(healths) - launchable

    with st.container(key="tn-listhead"):
        summary_col, recheck_col = st.columns([8, 1.5], vertical_alignment="center")
        with summary_col:
            render_summary(
                [
                    f"<span>起動中 <strong>{running}</strong> / {launchable}</span>",
                    f"<span>リンク {links}</span>" if links else "",
                    f"<span>表示 {len(shown)} 件</span>",
                    f"<span>最終確認 {escape_html(st.session_state['health_checked_label'])}</span>",
                ]
            )
        # 押すとフラグメントが再実行され、その場でヘルスチェックし直す。
        recheck_col.button(
            "再チェック",
            icon=":material/refresh:",
            use_container_width=True,
            key="recheck",
            on_click=request_check,
        )

    if not tools:
        if repository.count() == 0:
            render_note("ツールが登録されていません。「追加」から登録してください。")
        else:
            render_note("条件に一致するツールはありません。")
        return
    if not shown:
        render_note("この状態のツールはありません。")
        return

    for health in shown:
        render_tool_row(repository, settings, health)
