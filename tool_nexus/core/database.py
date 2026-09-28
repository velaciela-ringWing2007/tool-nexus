"""SQLite接続とスキーマ初期化.

Streamlitはリクエストごとにスクリプトを再実行し、複数スレッドから
呼ばれることがあるため、接続は長期間保持せず操作単位で開閉する。
登録件数は多くても数十件を想定しており、この方式で十分速い。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from tool_nexus.core.constants import DATABASE_PATH

# 起動中かどうかは保存しない。現在の状態は毎回ヘルスチェックで取得する。
SCHEMA_SQL: str = """
CREATE TABLE IF NOT EXISTS tools (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'streamlit',
    directory TEXT NOT NULL,
    command TEXT NOT NULL,
    port INTEGER,
    health_mode TEXT NOT NULL DEFAULT 'http',
    log_path TEXT NOT NULL DEFAULT '',
    autostart INTEGER NOT NULL DEFAULT 0,
    description TEXT NOT NULL DEFAULT '',
    sort_order INTEGER NOT NULL DEFAULT 0,
    last_pid INTEGER,
    last_pid_created_at TEXT,
    last_started_at TEXT,
    last_stopped_at TEXT,
    target TEXT NOT NULL DEFAULT '',
    group_name TEXT NOT NULL DEFAULT '',
    last_seen_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class DatabaseError(RuntimeError):
    """データベース操作に失敗した場合に送出する例外."""


def _ensure_parent_dir(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)


def open_connection(db_path: Path | str = DATABASE_PATH) -> sqlite3.Connection:
    """SQLite接続を開いて返す。呼び出し側が close する責任を持つ。"""
    path = Path(db_path)
    _ensure_parent_dir(path)
    try:
        connection = sqlite3.connect(path, timeout=10.0)
    except sqlite3.Error as exc:
        raise DatabaseError(f"データベースに接続できませんでした: {path}") from exc

    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON;")
        connection.execute("PRAGMA journal_mode = WAL;")
    except sqlite3.Error as exc:
        connection.close()
        raise DatabaseError("データベースの初期設定に失敗しました。") from exc
    return connection


@contextmanager
def connect(db_path: Path | str = DATABASE_PATH) -> Iterator[sqlite3.Connection]:
    """接続を開き、処理終了後に必ず閉じるコンテキストマネージャ。"""
    connection = open_connection(db_path)
    try:
        yield connection
    finally:
        connection.close()


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """成功時にcommit、例外時にrollbackするコンテキストマネージャ。"""
    try:
        yield connection
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()


def _migrate(connection: sqlite3.Connection) -> None:
    """既存DBに後から足した列を補う。

    列の追加は ALTER TABLE で行い、既存データはそのまま残す。
    """
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(tools)")}
    if "last_stopped_at" not in columns:
        # 停止操作の記録（起動直後に停止したとき「起動中…」に戻らないようにするため。SPEC 6.4）
        connection.execute("ALTER TABLE tools ADD COLUMN last_stopped_at TEXT")
    if "target" not in columns:
        # リンク先（種別 link のみ。SPEC 6.10）
        connection.execute("ALTER TABLE tools ADD COLUMN target TEXT NOT NULL DEFAULT ''")
    if "group_name" not in columns:
        # グループ（SPEC 6.11）
        connection.execute("ALTER TABLE tools ADD COLUMN group_name TEXT NOT NULL DEFAULT ''")


def initialize_database(db_path: Path | str = DATABASE_PATH) -> None:
    """DBファイルとテーブルを作成し、必要なら既存DBを移行する。"""
    try:
        with connect(db_path) as connection, transaction(connection):
            connection.executescript(SCHEMA_SQL)
            _migrate(connection)
    except sqlite3.Error as exc:
        raise DatabaseError("データベースの初期化に失敗しました。") from exc
