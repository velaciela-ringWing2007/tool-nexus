"""session_state・通知・ダイアログの開閉・画面の切り替え."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

import streamlit as st

from tool_nexus.core.constants import DATABASE_PATH, KIND_LABELS, KIND_STREAMLIT
from tool_nexus.core.models import Tool, default_health_mode
from tool_nexus.core.repositories import ToolRepository
from tool_nexus.process.health import Status
from tool_nexus.ui.grouping import SORT_MANUAL

logger = logging.getLogger("tool_nexus")

# 上部バーの種別タブ
KIND_ALL = "all"
KIND_TABS: dict[str, str] = {KIND_ALL: "すべて"} | KIND_LABELS

# 左ナビの状態フィルター
FILTER_ALL = "すべて"
FILTER_RUNNING = "起動中"
FILTER_STOPPED = "停止中"
FILTER_ATTENTION = "要確認"
STATUS_FILTERS: dict[str, frozenset[Status]] = {
    FILTER_ALL: frozenset(Status),
    FILTER_RUNNING: frozenset({Status.RUNNING, Status.STARTING}),
    FILTER_STOPPED: frozenset({Status.STOPPED}),
    FILTER_ATTENTION: frozenset({Status.FAILED, Status.UNKNOWN, Status.MISSING}),
}

# メッセージを表示し続ける秒数。エラーは読み逃さないよう長めにする。
FLASH_SECONDS: dict[str, float] = {"success": 6.0, "warning": 30.0, "error": 30.0}
FORM_KEYS: dict[str, Any] = {
    "form_name": "",
    "form_kind": KIND_STREAMLIT,
    "form_directory": "",
    "form_command": "",
    "form_port": "",
    "form_health_mode": default_health_mode(KIND_STREAMLIT),
    "form_log_path": "",
    "form_autostart": False,
    "form_description": "",
    "form_sort_order": 0,
    "form_target": "",
    "form_group": "",
}

# 画面
SCREEN_MAIN = "main"
SCREEN_SETTINGS = "settings"

# 設定画面のセクション
SETTINGS_TOOLS = "ツールの管理"
SETTINGS_DETECT = "検出"
SETTINGS_BEHAVIOR = "動作設定"
SETTINGS_DATA = "データ"
SETTINGS_SECTIONS: tuple[str, ...] = (SETTINGS_TOOLS, SETTINGS_DETECT, SETTINGS_BEHAVIOR, SETTINGS_DATA)
DEFAULT_STATE: dict[str, Any] = {
    "screen": SCREEN_MAIN,
    "settings_section": SETTINGS_TOOLS,
    "kind_filter": KIND_ALL,
    "status_filter": FILTER_ALL,
    "search_query": "",
    "dialog": None,
    "target_id": None,
    "health_cache": {},
    "health_checked_at": None,
    "health_checked_label": "",
    "force_check": False,
    "flash": [],
    "sort_key": SORT_MANUAL,
    "collapsed_groups": [],
}


def get_repository() -> ToolRepository:
    """リポジトリを取得する。初回のみDBを初期化する。"""
    repository = ToolRepository(DATABASE_PATH)
    if not st.session_state.get("db_ready"):
        repository.initialize()
        st.session_state["db_ready"] = True
    return repository


def init_state() -> None:
    for key, value in DEFAULT_STATE.items():
        # list / dict は複製する（同じオブジェクトを全セッションで共有しないように）
        st.session_state.setdefault(key, value.copy() if isinstance(value, (list, dict)) else value)


def toggle_group(group: str) -> None:
    """グループの開閉を切り替える（SPEC 8.1）。"""
    collapsed: list[str] = st.session_state["collapsed_groups"]
    if group in collapsed:
        collapsed.remove(group)
    else:
        collapsed.append(group)


def flash(message: str, level: str = "success") -> None:
    """メッセージを積む。一覧は数秒ごとに再描画されるため、表示し続ける期限を持たせる。"""
    expires_at = time.monotonic() + FLASH_SECONDS.get(level, FLASH_SECONDS["success"])
    st.session_state["flash"].append((level, message, expires_at))


def render_flash() -> None:
    now = time.monotonic()
    messages = [m for m in st.session_state.get("flash", []) if m[2] > now]
    for level, message, _ in messages:
        if level == "error":
            st.error(message, icon="⛔")
        elif level == "warning":
            st.warning(message, icon="⚠️")
        else:
            st.success(message, icon="✅")
    st.session_state["flash"] = messages


def health_timeout(settings: dict[str, str]) -> float:
    try:
        return max(0.1, float(settings["health_timeout"]))
    except (KeyError, ValueError):
        return 2.0


def prime_form(tool: Tool | None = None, overrides: dict[str, Any] | None = None) -> None:
    """登録・編集フォームの初期値を session_state へ設定する（ウィジェット生成前に呼ぶ）。

    overrides は検出結果などから初期値を入れる場合に使う（キーは FORM_KEYS と同じ）。
    """
    values = dict(FORM_KEYS)
    if tool is not None:
        values.update(
            {
                "form_name": tool.name,
                "form_kind": tool.kind,
                "form_directory": tool.directory,
                "form_command": tool.command,
                "form_port": "" if tool.port is None else str(tool.port),
                "form_health_mode": tool.health_mode,
                "form_log_path": tool.log_path,
                "form_autostart": tool.autostart,
                "form_description": tool.description,
                "form_sort_order": tool.sort_order,
                "form_target": tool.target,
                "form_group": tool.group_name,
            }
        )
    values.update(overrides or {})
    for key, value in values.items():
        st.session_state[key] = value
    st.session_state["delete_confirmed"] = False
    st.session_state["form_notes"] = []


def open_dialog(
    name: str, tool: Tool | None = None, overrides: dict[str, Any] | None = None
) -> None:
    """ダイアログを開く。

    一覧はフラグメント内にあり、ボタンを押してもフラグメントしか再実行されない。
    ダイアログはアプリ全体の描画で開くため、アプリ全体を再実行する。
    """
    if name in ("create", "edit"):
        prime_form(tool, overrides)
    st.session_state["dialog"] = name
    st.session_state["target_id"] = tool.id if tool else None
    st.rerun(scope="app")


def close_dialog() -> None:
    st.session_state["dialog"] = None
    st.session_state["target_id"] = None


def format_time(value: str | None) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


def request_check() -> None:
    """次の描画で必ずヘルスチェックを行う（再チェック・起動・停止の直後）。"""
    st.session_state["force_check"] = True


def go_to_settings() -> None:
    st.session_state["screen"] = SCREEN_SETTINGS


def go_to_main() -> None:
    st.session_state["screen"] = SCREEN_MAIN


def select_settings_section(section: str) -> None:
    st.session_state["settings_section"] = section


def select_kind(kind: str) -> None:
    st.session_state["kind_filter"] = kind
    st.session_state["screen"] = SCREEN_MAIN
