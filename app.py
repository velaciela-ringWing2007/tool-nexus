"""TOOL NEXUS - ローカルツールのランチャー（Streamlit UI）.

UIはこのモジュールに閉じ込め、SQL・プロセス操作・死活監視は
repositories / process_utils / health に委譲する。

画面構造は LIST NEXUS と同じ（全幅の上部バー＋左ナビと本文の2列）。
st.sidebar は使わない（上部バーを全幅にできなくなるため）。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import streamlit as st

import platform_ops
from constants import (
    APP_ICON,
    APP_NAME,
    DATABASE_PATH,
    HEALTH_MODE_VALUES,
    KIND_LABELS,
    KIND_STREAMLIT,
    KIND_VALUES,
    LOG_TAIL_BYTES,
    LOG_TAIL_LINES,
    TOOL_NEXUS_PORT,
    health_mode_label,
    kind_label,
)
from backup import MODE_LABELS as BACKUP_MODE_LABELS
from backup import MODE_REPLACE, BackupError, export_bytes, parse_backup, restore_backup
from database import DatabaseError
from health import (
    Status,
    ToolHealth,
    derive_status,
    is_check_due,
    parse_interval,
    probe_all,
)
from models import Tool, ValidationError, build_tool, can_auto_assign_port, default_health_mode
from port_utils import PortError, assign_port, is_port_free
from launch_assist import AssistError, pick_file, pick_folder, quote, suggest_from_file
from process_utils import (
    DetectedTool,
    ProcessInfo,
    ProcessQueryError,
    build_argv,
    detect_streamlit,
    find_port_owner,
    take_snapshot,
    LaunchError,
    PidStatus,
    ProcessNotIdentifiedError,
    StopError,
    launch,
    read_log_tail,
    resolve_log_path,
    stop,
)
from repositories import DuplicatePortError, ToolRepository
from settings_utils import SETTING_LABELS, SettingsError, validate_settings
from styles import (
    apply_styles,
    escape_html,
    render_app_bar,
    render_note,
    render_port_link,
    render_summary,
    render_tool_summary,
)

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
    FILTER_ATTENTION: frozenset({Status.FAILED, Status.UNKNOWN}),
}

# 一覧フラグメントの再描画の間隔。ヘルスチェック自体は設定の間隔（既定60秒）ごとにだけ行い、
# それ以外の再描画では前回の結果を使う（health.is_check_due）。
# 起動中…のツールがある間は毎回チェックするため、ヘルスが通れば最長でもこの間隔で表示が切り替わる。
# run_every をその場で切り替える方式（フラグメントの外から登録し直す）は、
# 前回の描画が消えずに残ったため採用しない（実機で確認）。
LIST_TICK = "3s"

# メッセージを表示し続ける秒数。エラーは読み逃さないよう長めにする。
FLASH_SECONDS: dict[str, float] = {"success": 6.0, "warning": 30.0, "error": 30.0}

# 停止後にポートの解放を確認する時間
STOP_RELEASE_TIMEOUT = 5.0

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
}


# ----------------------------------------------------------------------
# 初期化
# ----------------------------------------------------------------------
def get_repository() -> ToolRepository:
    """リポジトリを取得する。初回のみDBを初期化する。"""
    repository = ToolRepository(DATABASE_PATH)
    if not st.session_state.get("db_ready"):
        repository.initialize()
        st.session_state["db_ready"] = True
    return repository


def init_state() -> None:
    for key, value in DEFAULT_STATE.items():
        st.session_state.setdefault(key, value.copy() if isinstance(value, list) else value)


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


# ----------------------------------------------------------------------
# 起動・停止
# ----------------------------------------------------------------------
def log_path_for(tool: Tool, settings: dict[str, str]) -> Path:
    return resolve_log_path(
        directory=Path(tool.directory),
        log_path=tool.log_path,
        default_log_dir=settings.get("default_log_dir", ""),
        tool_id=tool.id,
    )


def start_tool(repository: ToolRepository, settings: dict[str, str], tool: Tool) -> None:
    if tool.port and not is_port_free(tool.port):
        flash(
            f"「{tool.name}」を起動できません。ポート {tool.port} は他のプロセスが使用中です。",
            "error",
        )
        return
    try:
        result = launch(
            command=tool.command,
            kind=tool.kind,
            port=tool.port,
            directory=tool.directory,
            log_path=log_path_for(tool, settings),
        )
    except LaunchError as exc:
        flash(f"「{tool.name}」を起動できませんでした。{exc}", "error")
        return

    repository.record_start(int(tool.id), pid=result.pid, created_at=result.created_at)
    if result.created_at is None:
        flash(
            f"「{tool.name}」を起動しましたが、プロセスの起動時刻を取得できませんでした。"
            "安全のため、この起動は「停止」ボタンでは止められません。",
            "warning",
        )
    else:
        flash(f"「{tool.name}」を起動しました。")


def wait_port_released(port: int, timeout: float = STOP_RELEASE_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_port_free(port):
            return True
        time.sleep(0.25)
    return is_port_free(port)


def stop_tool(repository: ToolRepository, tool: Tool) -> bool:
    """記録済みのPIDで停止する。

    照合が取れず、ポートから引き直せる場合は False を返す（呼び出し側で確認ダイアログを開く）。
    """
    try:
        stop(tool.last_pid, tool.last_pid_created_at)
    except ProcessNotIdentifiedError as exc:
        if exc.status in (PidStatus.NOT_FOUND, PidStatus.MISMATCH):
            # 記録しているプロセスはもう存在しない。古い記録は消しておく。
            repository.clear_pid(int(tool.id))
        if tool.port:
            return False
        flash(
            f"「{tool.name}」: 対象プロセスを特定できませんでした。停止していません。"
            "TOOL NEXUSの外で起動されたか、既に終了している可能性があります。"
            "動いている場合はタスクマネージャ（Linuxは kill）等で停止してください。",
            "error",
        )
        return True
    except StopError as exc:
        flash(f"「{tool.name}」を停止できませんでした。{exc}", "error")
        return True

    repository.clear_pid(int(tool.id))
    report_stopped(tool)
    return True


def report_stopped(tool: Tool) -> None:
    if tool.port and not wait_port_released(tool.port):
        flash(
            f"「{tool.name}」を停止しましたが、ポート {tool.port} がまだ解放されていません。",
            "warning",
        )
    else:
        flash(f"「{tool.name}」を停止しました。")


def start_autostart_tools(repository: ToolRepository, settings: dict[str, str]) -> None:
    """「まとめて起動」の対象を順に起動する。起動中・起動中…のものは飛ばす（二重起動しない）。"""
    tools = [tool for tool in repository.list_all() if tool.autostart]
    if not tools:
        flash("まとめて起動の対象がありません。編集画面で「まとめて起動の対象にする」を有効にしてください。", "warning")
        return
    alive_by_id = probe_all(tools, timeout=health_timeout(settings))
    started = skipped = 0
    for tool in tools:
        alive = alive_by_id.get(int(tool.id))
        status = derive_status(tool, alive)
        # 監視しない設定（不明）でも起動記録が残っていれば動いている可能性があるので飛ばす
        if status in (Status.RUNNING, Status.STARTING) or (status is Status.UNKNOWN and tool.last_pid):
            skipped += 1
            continue
        start_tool(repository, settings, tool)
        started += 1
    flash(f"まとめて起動: {started} 件を起動しました（起動済みのため {skipped} 件を飛ばしました）。")
    request_check()


# ----------------------------------------------------------------------
# ダイアログ制御
# ----------------------------------------------------------------------
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


def on_kind_change() -> None:
    """種別を変えたら死活監視モードを種別の既定値にする（exe → process）。"""
    st.session_state["form_health_mode"] = default_health_mode(st.session_state["form_kind"])


def on_pick_file() -> None:
    """ファイルを選ばせ、起動方式を推測してフォームに入れる（SPEC 6.9）。

    ボタンの on_click で呼ぶ（ウィジェット生成前に session_state を書き換えるため）。
    推測は入力欄に入れるだけで、保存はユーザーが確認してから行う。
    """
    try:
        path = pick_file(st.session_state.get("form_directory") or None)
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
    st.session_state["form_kind"] = suggestion.kind
    st.session_state["form_health_mode"] = suggestion.health_mode
    st.session_state["form_notes"] = [("info", note) for note in suggestion.notes] + [
        ("info", "推測した内容です。下の「実行されるコマンド」を確認してから登録してください。")
    ]


def on_pick_folder() -> None:
    try:
        path = pick_folder(st.session_state.get("form_directory") or None)
    except AssistError as exc:
        st.session_state["form_notes"] = [("error", str(exc))]
        return
    if path is not None:
        st.session_state["form_directory"] = str(path)


def render_command_preview(values: dict[str, Any]) -> None:
    """実行されるコマンドを表示する（{port} の置き換えと --server.* の付与を反映）。"""
    command = str(values["command"] or "").strip()
    if not command:
        return
    port_text = str(values["port"] or "").strip()
    auto = not port_text and can_auto_assign_port(values["kind"], command)
    try:
        argv = build_argv(
            command, kind=values["kind"], port=port_text or ("<保存時に割当>" if auto else None)
        )
    except LaunchError as exc:
        st.caption(f"実行されるコマンド: {exc}")
        return
    shown = " ".join(quote(arg) for arg in argv)
    st.caption("実行されるコマンド（作業ディレクトリで実行）")
    st.code(shown, language=None, wrap_lines=True)


def render_tool_form() -> dict[str, Any]:
    pick_col, note_col = st.columns([1.6, 5], vertical_alignment="center")
    pick_col.button(
        "ファイルから入力",
        icon=":material/folder_open:",
        use_container_width=True,
        on_click=on_pick_file,
        key="form_pick_file",
        help=f"起動する {platform_ops.EXECUTABLE_LABEL} を選ぶと、作業ディレクトリ・venv・種別・コマンドを推測して入力します。"
        "ダイアログはこのPCの画面に開きます。",
    )
    note_col.caption(f"起動する {platform_ops.EXECUTABLE_LABEL} を選ぶと、venv や uv も含めて推測して入力します。")
    for level, note in st.session_state.get("form_notes", []):
        if level == "error":
            st.error(note, icon="⛔")
        else:
            st.info(note, icon="ℹ️")

    st.text_input("名前 *", key="form_name", placeholder="在庫チェッカー")
    left, right = st.columns(2)
    with left:
        st.selectbox(
            "種別 *",
            options=list(KIND_VALUES),
            format_func=kind_label,
            key="form_kind",
            on_change=on_kind_change,
        )
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
    values = render_tool_form()
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

    values = render_tool_form()
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
def port_stop_dialog(repository: ToolRepository) -> None:
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
            repository.clear_pid(int(target.id))
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
            "form_command": detected.process.command_line,
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
        port_stop_dialog(repository)
    elif dialog == "detect":
        detect_dialog(repository)


# ----------------------------------------------------------------------
# 一覧（フラグメント）
# ----------------------------------------------------------------------
def format_time(value: str | None) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


def open_url(tool: Tool) -> str | None:
    """「開く」のURL。127.0.0.1 固定で、ユーザー入力は整数のポートのみ使う。"""
    return f"http://127.0.0.1:{int(tool.port)}" if tool.port else None


NOTICES: dict[Status, str] = {
    Status.STARTING: "起動中…（応答を待っています）",
    Status.FAILED: "起動できていない可能性があります。ログを確認してください。",
    Status.UNKNOWN: "",
}


def render_tool_row(
    repository: ToolRepository, settings: dict[str, str], health: ToolHealth
) -> None:
    """1件を1行で描画する。

    行あたりのウィジェットは 起動/停止・ログ・編集 の3つに抑える。
    状態・名前・パスは1つのHTML、「開く」は素のアンカーで描く（0ウィジェット）。
    """
    tool = health.tool
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
        [tool.name, tool.directory, tool.command, tool.description, str(tool.port or "")]
    ).lower()
    return all(word in haystack for word in query.lower().split())


def request_check() -> None:
    """次の描画で必ずヘルスチェックを行う（再チェック・起動・停止の直後）。"""
    st.session_state["force_check"] = True


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

    with st.container(key="tn-listhead"):
        summary_col, recheck_col = st.columns([8, 1.5], vertical_alignment="center")
        with summary_col:
            render_summary(
                [
                    f"<span>起動中 <strong>{running}</strong> / {len(healths)}</span>",
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


# ----------------------------------------------------------------------
# 設定画面（SPEC 8.2）
# ----------------------------------------------------------------------
def go_to_settings() -> None:
    st.session_state["screen"] = SCREEN_SETTINGS


def go_to_main() -> None:
    st.session_state["screen"] = SCREEN_MAIN


def select_settings_section(section: str) -> None:
    st.session_state["settings_section"] = section


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
    """表示順・まとめて起動の一括編集と、まとめて削除。

    行は非表示のID列で突き合わせる（並べ替え後に位置で照合すると別の行に適用されるため）。
    """
    tools = repository.list_all()
    if not tools:
        render_note("ツールが登録されていません。")
        return
    st.caption("表示順と「まとめて起動」を表で編集できます。削除する行は「選択」にチェックしてください。")
    rows = [
        {
            "ID": tool.id,
            "選択": False,
            "名前": tool.name,
            "種別": kind_label(tool.kind),
            "ポート": tool.port,
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
            "ポート": st.column_config.NumberColumn("ポート", format="%d", width="small"),
            "表示順": st.column_config.NumberColumn("表示順", step=1, format="%d", width="small"),
            "まとめて起動": st.column_config.CheckboxColumn("まとめて起動", width="small"),
        },
    )

    by_id = {tool.id: tool for tool in tools}
    changes = [
        (row["ID"], int(row["表示順"] or 0), bool(row["まとめて起動"]))
        for row in edited
        if row.get("ID") in by_id
        and (
            int(row["表示順"] or 0) != by_id[row["ID"]].sort_order
            or bool(row["まとめて起動"]) != by_id[row["ID"]].autostart
        )
    ]
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
        flash(f"{len(changes)}件の表示順・まとめて起動を保存しました。")
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


# ----------------------------------------------------------------------
# 上部バー・左ナビ・ツールバー
# ----------------------------------------------------------------------
def select_kind(kind: str) -> None:
    st.session_state["kind_filter"] = kind
    st.session_state["screen"] = SCREEN_MAIN


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


# ----------------------------------------------------------------------
# メイン
# ----------------------------------------------------------------------
def main() -> None:
    st.set_page_config(
        page_title=APP_NAME,
        page_icon=APP_ICON,
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    apply_styles()
    init_state()

    try:
        repository = get_repository()
        settings = repository.get_settings()
    except DatabaseError as exc:
        logger.exception("データベースの初期化に失敗しました")
        st.error(str(exc), icon="⛔")
        st.stop()
        return

    render_top_bar()
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


if __name__ == "__main__":
    main()
