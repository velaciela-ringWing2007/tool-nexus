"""設定画面（SPEC 8.2）: ツールの管理・検出・動作設定・データ."""

from __future__ import annotations

from datetime import datetime

import streamlit as st

from tool_nexus.core.backup import MODE_LABELS as BACKUP_MODE_LABELS
from tool_nexus.core.backup import MODE_REPLACE, BackupError, export_bytes, parse_backup, restore_backup
from tool_nexus.core.constants import DATABASE_PATH, KIND_LINK, TOOL_NEXUS_PORT, kind_label
from tool_nexus.core.models import ValidationError, normalize_group
from tool_nexus.core.repositories import ToolRepository
from tool_nexus.core.settings import SETTING_LABELS, SettingsError, validate_settings
from tool_nexus.ui.dialogs import render_detected_list
from tool_nexus.ui.state import (
    SETTINGS_BEHAVIOR,
    SETTINGS_DETECT,
    SETTINGS_SECTIONS,
    SETTINGS_TOOLS,
    flash,
    go_to_main,
    render_flash,
    request_check,
    select_settings_section,
)
from tool_nexus.ui.styles import render_note


def render_settings_screen(repository: ToolRepository, settings: dict[str, str]) -> None:
    render_flash()
    nav_col, main_col = st.columns([1.7, 8.3], gap="medium")
    with nav_col:
        with st.container(key="tn-nav-settings"):
            st.button(
                "一覧に戻る",
                icon=":material/arrow_back:",
                use_container_width=True,
                key="settings_back",
                on_click=go_to_main,
            )
            st.markdown("### 設定")
            for section in SETTINGS_SECTIONS:
                st.button(
                    section,
                    key=f"settings_nav_{section}",
                    use_container_width=True,
                    type="primary" if st.session_state["settings_section"] == section else "secondary",
                    on_click=select_settings_section,
                    args=(section,),
                )
    with main_col:
        section = st.session_state["settings_section"]
        st.subheader(section)
        if section == SETTINGS_TOOLS:
            render_tool_management(repository)
        elif section == SETTINGS_DETECT:
            st.caption("このPCで動いている、未登録のStreamlitを一覧します。「登録…」で値を入れた登録画面を開きます。")
            if st.button("検出する", icon=":material/radar:", key="settings_detect_run"):
                # 一覧内の「登録…」を押した次の実行でも一覧を出し続けるため、状態で持つ
                st.session_state["settings_detect_on"] = True
            if st.session_state.get("settings_detect_on"):
                render_detected_list(repository, key_prefix="settings_detect")
        elif section == SETTINGS_BEHAVIOR:
            render_behavior_settings(repository, settings)
        else:
            render_data_settings(repository)


def render_tool_management(repository: ToolRepository) -> None:
    """グループ・表示順・まとめて起動の一括編集と、まとめて削除。

    行は非表示のID列で突き合わせる（並べ替え後に位置で照合すると別の行に適用されるため）。
    """
    tools = repository.list_all()
    if not tools:
        render_note("ツールが登録されていません。")
        return
    st.caption("グループ・表示順・「まとめて起動」を表で編集できます。削除する行は「選択」にチェックしてください。")
    rows = [
        {
            "ID": tool.id,
            "選択": False,
            "名前": tool.name,
            "種別": kind_label(tool.kind),
            "グループ": tool.group_name,
            "ポート": str(tool.port) if tool.port else "",  # リンクなどポートの無いものは空欄
            "表示順": tool.sort_order,
            "まとめて起動": tool.autostart,
        }
        for tool in tools
    ]
    edited = st.data_editor(
        rows,
        key="manage_table",
        use_container_width=True,
        hide_index=True,
        num_rows="fixed",
        height=min(80 + 36 * len(rows), 560),
        disabled=["名前", "種別", "ポート"],
        column_config={
            "ID": None,
            "選択": st.column_config.CheckboxColumn("選択", width="small"),
            "名前": st.column_config.TextColumn("名前", width="large"),
            "グループ": st.column_config.TextColumn("グループ", width="medium", help="空欄は未分類"),
            "ポート": st.column_config.TextColumn("ポート", width="small"),
            "表示順": st.column_config.NumberColumn("表示順", step=1, format="%d", width="small"),
            "まとめて起動": st.column_config.CheckboxColumn("まとめて起動", width="small"),
        },
    )

    by_id = {tool.id: tool for tool in tools}
    changes: list[tuple[int, int, bool, str]] = []
    errors: list[str] = []
    for row in edited:
        tool = by_id.get(row.get("ID"))
        if tool is None:
            continue
        try:
            group = normalize_group(row.get("グループ"))
        except ValidationError as exc:
            errors.append(f"{tool.name}: {exc}")
            continue
        # まとめて起動はリンクには付けない（起動しないため）
        autostart = bool(row["まとめて起動"]) and tool.kind != KIND_LINK
        order = int(row["表示順"] or 0)
        if (order, autostart, group) != (tool.sort_order, tool.autostart, tool.group_name):
            changes.append((int(tool.id), order, autostart, group))
    for error in errors:
        st.error(error, icon="⛔")
    selected = [by_id[row["ID"]] for row in edited if row.get("選択") and row.get("ID") in by_id]

    save_col, _ = st.columns([1.5, 4])
    if save_col.button(
        f"変更を保存（{len(changes)}件）",
        type="primary",
        use_container_width=True,
        disabled=not changes,
        key="manage_save",
    ):
        repository.update_order(changes)
        flash(f"{len(changes)}件のグループ・表示順・まとめて起動を保存しました。")
        st.rerun()

    if selected:
        with st.container(border=True):
            st.warning(
                f"{len(selected)}件を削除します: " + "、".join(tool.name for tool in selected),
                icon="⚠️",
            )
            st.caption("起動中のツールは停止されません。この操作は取り消せません。")
            confirmed = st.checkbox("削除してよいことを確認しました", key="manage_delete_confirm")
            if st.button("選択したツールを削除", disabled=not confirmed, key="manage_delete"):
                deleted = repository.delete_many(tool.id for tool in selected)
                st.session_state.pop("manage_table", None)
                flash(f"{deleted}件を削除しました。")
                st.rerun()


def render_behavior_settings(repository: ToolRepository, settings: dict[str, str]) -> None:
    """動作設定。保存時に検証し、不正な値は保存しない（SPEC 8.2）。"""
    with st.form("behavior_form"):
        left, right = st.columns(2)
        with left:
            health_interval = st.text_input(
                SETTING_LABELS["health_interval"],
                value=settings["health_interval"],
                help="「60s」「2m」「90」の形式。5秒〜1時間。画面を開いている間だけ確認します。",
            )
            low = st.text_input(SETTING_LABELS["port_range_low"], value=settings["port_range_low"])
            reserved = st.text_input(
                SETTING_LABELS["reserved_ports"],
                value=settings["reserved_ports"],
                help=f"カンマ区切り。自動割当で使いません。TOOL NEXUS自身の {TOOL_NEXUS_PORT} は常に除外します。",
            )
        with right:
            timeout = st.text_input(
                SETTING_LABELS["health_timeout"], value=settings["health_timeout"], help="秒。0.2〜30。"
            )
            high = st.text_input(SETTING_LABELS["port_range_high"], value=settings["port_range_high"])
            log_dir = st.text_input(
                SETTING_LABELS["default_log_dir"],
                value=settings["default_log_dir"],
                help="空欄なら各ツールの作業ディレクトリに tool-nexus.log を作ります。絶対パスで指定します。",
            )
        st.caption("ポートの範囲を変えても、登録済みのツールのポートは変わりません（ブックマークを壊さないため）。")
        submitted = st.form_submit_button("保存", type="primary")

    if submitted:
        try:
            values = validate_settings(
                {
                    "health_interval": health_interval,
                    "health_timeout": timeout,
                    "port_range_low": low,
                    "port_range_high": high,
                    "reserved_ports": reserved,
                    "default_log_dir": log_dir,
                }
            )
        except SettingsError as exc:
            for key, message in exc.errors.items():
                st.error(f"{SETTING_LABELS.get(key, key)}: {message}", icon="⛔")
            return
        repository.set_settings(values)
        request_check()
        flash("動作設定を保存しました。")
        st.rerun()


def render_data_settings(repository: ToolRepository) -> None:
    """バックアップ（JSON）・復元・DBの場所（SPEC 7.3）。"""
    st.markdown("**データベースの場所**")
    st.code(str(DATABASE_PATH), language=None)

    st.markdown("**バックアップ**")
    st.caption("ツールの設定と動作設定をJSONで保存します。起動記録は含みません。")
    st.download_button(
        "JSONでバックアップ",
        icon=":material/download:",
        data=export_bytes(repository),
        file_name=f"tool-nexus-backup-{datetime.now():%Y%m%d-%H%M%S}.json",
        mime="application/json",
        key="backup_download",
    )

    st.markdown("**復元**")
    # 復元後にファイルを外すため、キーを変えて作り直す（ウィジェットの値は直接消せない）
    nonce = st.session_state.get("restore_nonce", 0)
    uploaded = st.file_uploader("バックアップ（JSON）", type=["json"], key=f"restore_file_{nonce}")
    if uploaded is None:
        return
    try:
        parsed = parse_backup(uploaded.getvalue())
    except BackupError as exc:
        st.error(str(exc), icon="⛔")
        return

    st.info(f"取り込めるツール: {len(parsed.tools)}件", icon="ℹ️")
    if parsed.problems:
        with st.expander(f"取り込めない行: {len(parsed.problems)}件", expanded=True):
            for problem in parsed.problems:
                st.write(f"- {problem}")
    if parsed.settings_problem:
        st.warning(f"動作設定は復元できません: {parsed.settings_problem}", icon="⚠️")

    mode = st.radio(
        "復元の方法",
        options=list(BACKUP_MODE_LABELS),
        format_func=lambda value: BACKUP_MODE_LABELS[value],
        key="restore_mode",
    )
    include_settings = st.checkbox(
        "動作設定も復元する", value=False, disabled=not parsed.settings, key="restore_settings"
    )
    confirmed = True
    if mode == MODE_REPLACE:
        confirmed = st.checkbox(
            f"登録済みの {repository.count()} 件をすべて削除して置き換えることを確認しました",
            key="restore_replace_confirm",
        )
    if st.button("復元する", type="primary", disabled=not confirmed, key="restore_submit"):
        summary = restore_backup(repository, parsed, mode=mode, include_settings=include_settings)
        message = f"復元しました: {len(summary.added)}件を追加"
        if summary.settings_restored:
            message += "、動作設定を復元"
        flash(message + "。")
        for skipped in summary.skipped:
            flash(f"飛ばしました: {skipped}", "warning")
        st.session_state["restore_nonce"] = nonce + 1
        request_check()
        st.rerun()
