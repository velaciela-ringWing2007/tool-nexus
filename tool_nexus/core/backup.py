"""JSONバックアップと復元（SPEC 7.3）.

バックアップに含めるのは設定だけ。起動記録（last_pid など）と内部IDは、
別PCや別時点では意味を持たないため含めない。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from tool_nexus.core.constants import APP_NAME, DEFAULT_SETTINGS
from tool_nexus.core.models import Tool, ValidationError, build_tool, now_iso
from tool_nexus.core.repositories import DuplicatePortError, ToolRepository
from tool_nexus.core.settings import SettingsError, validate_settings

SCHEMA_VERSION = 1

MODE_APPEND = "append"
MODE_REPLACE = "replace"
MODE_LABELS: dict[str, str] = {
    MODE_APPEND: "追加（既存を残す。同じ名前・作業ディレクトリのものは飛ばす）",
    MODE_REPLACE: "置き換え（既存のツールをすべて削除してから復元）",
}

# DBの列名 ⇔ JSONのキー
_TOOL_KEYS: dict[str, str] = {
    "name": "name",
    "kind": "kind",
    "directory": "directory",
    "command": "command",
    "port": "port",
    "health_mode": "healthMode",
    "log_path": "logPath",
    "autostart": "autostart",
    "description": "description",
    "sort_order": "sortOrder",
    "target": "target",
}


class BackupError(ValueError):
    """バックアップ全体を読めない場合に送出する例外."""


@dataclass(slots=True)
class ParsedBackup:
    """検証済みのバックアップ内容."""

    tools: list[Tool]
    settings: dict[str, str]
    problems: list[str] = field(default_factory=list)  # 取り込めない行と理由
    settings_problem: str = ""


@dataclass(slots=True)
class RestoreSummary:
    added: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # 「名前: 理由」
    settings_restored: bool = False


# ----------------------------------------------------------------------
# エクスポート
# ----------------------------------------------------------------------
def tool_to_dict(tool: Tool) -> dict[str, Any]:
    return {json_key: getattr(tool, attr) for attr, json_key in _TOOL_KEYS.items()}


def export_backup(repository: ToolRepository) -> dict[str, Any]:
    return {
        "app": APP_NAME,
        "schemaVersion": SCHEMA_VERSION,
        "exportedAt": now_iso(),
        "settings": repository.get_settings(),
        "tools": [tool_to_dict(tool) for tool in repository.list_all()],
    }


def export_bytes(repository: ToolRepository) -> bytes:
    return json.dumps(export_backup(repository), ensure_ascii=False, indent=2).encode("utf-8")


# ----------------------------------------------------------------------
# 読み込み
# ----------------------------------------------------------------------
def parse_backup(text: str | bytes) -> ParsedBackup:
    """バックアップを読み、各ツールを登録時と同じ検証に通す。

    作業ディレクトリの存在確認はしない（別PCのバックアップを持ち込むことがあるため。起動時に確認される）。
    """
    if isinstance(text, bytes):
        text = text.decode("utf-8-sig", errors="replace")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BackupError(f"JSONとして読めませんでした（{exc.lineno}行目）。") from exc
    if not isinstance(data, dict) or not isinstance(data.get("tools"), list):
        raise BackupError("TOOL NEXUS のバックアップではありません（tools がありません）。")
    version = data.get("schemaVersion", 1)
    if not isinstance(version, int) or version > SCHEMA_VERSION:
        raise BackupError(
            f"このバックアップの形式（schemaVersion={version}）には対応していません。"
            "TOOL NEXUS を更新してから復元してください。"
        )

    tools: list[Tool] = []
    problems: list[str] = []
    for index, item in enumerate(data["tools"], start=1):
        label = f"{index}件目"
        if not isinstance(item, dict):
            problems.append(f"{label}: 形式が不正です。")
            continue
        label = f"{index}件目「{item.get('name') or '（名前なし）'}」"
        values = {attr: item.get(json_key) for attr, json_key in _TOOL_KEYS.items() if json_key in item}
        try:
            tools.append(build_tool(**values, check_directory=False))
        except (ValidationError, TypeError) as exc:
            problems.append(f"{label}: {exc}")

    settings: dict[str, str] = {}
    settings_problem = ""
    raw_settings = data.get("settings")
    if isinstance(raw_settings, dict):
        known = {key: value for key, value in raw_settings.items() if key in DEFAULT_SETTINGS}
        try:
            settings = validate_settings(known)
        except SettingsError as exc:
            settings_problem = str(exc)
    return ParsedBackup(tools, settings, problems, settings_problem)


# ----------------------------------------------------------------------
# 復元
# ----------------------------------------------------------------------
def _identity(tool: Tool) -> tuple[str, str]:
    """重複判定のキー（名前と作業ディレクトリ。Windows では大文字小文字を区別しない）。"""
    return tool.name.strip().casefold(), os.path.normcase(os.path.normpath(tool.directory.strip()))


def restore_backup(
    repository: ToolRepository,
    parsed: ParsedBackup,
    *,
    mode: str = MODE_APPEND,
    include_settings: bool = False,
) -> RestoreSummary:
    summary = RestoreSummary()

    if mode == MODE_REPLACE:
        # 置き換え: バックアップ内でポートが重複する行は後のものを飛ばしてから、まとめて入れ替える
        kept: list[Tool] = []
        ports: set[int] = set()
        for tool in parsed.tools:
            if tool.port is not None and tool.port in ports:
                summary.skipped.append(f"{tool.name}: ポート {tool.port} がバックアップ内で重複しています。")
                continue
            if tool.port is not None:
                ports.add(tool.port)
            kept.append(tool)
        repository.replace_all(kept)
        summary.added = [tool.name for tool in kept]
    elif mode == MODE_APPEND:
        existing = {_identity(tool) for tool in repository.list_all()}
        for tool in parsed.tools:
            key = _identity(tool)
            if key in existing:
                summary.skipped.append(f"{tool.name}: 同じ名前・作業ディレクトリのツールが登録済みです。")
                continue
            try:
                repository.create(tool)
            except DuplicatePortError as exc:
                summary.skipped.append(f"{tool.name}: {exc}")
                continue
            existing.add(key)
            summary.added.append(tool.name)
    else:
        raise ValueError(f"未知の復元方法です: {mode}")

    if include_settings and parsed.settings:
        repository.set_settings(parsed.settings)
        summary.settings_restored = True
    return summary
