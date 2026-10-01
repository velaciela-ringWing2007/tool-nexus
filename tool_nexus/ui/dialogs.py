"""登録・編集・ログ・ポートからの停止・検出のダイアログ."""

from __future__ import annotations

from typing import Any

import streamlit as st

from tool_nexus import osdep
from tool_nexus.core.constants import (
    HEALTH_MODE_VALUES,
    KIND_EXE,
    KIND_LINK,
    KIND_SCRIPT,
    KIND_STREAMLIT,
    KIND_VALUES,
    KIND_WEB,
    LOG_TAIL_BYTES,
    LOG_TAIL_LINES,
    TOOL_NEXUS_PORT,
    UNGROUPED_LABEL,
    health_mode_label,
    kind_label,
)
from tool_nexus.core.database import DatabaseError
from tool_nexus.core.models import (
    Tool,
    ValidationError,
    build_tool,
    can_auto_assign_port,
    default_health_mode,
)
from tool_nexus.core.ports import PortError, assign_port
from tool_nexus.core.repositories import DuplicatePortError, ToolRepository
from tool_nexus.process.control import (
    DetectedTool,
    LaunchError,
    ProcessInfo,
    ProcessNotIdentifiedError,
    ProcessQueryError,
    StopError,
    build_argv,
    detect_streamlit,
    find_port_owner,
    read_log_tail,
    stop,
    take_snapshot,
)
from tool_nexus.process.launch_assist import (
    BACKGROUND_SCRIPT_NOTE,
    BYPASS_OPTION,
    AssistError,
    has_bypass,
    is_powershell_command,
    pick_file,
    pick_folder,
    quote,
    set_bypass,
    static_server_command,
    suggest_from_file,
    suggest_stop_command,
)
from tool_nexus.ui.actions import log_path_for, mark_stopped, report_stopped
from tool_nexus.ui.state import close_dialog, flash, format_time, logger, open_dialog, request_check


def on_kind_change() -> None:
    """種別を変えたら死活監視モードを種別の既定値にする（exe → process）。

    web にしたとき起動コマンドが空なら、静的ファイルサーバーのコマンドを入れる（SPEC 6.1）。
    """
    kind = st.session_state["form_kind"]
    st.session_state["form_health_mode"] = default_health_mode(kind)
    st.session_state["form_notes"] = []  # 前の種別の案内は消す
    if kind == KIND_SCRIPT:
        st.session_state["form_notes"] = [("info", BACKGROUND_SCRIPT_NOTE)]
    if kind == KIND_WEB and not str(st.session_state.get("form_command", "")).strip():
        st.session_state["form_command"] = static_server_command()
        st.session_state["form_notes"] = [
            ("info", "標準ライブラリの http.server で作業ディレクトリのフォルダを配信するコマンドを入れました。"
                     "Flask などを登録する場合は書き換えてください。")
        ]


def on_pick_file() -> None:
    """ファイルを選ばせ、起動方式を推測してフォームに入れる（SPEC 6.9）。

    ボタンの on_click で呼ぶ（ウィジェット生成前に session_state を書き換えるため）。
    推測は入力欄に入れるだけで、保存はユーザーが確認してから行う。
    """
    try:
        path = pick_file(
            st.session_state.get("form_directory") or st.session_state.get("form_target") or None
        )
        if path is None:
            return
        suggestion = suggest_from_file(path)
    except AssistError as exc:
        st.session_state["form_notes"] = [("error", str(exc))]
        return

    if not str(st.session_state.get("form_name", "")).strip():
        st.session_state["form_name"] = suggestion.name
    st.session_state["form_directory"] = suggestion.directory
    st.session_state["form_command"] = suggestion.command
    st.session_state["form_target"] = suggestion.target
    st.session_state["form_kind"] = suggestion.kind
    st.session_state["form_health_mode"] = suggestion.health_mode
    st.session_state["form_stop_command"] = suggestion.stop_command
    check = "内容" if suggestion.kind == KIND_LINK else "下の「実行されるコマンド」"
    st.session_state["form_notes"] = [("info", note) for note in suggestion.notes] + [
        ("info", f"推測した内容です。{check}を確認してから登録してください。")
    ]


def on_pick_stop_file() -> None:
    """停止コマンドに使うファイルを選ばせ、停止コマンドを入れる（SPEC 6.9）。"""
    directory = str(st.session_state.get("form_directory") or "")
    try:
        path = pick_file(directory or None)
        if path is None:
            return
        command, notes = suggest_stop_command(path, directory)
    except AssistError as exc:
        st.session_state["form_notes"] = [("error", str(exc))]
        return
    # 実行ポリシーのチェックが入っていれば、停止コマンドにも合わせる
    if has_bypass(str(st.session_state.get("form_command") or "")):
        command = set_bypass(command, True)
    st.session_state["form_stop_command"] = command
    st.session_state["form_notes"] = [("info", note) for note in notes] + [
        ("info", "停止コマンドを入れました。下の「停止時に実行されるコマンド」を確認してから保存してください。")
    ]


def on_bypass_change() -> None:
    """実行ポリシーのチェックに合わせて、起動コマンド・停止コマンドの -ExecutionPolicy Bypass を付け外しする。"""
    enabled = bool(st.session_state["form_ps_bypass"])
    for key in ("form_command", "form_stop_command"):
        st.session_state[key] = set_bypass(str(st.session_state.get(key) or ""), enabled)


def render_bypass_option() -> None:
    """PowerShell のコマンドがあるときだけ、実行ポリシーのチェックを出す（SPEC 6.1）。

    チェックの状態は保存しない。コマンドに -ExecutionPolicy Bypass があるかで決める（コマンドが正）。
    """
    commands = [str(st.session_state.get(key) or "") for key in ("form_command", "form_stop_command")]
    if not any(is_powershell_command(command) for command in commands):
        return
    st.session_state["form_ps_bypass"] = any(has_bypass(command) for command in commands)
    st.checkbox(
        f"スクリプトの実行ポリシーを無視する（{BYPASS_OPTION}）",
        key="form_ps_bypass",
        on_change=on_bypass_change,
        help="PCの実行ポリシーで .ps1 が止められる場合に使います。チェックすると起動コマンド・停止コマンドに "
        f"{BYPASS_OPTION} を足します。会社のグループポリシーで決められている場合は効きません。",
    )


def on_pick_folder() -> None:
    try:
        path = pick_folder(st.session_state.get("form_directory") or None)
    except AssistError as exc:
        st.session_state["form_notes"] = [("error", str(exc))]
        return
    if path is not None:
        st.session_state["form_directory"] = str(path)


def on_pick_target_folder() -> None:
    """リンク先にフォルダを選ぶ（フォルダの一覧や index.html を配信する）。"""
    try:
        path = pick_folder(st.session_state.get("form_target") or None)
    except AssistError as exc:
        st.session_state["form_notes"] = [("error", str(exc))]
        return
    if path is not None:
        st.session_state["form_target"] = str(path)
        if not str(st.session_state.get("form_name", "")).strip():
            st.session_state["form_name"] = path.name


def render_link_fields() -> None:
    """種別 link の入力欄（リンク先だけ。起動しないので作業ディレクトリやコマンドは無い）。"""
    target_col, button_col = st.columns([8, 1.2], vertical_alignment="bottom")
    target_col.text_input(
        "リンク先 *",
        key="form_target",
        placeholder=r"https://example.com/  または  C:\dev\tool-a\docs\index.html",
        help="URL（http / https）か、ローカルのファイル／フォルダの絶対パス。"
        "ローカルは TOOL NEXUS が http://127.0.0.1:8498/links/... として配信します（同じフォルダのCSS・画像も表示されます）。",
    )
    button_col.button(
        ":material/folder:",
        help="フォルダを選ぶ（フォルダの一覧や index.html を開くリンクにする）",
        use_container_width=True,
        on_click=on_pick_target_folder,
        key="form_pick_target_folder",
    )
    st.number_input("表示順", key="form_sort_order", step=1)
    st.text_area("説明", key="form_description", height=70)


def render_command_preview(values: dict[str, Any]) -> None:
    """実行されるコマンドを表示する（{port} の置き換えと --server.* の付与を反映）。停止コマンドも同様。"""
    command = str(values["command"] or "").strip()
    port_text = str(values["port"] or "").strip()
    auto = bool(command) and not port_text and can_auto_assign_port(values["kind"], command)
    port = port_text or ("<保存時に割当>" if auto else None)
    previews = [("実行されるコマンド", command, values["kind"])]
    previews.append(("停止時に実行されるコマンド", str(values.get("stop_command") or "").strip(), KIND_EXE))
    for title, text, kind in previews:
        if not text:
            continue
        try:
            argv = build_argv(text, kind=kind, port=port)
        except LaunchError as exc:
            st.caption(f"{title}: {exc}")
            continue
        st.caption(f"{title}（作業ディレクトリで実行）")
        st.code(" ".join(quote(arg) for arg in argv), language=None, wrap_lines=True)


def render_group_field(groups: list[str]) -> None:
    """グループ: 既存の名前から選ぶか、新しい名前を入力する（SPEC 6.11）。空は未分類。"""
    current = str(st.session_state.get("form_group") or "")
    options = ["", *groups] + ([current] if current and current not in groups else [])
    st.selectbox(
        "グループ",
        options=options,
        format_func=lambda value: value or f"（{UNGROUPED_LABEL}）",
        key="form_group",
        accept_new_options=True,
        help="既存のグループから選ぶか、新しい名前を入力します。グループ単位でまとめて起動・停止できます。",
    )


def render_tool_form(groups: list[str]) -> dict[str, Any]:
    pick_col, note_col = st.columns([1.6, 5], vertical_alignment="center")
    pick_col.button(
        "ファイルから入力",
        icon=":material/folder_open:",
        use_container_width=True,
        on_click=on_pick_file,
        key="form_pick_file",
        help=f"{osdep.EXECUTABLE_LABEL} を選ぶと、作業ディレクトリ・venv・種別・コマンドを推測して入力します。"
        "HTML / PDF はリンクとして登録します。ダイアログはこのPCの画面に開きます。",
    )
    note_col.caption(f"{osdep.EXECUTABLE_LABEL} を選ぶと推測して入力します（HTML / PDF はリンクになります）。")
    for level, note in st.session_state.get("form_notes", []):
        if level == "error":
            st.error(note, icon="⛔")
        else:
            st.info(note, icon="ℹ️")

    name_col, group_col = st.columns([3, 2])
    name_col.text_input("名前 *", key="form_name", placeholder="在庫チェッカー")
    with group_col:
        render_group_field(groups)
    left, right = st.columns(2)
    with left:
        st.selectbox(
            "種別 *",
            options=list(KIND_VALUES),
            format_func=kind_label,
            key="form_kind",
            on_change=on_kind_change,
        )
    if st.session_state["form_kind"] == KIND_LINK:
        render_link_fields()
        return {
            "name": st.session_state["form_name"],
            "kind": KIND_LINK,
            "directory": "",
            "command": "",
            "target": st.session_state["form_target"],
            "group_name": st.session_state["form_group"] or "",
            "description": st.session_state["form_description"],
            "sort_order": st.session_state["form_sort_order"],
        }
    with right:
        st.selectbox(
            "死活監視 *",
            options=list(HEALTH_MODE_VALUES),
            format_func=health_mode_label,
            key="form_health_mode",
            help="HTTP: ポートに応答するか / プロセス: 起動したプロセスが生きているか",
        )
    dir_col, dir_button_col = st.columns([8, 1.2], vertical_alignment="bottom")
    dir_col.text_input("作業ディレクトリ *", key="form_directory", placeholder=r"C:\dev\tool-a")
    dir_button_col.button(
        ":material/folder:",
        help="フォルダを選ぶ",
        use_container_width=True,
        on_click=on_pick_folder,
        key="form_pick_folder",
    )
    st.text_input(
        "起動コマンド *",
        key="form_command",
        placeholder=r".venv\Scripts\python.exe -m streamlit run app.py",
        help="Streamlitの場合、--server.port / --server.address / --server.headless は自動で付与します。"
        "空白を含む値は --name \"a b\" のように別に書いてください。",
    )
    stop_col, stop_button_col = st.columns([8, 1.2], vertical_alignment="bottom")
    stop_col.text_input(
        "停止コマンド",
        key="form_stop_command",
        placeholder="docker compose down　/　powershell -NoProfile -File stop.ps1",
        help="登録すると「停止」でこれを作業ディレクトリで実行します（最大60秒待ちます）。"
        "その後、TOOL NEXUS が起動したプロセスが残っていれば止めます。空欄なら今までどおりプロセスを止めます。",
    )
    stop_button_col.button(
        ":material/folder_open:",
        help="停止に使うファイル（.ps1 / .sh / 実行ファイル / .py）を選ぶ",
        use_container_width=True,
        on_click=on_pick_stop_file,
        key="form_pick_stop_file",
    )
    render_bypass_option()
    left, right = st.columns(2)
    with left:
        st.text_input(
            "ポート",
            key="form_port",
            placeholder="空欄なら自動割当",
            help="Streamlit、または起動コマンドに {port} を含む場合は、空欄なら保存時に空きポートを割り当てます。"
            "編集で空欄にすると振り直します（既存のリンクは切れます）。",
        )
    with right:
        st.number_input("表示順", key="form_sort_order", step=1)
    st.text_input(
        "ログ出力先",
        key="form_log_path",
        placeholder="空欄なら作業ディレクトリ配下の tool-nexus.log",
    )
    st.checkbox("まとめて起動の対象にする", key="form_autostart")
    st.text_area("説明", key="form_description", height=70)
    values = {
        "name": st.session_state["form_name"],
        "kind": st.session_state["form_kind"],
        "directory": st.session_state["form_directory"],
        "command": st.session_state["form_command"],
        "port": st.session_state["form_port"],
        "health_mode": st.session_state["form_health_mode"],
        "log_path": st.session_state["form_log_path"],
        "autostart": st.session_state["form_autostart"],
        "description": st.session_state["form_description"],
        "sort_order": st.session_state["form_sort_order"],
        "group_name": st.session_state["form_group"] or "",
        "stop_command": st.session_state["form_stop_command"],
    }
    render_command_preview(values)
    return values


def save_tool(
    repository: ToolRepository,
    settings: dict[str, str],
    values: dict[str, Any],
    tool_id: int | None,
) -> tuple[Tool, int | None] | None:
    """自動割当 → 検証 → 保存の順に行い、(保存したツール, 自動割当したポート) を返す。

    ポートの自動割当は起動時ではなく保存時に行い、DBに残す
    （起動のたびに変わるとブックマークやLIST NEXUSのリンクが壊れるため）。
    失敗したらダイアログ内にエラーを出して None を返す。
    """
    values = dict(values)
    assigned: int | None = None
    try:
        if not str(values.get("port") or "").strip() and can_auto_assign_port(
            str(values.get("kind") or ""), str(values.get("command") or "")
        ):
            assigned = assign_port(settings, repository.used_ports())
            values["port"] = assigned
        tool = build_tool(id=tool_id, **values)
        saved = repository.update(tool) if tool_id else repository.create(tool)
        return saved, assigned
    except PortError as exc:
        st.error(f"ポートを自動で割り当てられませんでした。{exc}", icon="⛔")
    except ValidationError as exc:
        st.error(str(exc), icon="⛔")
    except DuplicatePortError as exc:
        st.error(str(exc), icon="⛔")
    except DatabaseError as exc:
        logger.exception("ツールの保存に失敗しました")
        st.error(str(exc), icon="⛔")
    return None


@st.dialog("ツールを追加", width="large", on_dismiss=close_dialog)
def create_dialog(repository: ToolRepository, settings: dict[str, str]) -> None:
    values = render_tool_form(repository.group_names())
    save_col, cancel_col = st.columns(2)
    if save_col.button("登録", type="primary", use_container_width=True, key="create_submit"):
        result = save_tool(repository, settings, values, None)
        if result is not None:
            created, assigned = result
            suffix = f"ポート {assigned} を割り当てました。" if assigned else ""
            flash(f"「{created.name}」を登録しました。{suffix}")
            close_dialog()
            st.rerun()
    if cancel_col.button("キャンセル", use_container_width=True, key="create_cancel"):
        close_dialog()
        st.rerun()


def get_target(repository: ToolRepository) -> Tool | None:
    target_id = st.session_state.get("target_id")
    return repository.get_by_id(int(target_id)) if target_id is not None else None


def render_missing_target() -> None:
    st.error("対象のツールが見つかりませんでした。", icon="⛔")
    if st.button("閉じる", key="missing_close"):
        close_dialog()
        st.rerun()


@st.dialog("ツールを編集", width="large", on_dismiss=close_dialog)
def edit_dialog(repository: ToolRepository, settings: dict[str, str]) -> None:
    target = get_target(repository)
    if target is None:
        render_missing_target()
        return

    values = render_tool_form(repository.group_names())
    save_col, cancel_col = st.columns(2)
    if save_col.button("更新", type="primary", use_container_width=True, key="edit_submit"):
        result = save_tool(repository, settings, values, target.id)
        if result is not None:
            updated, assigned = result
            flash(f"「{updated.name}」を更新しました。")
            if assigned and target.port and assigned != target.port:
                flash(
                    f"ポートを {target.port} から {assigned} に振り直しました。"
                    "既存のブックマークやLIST NEXUSに登録したリンクは切れるため、更新してください。",
                    "warning",
                )
            close_dialog()
            st.rerun()
    if cancel_col.button("キャンセル", use_container_width=True, key="edit_cancel"):
        close_dialog()
        st.rerun()

    # 削除は誤操作を避けるため行には置かず、編集画面の奥に置く（行のウィジェットを3個に抑える意味もある）。
    with st.expander("このツールを削除"):
        st.caption("登録を削除します。起動中のツールは停止されません。この操作は取り消せません。")
        st.checkbox(f"「{target.name}」を削除する", key="delete_confirmed")
        if st.button(
            "削除する",
            key="delete_submit",
            disabled=not st.session_state.get("delete_confirmed"),
        ):
            try:
                repository.delete(int(target.id))
            except DatabaseError as exc:
                logger.exception("ツールの削除に失敗しました")
                st.error(str(exc), icon="⛔")
            else:
                flash(f"「{target.name}」を削除しました。")
                close_dialog()
                st.rerun()


@st.dialog("ログ", width="large", on_dismiss=close_dialog)
def log_dialog(repository: ToolRepository, settings: dict[str, str]) -> None:
    target = get_target(repository)
    if target is None:
        render_missing_target()
        return

    path = log_path_for(target, settings)
    st.text(target.name)
    st.code(str(path), language=None)  # 標準のコピーボタンでパスをコピーできる
    try:
        text = read_log_tail(path, max_lines=LOG_TAIL_LINES, max_bytes=LOG_TAIL_BYTES)
    except OSError as exc:
        st.error(f"ログを読み込めませんでした: {exc}", icon="⛔")
        text = None
    if text is None:
        st.info("ログはまだありません。", icon="ℹ️")
    else:
        st.caption(f"末尾 {LOG_TAIL_LINES} 行まで")
        st.code(text or "（空）", language=None, height=420)

    reload_col, close_col = st.columns(2)
    reload_col.button("再読み込み", use_container_width=True, key="log_reload")
    if close_col.button("閉じる", use_container_width=True, key="log_close"):
        close_dialog()
        st.rerun()


def render_process_card(process: ProcessInfo, port: int) -> None:
    started = format_time(process.created_at)
    st.markdown(
        f"ポート **{port}** ／ PID **{process.pid}**（{process.name}）"
        + (f" ／ 起動 {started}" if started else "")
    )
    st.code(process.command_line or "（コマンドラインを取得できませんでした）", language=None, wrap_lines=True)


@st.dialog("ポートから特定して停止", width="large", on_dismiss=close_dialog)
def port_stop_dialog(repository: ToolRepository, settings: dict[str, str]) -> None:
    """記録から特定できないツールを、ポートでLISTENしているプロセスから引き直して停止する。

    強制終了なので、対象を画面に出してユーザーの確認を取ってから停止する（SPEC 6.3）。
    """
    target = get_target(repository)
    if target is None or not target.port:
        render_missing_target()
        return

    st.warning(
        f"「{target.name}」は起動記録から対象プロセスを特定できませんでした"
        "（TOOL NEXUSの外で起動された、または記録が古い）。"
        f"ポート {target.port} で待ち受けているプロセスを探しました。",
        icon="⚠️",
    )
    try:
        owner = find_port_owner(take_snapshot(), target.port)
    except ProcessQueryError as exc:
        st.error(f"プロセス情報を取得できませんでした。{exc}", icon="⛔")
        owner = None

    if owner is None:
        st.info(f"ポート {target.port} で待ち受けているプロセスは見つかりませんでした。既に停止しています。")
        if st.button("閉じる", use_container_width=True, key="port_stop_close"):
            close_dialog()
            request_check()
            st.rerun()
        return

    render_process_card(owner, target.port)
    st.caption("このプロセスと子プロセスを強制終了します（ツール側の終了処理は走りません）。")
    stop_col, cancel_col = st.columns(2)
    if stop_col.button("停止する", type="primary", use_container_width=True, key="port_stop_submit"):
        try:
            # 直前に取得した起動時刻で照合してから停止する（取得後にPIDが入れ替わっていれば止めない）
            stop(owner.pid, owner.created_at)
        except ProcessNotIdentifiedError:
            flash("対象のプロセスが入れ替わったため停止しませんでした。もう一度お試しください。", "error")
        except StopError as exc:
            flash(f"「{target.name}」を停止できませんでした。{exc}", "error")
        else:
            mark_stopped(repository, settings, target)
            report_stopped(target)
        close_dialog()
        request_check()
        st.rerun()
    if cancel_col.button("キャンセル", use_container_width=True, key="port_stop_cancel"):
        close_dialog()
        st.rerun()


def register_detected(detected: DetectedTool) -> None:
    open_dialog(
        "create",
        overrides={
            "form_name": detected.name,
            "form_kind": KIND_STREAMLIT,
            "form_directory": detected.directory,
            "form_command": detected.command,
            "form_port": str(detected.port),
            "form_health_mode": default_health_mode(KIND_STREAMLIT),
        },
    )


@st.dialog("起動中のStreamlitを検出", width="large", on_dismiss=close_dialog)
def detect_dialog(repository: ToolRepository) -> None:
    render_detected_list(repository, key_prefix="detect")
    if st.button("閉じる", use_container_width=True, key="detect_close"):
        close_dialog()
        st.rerun()


def render_detected_list(repository: ToolRepository, *, key_prefix: str) -> None:
    """このPCで動いている未登録の Streamlit を一覧し、登録フォームを開く（SPEC 6.6）。

    検出ダイアログと設定画面の「検出」で共用する。
    """
    try:
        with st.spinner("プロセスを調べています…"):
            found = detect_streamlit(
                take_snapshot(), exclude_ports=repository.used_ports() | {TOOL_NEXUS_PORT}
            )
    except ProcessQueryError as exc:
        st.error(f"プロセス情報を取得できませんでした。{exc}", icon="⛔")
        found = []

    if not found:
        st.info("未登録の起動中Streamlitは見つかりませんでした。", icon="ℹ️")
    for index, detected in enumerate(found):
        with st.container(border=True):
            render_process_card(detected.process, detected.port)
            info_col, button_col = st.columns([5, 1.3], vertical_alignment="center")
            info_col.caption(
                f"作業ディレクトリ: {detected.directory}"
                if detected.directory
                else "作業ディレクトリを推測できませんでした。登録画面で入力してください。"
            )
            if button_col.button("登録…", key=f"{key_prefix}_register_{index}", use_container_width=True):
                register_detected(detected)


def render_dialogs(repository: ToolRepository, settings: dict[str, str]) -> None:
    dialog = st.session_state.get("dialog")
    if dialog == "create":
        create_dialog(repository, settings)
    elif dialog == "edit":
        edit_dialog(repository, settings)
    elif dialog == "log":
        log_dialog(repository, settings)
    elif dialog == "port_stop":
        port_stop_dialog(repository, settings)
    elif dialog == "detect":
        detect_dialog(repository)
