"""種別 link（SPEC 6.10）と、種別 web の既定コマンド（SPEC 6.1）のテスト."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tool_nexus import osdep
from tool_nexus.core.backup import export_bytes, parse_backup, restore_backup
from tool_nexus.core.constants import HEALTH_NONE, KIND_LINK, LINK_SERVER_PORT
from tool_nexus.core.models import ValidationError, build_tool
from tool_nexus.core.ports import pick_free_port
from tool_nexus.core.repositories import ToolRepository
from tool_nexus.process.control import split_command
from tool_nexus.process.health import Status, ToolHealth, derive_status
from tool_nexus.process.launch_assist import html_title, static_server_command, suggest_from_file


def link(target: str, **kw):
    return build_tool(name="L", directory="ignored", command="ignored", kind=KIND_LINK, target=target, **kw)


class TestBuildLink:
    def test_url(self) -> None:
        tool = link("https://example.com/docs")
        assert tool.kind == KIND_LINK and tool.target == "https://example.com/docs"
        # 起動しないので、作業ディレクトリ・コマンド・ポート・監視は持たない
        assert (tool.directory, tool.command, tool.port, tool.health_mode, tool.autostart) == ("", "", None, HEALTH_NONE, False)

    def test_local_file(self, tmp_path: Path) -> None:
        page = tmp_path / "index.html"
        page.write_text("x", encoding="utf-8")
        assert link(f'"{page}"').target == str(page)

    @pytest.mark.parametrize(
        "target", ["", "   ", "javascript:alert(1)", "file:///C:/x.html", "ftp://example.com", "docs/index.html", "https://"]
    )
    def test_invalid(self, target: str) -> None:
        with pytest.raises(ValidationError):
            link(target, check_directory=False)

    def test_missing_local_is_rejected_on_registration(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError, match="見つかりません"):
            link(str(tmp_path / "missing.html"))
        # 復元（別PCのバックアップ）では存在を確認しない
        assert link(str(tmp_path / "missing.html"), check_directory=False).target


class TestLinkStatus:
    def test_url_and_existing_file(self, tmp_path: Path) -> None:
        page = tmp_path / "a.html"
        page.write_text("x", encoding="utf-8")
        for target in ("https://example.com", str(page), str(tmp_path)):
            health = ToolHealth(link(target), None, derive_status(link(target), None))
            assert health.status is Status.LINK
            assert health.can_stop is False

    def test_missing_file(self, tmp_path: Path) -> None:
        tool = link(str(tmp_path / "gone.html"), check_directory=False)
        assert derive_status(tool, None) is Status.MISSING


class TestSuggest:
    def test_html_becomes_link_named_by_title(self, tmp_path: Path) -> None:
        page = tmp_path / "readme.html"
        page.write_text("<html><head><title> 在庫チェッカー &amp; README </title></head></html>", encoding="utf-8")
        s = suggest_from_file(page)
        assert s.kind == KIND_LINK
        assert s.name == "在庫チェッカー & README"
        assert s.target == str(page)
        assert s.health_mode == HEALTH_NONE

    def test_pdf_uses_file_name(self, tmp_path: Path) -> None:
        pdf = tmp_path / "manual.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        assert suggest_from_file(pdf).name == "manual"
        assert html_title(pdf) == ""


class TestStaticServerCommand:
    def test_uses_os_default_python(self) -> None:
        first = osdep.DEFAULT_PYTHONS[0]
        command = static_server_command(which=lambda n: n if n == first else None)
        assert command == f"{first} -m http.server {{port}} --bind 127.0.0.1"

    def test_falls_back_to_own_python(self) -> None:
        import sys

        argv = split_command(static_server_command(which=lambda n: None))
        assert argv[0] == sys.executable
        assert argv[1:] == ["-m", "http.server", "{port}", "--bind", "127.0.0.1"]


class TestStorage:
    def test_round_trip(self, tmp_path: Path) -> None:
        repo = ToolRepository(tmp_path / "a.sqlite3")
        repo.initialize()
        created = repo.create(link("https://example.com/x"))
        assert repo.get_by_id(created.id).target == "https://example.com/x"

        other = ToolRepository(tmp_path / "b.sqlite3")
        other.initialize()
        restore_backup(other, parse_backup(export_bytes(repo)))
        [restored] = other.list_all()
        assert (restored.kind, restored.target) == (KIND_LINK, "https://example.com/x")
        assert json.loads(export_bytes(repo))["tools"][0]["target"] == "https://example.com/x"

    def test_migrates_database_without_target(self, tmp_path: Path) -> None:
        path = tmp_path / "old.sqlite3"
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE tools (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'streamlit', directory TEXT NOT NULL, command TEXT NOT NULL,
                port INTEGER, health_mode TEXT NOT NULL DEFAULT 'http', log_path TEXT NOT NULL DEFAULT '',
                autostart INTEGER NOT NULL DEFAULT 0, description TEXT NOT NULL DEFAULT '',
                sort_order INTEGER NOT NULL DEFAULT 0, last_pid INTEGER, last_pid_created_at TEXT,
                last_started_at TEXT, last_stopped_at TEXT, last_seen_at TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            INSERT INTO tools (name, directory, command, port, created_at, updated_at)
            VALUES ('既存', '/x', 'python a.py', 8600, 'c', 'u');
            """
        )
        connection.commit()
        connection.close()
        repo = ToolRepository(path)
        repo.initialize()
        [tool] = repo.list_all()
        assert tool.name == "既存" and tool.target == ""


def test_link_server_port_is_never_auto_assigned() -> None:
    with pytest.raises(Exception):
        pick_free_port(set(), LINK_SERVER_PORT, LINK_SERVER_PORT, is_free=lambda p: True)
