"""上部バー・左ナビ・ツールバー."""

from __future__ import annotations

import streamlit as st

from tool_nexus.core.repositories import ToolRepository
from tool_nexus.ui.actions import start_autostart_tools
from tool_nexus.ui.state import (
    KIND_TABS,
    SCREEN_SETTINGS,
    STATUS_FILTERS,
    go_to_settings,
    open_dialog,
    select_kind,
)
from tool_nexus.ui.styles import render_app_bar


def render_top_bar() -> None:
    with st.container(key="tn-topbar"):
        brand_col, tabs_col, settings_col = st.columns([2.6, 8.5, 0.5], vertical_alignment="bottom")
        with brand_col:
            render_app_bar()
        with tabs_col:
            with st.container(key="tn-tabs"):
                columns = st.columns([1.3] * len(KIND_TABS) + [6], vertical_alignment="bottom")
                active = st.session_state["kind_filter"]
                for column, (value, label) in zip(columns, KIND_TABS.items()):
                    column.button(
                        label,
                        key=f"kind_tab_{value}",
                        type="primary" if value == active else "secondary",
                        use_container_width=True,
                        on_click=select_kind,
                        args=(value,),
                    )
        settings_col.button(
            ":material/settings:",
            key="open_settings",
            help="設定（ツールの管理・検出・動作設定・データ）",
            type="primary" if st.session_state["screen"] == SCREEN_SETTINGS else "secondary",
            use_container_width=True,
            on_click=go_to_settings,
        )


def render_side_nav(repository: ToolRepository) -> None:
    with st.container(key="tn-nav"):
        st.markdown("### 状態")
        st.radio(
            "状態",
            options=list(STATUS_FILTERS),
            key="status_filter",
            label_visibility="collapsed",
        )
        st.markdown("---")
        st.caption(f"登録 {repository.count()} 件")


def render_toolbar(repository: ToolRepository, settings: dict[str, str]) -> None:
    with st.container(key="tn-header"):
        search_col, bulk_col, detect_col, create_col = st.columns(
            [9, 0.5, 0.5, 1.8], vertical_alignment="center"
        )
    with search_col:
        st.text_input(
            "検索",
            key="search_query",
            placeholder="名前 / ディレクトリ / コマンド / 説明 / ポート",
            label_visibility="collapsed",
        )
    with bulk_col:
        if st.button(
            ":material/play_circle:",
            help="まとめて起動（「まとめて起動の対象」のツールを起動。起動中のものは飛ばします）",
            key="bulk_start",
            use_container_width=True,
        ):
            with st.spinner("まとめて起動しています…"):
                start_autostart_tools(repository, settings)
    with detect_col:
        if st.button(
            ":material/radar:",
            help="起動中のStreamlitを検出して登録",
            key="open_detect",
            use_container_width=True,
        ):
            open_dialog("detect")
    with create_col:
        if st.button("追加", icon=":material/add:", type="primary", use_container_width=True):
            open_dialog("create")
