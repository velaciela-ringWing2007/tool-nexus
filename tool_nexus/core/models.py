"""データモデルと、SQLite行との相互変換."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from tool_nexus.core.constants import (
    DEFAULT_HEALTH_MODE_BY_KIND,
    HEALTH_HTTP,
    HEALTH_MODE_VALUES,
    HEALTH_NONE,
    KIND_LINK,
    KIND_STREAMLIT,
    KIND_VALUES,
    MAX_GROUP_LENGTH,
    MAX_NAME_LENGTH,
    MAX_PORT,
    MIN_PORT,
    PORT_PLACEHOLDER,
    UNGROUPED_LABEL,
)
from tool_nexus.process.control import split_command


class ValidationError(ValueError):
    """入力値が業務要件を満たさない場合に送出する例外."""


@dataclass(slots=True)
class Tool:
    """1件の登録ツール.

    起動中かどうかはここに持たない（保存した瞬間から嘘になるため）。
    last_* は状態ではなく、起動操作の記録である。
    """

    id: int | None
    name: str
    directory: str
    command: str
    kind: str = KIND_STREAMLIT
    port: int | None = None
    health_mode: str = HEALTH_HTTP
    log_path: str = ""
    autostart: bool = False
    description: str = ""
    sort_order: int = 0
    target: str = ""
    group_name: str = ""
    last_pid: int | None = None
    last_pid_created_at: str | None = None
    last_started_at: str | None = None
    last_stopped_at: str | None = None
    last_seen_at: str | None = None
    created_at: str = ""
    updated_at: str = ""


def now_iso() -> str:
    """ローカル時刻（タイムゾーン付き）の秒精度ISO 8601文字列を返す。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def now_iso_precise() -> str:
    """マイクロ秒まで含むローカル時刻。起動・停止・ヘルス通過の記録に使う。

    秒精度だと、再起動（停止の直後に起動）で停止と起動が同じ秒になり、
    起動中…なのに「停止」と表示されてしまう（SPEC 6.4）。
    """
    return datetime.now().astimezone().isoformat(timespec="microseconds")


def row_to_tool(row: sqlite3.Row) -> Tool:
    """SQLiteの行をモデルへ変換する。"""
    return Tool(
        id=row["id"],
        name=row["name"],
        kind=row["kind"],
        directory=row["directory"],
        command=row["command"],
        port=row["port"],
        health_mode=row["health_mode"],
        log_path=row["log_path"] or "",
        autostart=bool(row["autostart"]),
        description=row["description"] or "",
        sort_order=int(row["sort_order"]),
        last_pid=row["last_pid"],
        last_pid_created_at=row["last_pid_created_at"],
        last_started_at=row["last_started_at"],
        last_stopped_at=row["last_stopped_at"],
        target=row["target"] or "",
        group_name=row["group_name"] or "",
        last_seen_at=row["last_seen_at"],
        created_at=row["created_at"] or "",
        updated_at=row["updated_at"] or "",
    )


def tool_to_params(tool: Tool) -> dict[str, Any]:
    """モデルのうち、ユーザーが編集する設定項目をSQLのバインドパラメータへ変換する。

    last_* は起動・停止の操作でだけ更新するため含めない。
    """
    return {
        "name": tool.name,
        "kind": tool.kind,
        "directory": tool.directory,
        "command": tool.command,
        "port": tool.port,
        "health_mode": tool.health_mode,
        "log_path": tool.log_path,
        "autostart": 1 if tool.autostart else 0,
        "description": tool.description,
        "sort_order": tool.sort_order,
        "target": tool.target,
        "group_name": tool.group_name,
    }


# ----------------------------------------------------------------------
# 入力の検証・正規化
# ----------------------------------------------------------------------
def can_auto_assign_port(kind: str, command: str) -> bool:
    """自動割当してよいか。割り当てたポートで待ち受けることが保証できる場合だけ True。

    streamlit は --server.port を付与し、{port} を含むコマンドは起動時に置き換えるため保証できる。
    それ以外に振ると、実際には待ち受けないまま監視され続ける。
    """
    return kind == KIND_STREAMLIT or PORT_PLACEHOLDER in (command or "")


def default_health_mode(kind: str) -> str:
    """種別に応じた死活監視モードの既定値（登録フォームの初期値に使う）。"""
    return DEFAULT_HEALTH_MODE_BY_KIND.get(kind, HEALTH_HTTP)


def normalize_name(raw: str | None) -> str:
    name = (raw or "").strip()
    if not name:
        raise ValidationError("名前を入力してください。")
    if len(name) > MAX_NAME_LENGTH:
        raise ValidationError(f"名前が長すぎます。{MAX_NAME_LENGTH}文字以内で入力してください。")
    return name


def normalize_kind(raw: str | None) -> str:
    kind = (raw or "").strip().lower()
    if kind not in KIND_VALUES:
        raise ValidationError(f"種別が不正です: {raw}")
    return kind


def normalize_health_mode(raw: str | None, kind: str) -> str:
    """未指定なら種別の既定値にする。"""
    mode = (raw or "").strip().lower()
    if not mode:
        return default_health_mode(kind)
    if mode not in HEALTH_MODE_VALUES:
        raise ValidationError(f"死活監視モードが不正です: {raw}")
    return mode


def normalize_directory(raw: str | None, *, check_exists: bool) -> str:
    directory = (raw or "").strip().strip('"')
    if not directory:
        raise ValidationError("作業ディレクトリを入力してください。")
    if check_exists and not Path(directory).is_dir():
        raise ValidationError(f"作業ディレクトリが見つかりません: {directory}")
    return directory


def normalize_group(raw: str | None) -> str:
    """グループ名を正規化する。空は未分類。"""
    group = " ".join((raw or "").split())
    if group == UNGROUPED_LABEL:
        return ""
    if len(group) > MAX_GROUP_LENGTH:
        raise ValidationError(f"グループ名が長すぎます。{MAX_GROUP_LENGTH}文字以内で入力してください。")
    return group


def is_url(value: str) -> bool:
    return urlparse(value).scheme.lower() in ("http", "https")


def normalize_target(raw: str | None, *, check_exists: bool) -> str:
    """リンク先を検証する。URL（http / https）か、ローカルのファイル／フォルダの絶対パス。"""
    target = (raw or "").strip().strip('"')
    if not target:
        raise ValidationError("リンク先（URL またはファイル／フォルダのパス）を入力してください。")
    if is_url(target):
        if not urlparse(target).netloc:
            raise ValidationError(f"URL の形式が正しくありません: {target}")
        return target
    if "://" in target or target.lower().startswith(("javascript:", "data:", "file:")):
        raise ValidationError("リンク先の URL は http / https のみ使えます（ローカルのファイルはパスで指定します）。")
    if not Path(target).is_absolute():
        raise ValidationError("ローカルのファイル／フォルダは絶対パスで指定してください。")
    if check_exists and not Path(target).exists():
        raise ValidationError(f"リンク先が見つかりません: {target}")
    return target


def normalize_command(raw: str | None) -> str:
    command = (raw or "").strip()
    if not command:
        raise ValidationError("起動コマンドを入力してください。")
    try:
        argv = split_command(command)
    except ValueError as exc:  # 閉じていないクォートなど
        raise ValidationError(f"起動コマンドを解釈できません: {exc}") from exc
    if not argv or not argv[0]:
        raise ValidationError("起動コマンドを入力してください。")
    return command


def normalize_port(raw: Any) -> int | None:
    """ポートを整数へ変換する。未入力は None（自動割当・exe用）。"""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if isinstance(raw, bool):
        raise ValidationError("ポートは整数で入力してください。")
    try:
        port = int(str(raw).strip())
    except ValueError as exc:
        raise ValidationError("ポートは整数で入力してください。") from exc
    if not MIN_PORT <= port <= MAX_PORT:
        raise ValidationError(f"ポートは{MIN_PORT}〜{MAX_PORT}で入力してください。")
    return port


def normalize_sort_order(raw: Any) -> int:
    if raw is None or raw == "":
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError("表示順は整数で入力してください。") from exc


def build_tool(
    *,
    id: int | None = None,
    name: str | None,
    directory: str | None,
    command: str | None,
    kind: str | None = KIND_STREAMLIT,
    port: Any = None,
    health_mode: str | None = None,
    log_path: str | None = "",
    autostart: bool = False,
    description: str | None = "",
    sort_order: Any = 0,
    target: str | None = "",
    group_name: str | None = "",
    check_directory: bool = True,
) -> Tool:
    """入力値を検証・正規化して Tool を組み立てる。

    health_mode が http のときはポート必須。黙って process へ切り替えることはしない
    （ユーザーが気づかないまま監視の強度が落ちるため）。
    check_directory=False はバックアップの復元など、別PCのパスを受け入れる場合に使う。
    """
    normalized_kind = normalize_kind(kind)
    if normalized_kind == KIND_LINK:
        # リンクは起動しないので、作業ディレクトリ・コマンド・ポート・監視は持たない（SPEC 6.10）
        return Tool(
            id=id,
            name=normalize_name(name),
            kind=KIND_LINK,
            directory="",
            command="",
            port=None,
            health_mode=HEALTH_NONE,
            log_path="",
            autostart=False,
            description=(description or "").strip(),
            sort_order=normalize_sort_order(sort_order),
            target=normalize_target(target, check_exists=check_directory),
            group_name=normalize_group(group_name),
        )
    normalized_mode = normalize_health_mode(health_mode, normalized_kind)
    normalized_port = normalize_port(port)
    normalized_command = normalize_command(command)
    if PORT_PLACEHOLDER in normalized_command and normalized_port is None:
        raise ValidationError(
            f"起動コマンドに {PORT_PLACEHOLDER} があるため、ポートが必要です。"
        )
    if normalized_mode == HEALTH_HTTP and normalized_port is None:
        raise ValidationError(
            "死活監視モードがHTTPのときはポートが必要です。"
            "ポートを入力するか、監視モードを「プロセス」または「監視しない」にしてください。"
        )

    return Tool(
        id=id,
        name=normalize_name(name),
        kind=normalized_kind,
        directory=normalize_directory(directory, check_exists=check_directory),
        command=normalized_command,
        port=normalized_port,
        health_mode=normalized_mode,
        log_path=(log_path or "").strip().strip('"'),
        autostart=bool(autostart),
        description=(description or "").strip(),
        sort_order=normalize_sort_order(sort_order),
        group_name=normalize_group(group_name),
    )
