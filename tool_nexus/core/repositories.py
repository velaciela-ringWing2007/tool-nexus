"""tools / settings テーブルへのデータアクセス層.

SQLはこのモジュールに閉じ込め、UI層からSQLite接続を直接扱わない。
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Iterable

from tool_nexus.core.constants import DEFAULT_SETTINGS
from tool_nexus.core.database import DatabaseError, connect, initialize_database, transaction
from tool_nexus.core.models import Tool, now_iso, now_iso_precise, row_to_tool, tool_to_params

_SELECT_COLUMNS = """
    id, name, kind, directory, command, port, health_mode, log_path, autostart,
    description, sort_order, last_pid, last_pid_created_at, last_started_at,
    last_stopped_at, last_seen_at, target, group_name, created_at, updated_at
"""

# 既定の並び順: 表示順 → 名前
_DEFAULT_ORDER = "ORDER BY sort_order ASC, name COLLATE NOCASE ASC, id ASC"


class DuplicatePortError(ValueError):
    """ポートが他のツールに登録済みの場合に送出する例外."""

    def __init__(self, port: int, existing: Tool) -> None:
        super().__init__(f"ポート {port} は「{existing.name}」が使用しています。")
        self.port = port
        self.existing = existing


class ToolNotFoundError(LookupError):
    """指定IDのツールが存在しない場合に送出する例外."""


class ToolRepository:
    """登録ツールと設定のリポジトリ."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)

    # ------------------------------------------------------------------
    # 初期化
    # ------------------------------------------------------------------
    def initialize(self) -> None:
        """DBファイルとテーブルを用意する。"""
        initialize_database(self.db_path)

    # ------------------------------------------------------------------
    # 取得
    # ------------------------------------------------------------------
    def list_all(self) -> list[Tool]:
        """全件を既定の並び順で取得する。"""
        with connect(self.db_path) as connection:
            rows = connection.execute(
                f"SELECT {_SELECT_COLUMNS} FROM tools {_DEFAULT_ORDER}"
            ).fetchall()
        return [row_to_tool(row) for row in rows]

    def get_by_id(self, tool_id: int) -> Tool | None:
        """ID指定で1件取得する。存在しなければ None を返す。"""
        with connect(self.db_path) as connection:
            row = connection.execute(
                f"SELECT {_SELECT_COLUMNS} FROM tools WHERE id = ?", (tool_id,)
            ).fetchone()
        return row_to_tool(row) if row else None

    def find_by_port(self, port: int, *, exclude_id: int | None = None) -> Tool | None:
        """指定ポートを使っているツールを返す。exclude_id は編集中の自分自身を除く。"""
        with connect(self.db_path) as connection:
            return self._find_by_port(connection, port, exclude_id)

    def used_ports(self) -> set[int]:
        """登録済みのポートの集合（自動割当で避ける）。"""
        with connect(self.db_path) as connection:
            rows = connection.execute("SELECT port FROM tools WHERE port IS NOT NULL").fetchall()
        return {int(row["port"]) for row in rows}

    def group_names(self) -> list[str]:
        """登録済みのグループ名（空を除く、名前順）。"""
        with connect(self.db_path) as connection:
            rows = connection.execute(
                "SELECT DISTINCT group_name FROM tools WHERE group_name <> '' ORDER BY group_name COLLATE NOCASE"
            ).fetchall()
        return [row["group_name"] for row in rows]

    def count(self) -> int:
        with connect(self.db_path) as connection:
            row = connection.execute("SELECT COUNT(*) AS n FROM tools").fetchone()
        return int(row["n"])

    # ------------------------------------------------------------------
    # 登録・更新・削除
    # ------------------------------------------------------------------
    def create(self, tool: Tool) -> Tool:
        """新規登録し、採番されたIDを含むモデルを返す。"""
        timestamp = now_iso()
        record = replace(tool, id=None, created_at=timestamp, updated_at=timestamp)
        params = tool_to_params(record) | {"created_at": timestamp, "updated_at": timestamp}

        try:
            with connect(self.db_path) as connection, transaction(connection):
                self._ensure_port_free(connection, record.port, exclude_id=None)
                cursor = connection.execute(
                    """
                    INSERT INTO tools (
                        name, kind, directory, command, port, health_mode, log_path,
                        autostart, description, sort_order, target, group_name, created_at, updated_at
                    ) VALUES (
                        :name, :kind, :directory, :command, :port, :health_mode, :log_path,
                        :autostart, :description, :sort_order, :target, :group_name, :created_at, :updated_at
                    )
                    """,
                    params,
                )
                new_id = int(cursor.lastrowid)
        except sqlite3.Error as exc:
            raise DatabaseError("ツールの登録に失敗しました。") from exc
        return replace(record, id=new_id)

    def update(self, tool: Tool) -> Tool:
        """設定項目を更新する。起動記録（last_*）は変更しない。"""
        if tool.id is None:
            raise ValueError("更新にはIDが必要です。")
        timestamp = now_iso()
        params = tool_to_params(tool) | {"id": tool.id, "updated_at": timestamp}

        try:
            with connect(self.db_path) as connection, transaction(connection):
                self._ensure_port_free(connection, tool.port, exclude_id=tool.id)
                cursor = connection.execute(
                    """
                    UPDATE tools SET
                        name = :name, kind = :kind, directory = :directory,
                        command = :command, port = :port, health_mode = :health_mode,
                        log_path = :log_path, autostart = :autostart,
                        description = :description, sort_order = :sort_order, target = :target,
                        group_name = :group_name,
                        updated_at = :updated_at
                    WHERE id = :id
                    """,
                    params,
                )
                if cursor.rowcount == 0:
                    raise ToolNotFoundError(f"ツールが見つかりません: id={tool.id}")
        except sqlite3.Error as exc:
            raise DatabaseError("ツールの更新に失敗しました。") from exc

        updated = self.get_by_id(tool.id)
        assert updated is not None
        return updated

    def delete(self, tool_id: int) -> bool:
        """削除する。削除できた場合 True。"""
        try:
            with connect(self.db_path) as connection, transaction(connection):
                cursor = connection.execute("DELETE FROM tools WHERE id = ?", (tool_id,))
        except sqlite3.Error as exc:
            raise DatabaseError("ツールの削除に失敗しました。") from exc
        return cursor.rowcount > 0

    def delete_many(self, tool_ids: Iterable[int]) -> int:
        """まとめて削除する。削除した件数を返す。"""
        ids = sorted({int(tool_id) for tool_id in tool_ids})
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        try:
            with connect(self.db_path) as connection, transaction(connection):
                cursor = connection.execute(f"DELETE FROM tools WHERE id IN ({placeholders})", ids)
        except sqlite3.Error as exc:
            raise DatabaseError("ツールの削除に失敗しました。") from exc
        return cursor.rowcount

    def update_order(self, changes: Iterable[tuple[int, int, bool, str]]) -> int:
        """(ID, 表示順, まとめて起動, グループ) の組で一括更新する。更新した件数を返す。

        行はIDで突き合わせる（表の並べ替え後に位置で照合すると別の行に適用されるため）。
        グループ名は呼び出し側で正規化しておく（models.normalize_group）。
        """
        rows = [
            (int(order), 1 if autostart else 0, group, now_iso(), int(tool_id))
            for tool_id, order, autostart, group in changes
        ]
        if not rows:
            return 0
        try:
            with connect(self.db_path) as connection, transaction(connection):
                cursor = connection.executemany(
                    "UPDATE tools SET sort_order = ?, autostart = ?, group_name = ?, updated_at = ? WHERE id = ?",
                    rows,
                )
        except sqlite3.Error as exc:
            raise DatabaseError("表示順の更新に失敗しました。") from exc
        return cursor.rowcount

    def replace_all(self, tools: Iterable[Tool]) -> int:
        """全ツールを削除して入れ替える（バックアップの「置き換え」復元）。1つのトランザクションで行う。"""
        timestamp = now_iso()
        records = [tool_to_params(t) | {"created_at": timestamp, "updated_at": timestamp} for t in tools]
        try:
            with connect(self.db_path) as connection, transaction(connection):
                connection.execute("DELETE FROM tools")
                seen_ports: set[int] = set()
                for record in records:
                    port = record["port"]
                    if port is not None and port in seen_ports:
                        raise DuplicatePortError(port, Tool(id=None, name=record["name"], directory="", command=""))
                    if port is not None:
                        seen_ports.add(port)
                    connection.execute(
                        """
                        INSERT INTO tools (
                            name, kind, directory, command, port, health_mode, log_path,
                            autostart, description, sort_order, target, group_name, created_at, updated_at
                        ) VALUES (
                            :name, :kind, :directory, :command, :port, :health_mode, :log_path,
                            :autostart, :description, :sort_order, :target, :group_name, :created_at, :updated_at
                        )
                        """,
                        record,
                    )
        except sqlite3.Error as exc:
            raise DatabaseError("ツールの復元に失敗しました。") from exc
        return len(records)

    # ------------------------------------------------------------------
    # 起動記録
    # ------------------------------------------------------------------
    def record_start(self, tool_id: int, *, pid: int, created_at: str | None) -> None:
        """起動操作を記録する。

        PID単体では再利用を見分けられないため、last_pid と last_pid_created_at は
        常に対で保存する。起動時刻が取れなかった場合はどちらも保存しない
        （停止はポートからの引き直しに倒す）。
        """
        pair = (pid, created_at) if created_at else (None, None)
        self._execute_update(
            """
            UPDATE tools SET last_pid = ?, last_pid_created_at = ?, last_started_at = ?
            WHERE id = ?
            """,
            (*pair, now_iso_precise(), tool_id),
        )

    def record_pid(self, tool_id: int, *, pid: int, created_at: str) -> None:
        """探し直したプロセスの PID と起動時刻を保存し直す（起動の記録は変えない。SPEC 6.3）。"""
        self._execute_update(
            "UPDATE tools SET last_pid = ?, last_pid_created_at = ? WHERE id = ?",
            (pid, created_at, tool_id),
        )

    def clear_pid(self, tool_id: int) -> None:
        """停止後などに、PIDと起動時刻を対で消去する。"""
        self._execute_update(
            "UPDATE tools SET last_pid = NULL, last_pid_created_at = NULL WHERE id = ?",
            (tool_id,),
        )

    def record_stop(self, tool_id: int) -> None:
        """停止操作を記録し、PIDと起動時刻を対で消去する。

        停止の記録が無いと、起動から30秒以内に停止したときに「起動中…」と表示されてしまう（SPEC 6.4）。
        """
        self._execute_update(
            """
            UPDATE tools SET last_pid = NULL, last_pid_created_at = NULL, last_stopped_at = ?
            WHERE id = ?
            """,
            (now_iso_precise(), tool_id),
        )

    def mark_seen(self, tool_id: int) -> None:
        """ヘルスチェックが通った時刻を記録する。"""
        self._execute_update(
            "UPDATE tools SET last_seen_at = ? WHERE id = ?", (now_iso_precise(), tool_id)
        )

    # ------------------------------------------------------------------
    # 設定
    # ------------------------------------------------------------------
    def get_settings(self) -> dict[str, str]:
        """全設定を返す。未保存のキーは既定値で補う。"""
        with connect(self.db_path) as connection:
            rows = connection.execute("SELECT key, value FROM settings").fetchall()
        stored = {row["key"]: row["value"] for row in rows}
        return DEFAULT_SETTINGS | {k: v for k, v in stored.items() if k in DEFAULT_SETTINGS}

    def get_setting(self, key: str) -> str:
        """1件の設定値を返す。未保存なら既定値。"""
        if key not in DEFAULT_SETTINGS:
            raise KeyError(f"未知の設定です: {key}")
        return self.get_settings()[key]

    def set_setting(self, key: str, value: str) -> None:
        """設定値を保存する。値の妥当性は呼び出し側で検証する（core.settings）。"""
        self.set_settings({key: value})

    def set_settings(self, values: dict[str, str]) -> None:
        """複数の設定値を1つのトランザクションで保存する。"""
        unknown = [key for key in values if key not in DEFAULT_SETTINGS]
        if unknown:
            raise KeyError(f"未知の設定です: {', '.join(unknown)}")
        try:
            with connect(self.db_path) as connection, transaction(connection):
                connection.executemany(
                    """
                    INSERT INTO settings (key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    [(key, str(value)) for key, value in values.items()],
                )
        except sqlite3.Error as exc:
            raise DatabaseError("設定の保存に失敗しました。") from exc

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    @staticmethod
    def _find_by_port(
        connection: sqlite3.Connection, port: int, exclude_id: int | None
    ) -> Tool | None:
        row = connection.execute(
            f"SELECT {_SELECT_COLUMNS} FROM tools WHERE port = ? AND id IS NOT ? LIMIT 1",
            (port, exclude_id),
        ).fetchone()
        return row_to_tool(row) if row else None

    def _ensure_port_free(
        self, connection: sqlite3.Connection, port: int | None, *, exclude_id: int | None
    ) -> None:
        if port is None:
            return
        existing = self._find_by_port(connection, port, exclude_id)
        if existing is not None:
            raise DuplicatePortError(port, existing)

    def _execute_update(self, sql: str, params: tuple) -> None:
        try:
            with connect(self.db_path) as connection, transaction(connection):
                cursor = connection.execute(sql, params)
                if cursor.rowcount == 0:
                    raise ToolNotFoundError(f"ツールが見つかりません: id={params[-1]}")
        except sqlite3.Error as exc:
            raise DatabaseError("起動記録の更新に失敗しました。") from exc
