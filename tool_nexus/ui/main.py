"""画面全体の組み立て.

画面構造は LIST NEXUS と同じ（全幅の上部バー＋左ナビと本文の2列）。
st.sidebar は使わない（上部バーを全幅にできなくなるため）。
"""

from __future__ import annotations

import streamlit as st

from tool_nexus.core.constants import APP_ICON, APP_NAME
from tool_nexus.core.database import DatabaseError
from tool_nexus.process.link_server import ensure_server as ensure_link_server
from tool_nexus.ui.dialogs import render_dialogs
from tool_nexus.ui.layout import render_side_nav, render_toolbar, render_top_bar
from tool_nexus.ui.log_filters import install as install_log_filters
from tool_nexus.ui.settings_view import render_settings_screen
from tool_nexus.ui.state import SCREEN_SETTINGS, get_repository, init_state, logger
from tool_nexus.ui.styles import apply_styles
from tool_nexus.ui.tool_list import render_tool_list


def main() -> None:
    st.set_page_config(
        page_title=APP_NAME,
        page_icon=APP_ICON,
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    apply_styles()
    init_state()
    # ブラウザを閉じたときの接続リセット（Windows の asyncio）のトレースバックを出さない
    install_log_filters()

    try:
        repository = get_repository()
        settings = repository.get_settings()
    except DatabaseError as exc:
        logger.exception("データベースの初期化に失敗しました")
        st.error(str(exc), icon="⛔")
        st.stop()
        return

    # ローカルのリンクを配信するサーバー（プロセス内で1回だけ起動。SPEC 6.10）
    server_error = ensure_link_server(repository.get_by_id)

    render_top_bar()
    if server_error:
        st.warning(f"{server_error} ローカルのファイルのリンクは開けません。", icon="⚠️")
    try:
        if st.session_state["screen"] == SCREEN_SETTINGS:
            render_settings_screen(repository, settings)
        else:
            nav_col, main_col = st.columns([1.7, 8.3], gap="medium")
            with nav_col:
                render_side_nav(repository)
            with main_col:
                render_toolbar(repository, settings)
                render_tool_list(repository, settings)
        render_dialogs(repository, settings)
    except DatabaseError as exc:
        logger.exception("データベース操作に失敗しました")
        st.error(str(exc), icon="⛔")
