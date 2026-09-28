"""backup のテスト（一時SQLite DBを使用）."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tool_nexus.core.backup import (
    MODE_APPEND,
    MODE_REPLACE,
    SCHEMA_VERSION,
    BackupError,
    export_backup,
    export_bytes,
    parse_backup,
    restore_backup,
)
from tool_nexus.core.constants import HEALTH_PROCESS, KIND_EXE
from tool_nexus.core.models import build_tool
from tool_nexus.core.repositories import ToolRepository

STARTED = "2026-09-27T13:45:01+09:00"


@pytest.fixture()
def repo(tmp_path: Path) -> ToolRepository:
    repository = ToolRepository(tmp_path / "a.sqlite3")
    repository.initialize()
    return repository


@pytest.fixture()
def other(tmp_path: Path) -> ToolRepository:
    repository = ToolRepository(tmp_path / "b.sqlite3")
    repository.initialize()
    return repository


def add(repo: ToolRepository, name: str, port: int | None, directory: str = "/dev/x", **kw):
    return repo.create(
        build_tool(name=name, directory=directory, command="python -m streamlit run app.py",
                   port=port, check_directory=False, **kw)
    )


class TestExport:
    def test_no_runtime_records_or_ids(self, repo: ToolRepository) -> None:
        tool = add(repo, "A", 8502, autostart=True, description="説明")
        repo.record_start(tool.id, pid=123, created_at=STARTED)
        data = export_backup(repo)
        assert data["app"] == "TOOL NEXUS"
        assert data["schemaVersion"] == SCHEMA_VERSION
        [item] = data["tools"]
        assert item == {
            "name": "A", "kind": "streamlit", "directory": "/dev/x",
            "command": "python -m streamlit run app.py", "port": 8502, "healthMode": "http",
            "logPath": "", "autostart": True, "description": "説明", "sortOrder": 0, "target": "", "group": "",
        }
        text = export_bytes(repo).decode("utf-8")
        assert "123" not in text and "last" not in text.lower() and '"id"' not in text

    def test_round_trip(self, repo: ToolRepository, other: ToolRepository) -> None:
        add(repo, "A", 8502, sort_order=2)
        add(repo, "B", None, kind=KIND_EXE, health_mode=HEALTH_PROCESS)
        repo.set_settings({"health_interval": "30s", "reserved_ports": "8501,8600"})

        parsed = parse_backup(export_bytes(repo))
        summary = restore_backup(other, parsed, include_settings=True)
        assert summary.added == ["B", "A"]  # 表示順（B=0, A=2）どおり
        assert summary.settings_restored
        strip = lambda t: {k: v for k, v in export_backup(t).items() if k != "exportedAt"}  # noqa: E731
        assert strip(other) == strip(repo)


class TestParse:
    def test_invalid_rows_are_reported(self) -> None:
        text = json.dumps(
            {
                "schemaVersion": 1,
                "tools": [
                    {"name": "ok", "directory": "/x", "command": "python a.py", "port": 8600},
                    {"name": "http no port", "directory": "/x", "command": "python a.py", "kind": "web"},
                    "garbage",
                    {"name": "", "directory": "/x", "command": "c"},
                ],
            }
        )
        parsed = parse_backup(text)
        assert [t.name for t in parsed.tools] == ["ok"]
        assert len(parsed.problems) == 3
        assert "http no port" in parsed.problems[0]

    def test_missing_directory_is_allowed(self) -> None:
        text = json.dumps({"tools": [{"name": "a", "directory": "/no/such/dir", "command": "c", "port": 8600}]})
        assert len(parse_backup(text).tools) == 1

    def test_invalid_settings_are_reported_not_applied(self) -> None:
        parsed = parse_backup(json.dumps({"tools": [], "settings": {"health_interval": "abc"}}))
        assert parsed.settings == {}
        assert "死活監視の間隔" in parsed.settings_problem

    @pytest.mark.parametrize("text", ["{oops", "[]", '{"tools": 1}'])
    def test_broken(self, text: str) -> None:
        with pytest.raises(BackupError):
            parse_backup(text)

    def test_newer_schema_is_rejected(self) -> None:
        with pytest.raises(BackupError, match="schemaVersion"):
            parse_backup(json.dumps({"schemaVersion": SCHEMA_VERSION + 1, "tools": []}))

    def test_utf8_bom(self) -> None:
        raw = "﻿" + json.dumps({"tools": []})
        assert parse_backup(raw.encode("utf-8")).tools == []


class TestRestore:
    def test_append_skips_same_name_and_directory(self, repo: ToolRepository) -> None:
        add(repo, "A", 8502, directory="/dev/a")
        parsed = parse_backup(json.dumps({"tools": [
            {"name": "a", "directory": "/dev/a/", "command": "c", "port": 8700},  # 大文字小文字・末尾区切りの違いも同一
            {"name": "C", "directory": "/dev/c", "command": "c", "port": 8701},
        ]}))
        summary = restore_backup(repo, parsed, mode=MODE_APPEND)
        assert summary.added == ["C"]
        assert len(summary.skipped) == 1
        assert repo.count() == 2

    def test_append_skips_port_collision(self, repo: ToolRepository) -> None:
        add(repo, "A", 8502)
        parsed = parse_backup(json.dumps({"tools": [{"name": "B", "directory": "/y", "command": "c", "port": 8502}]}))
        summary = restore_backup(repo, parsed, mode=MODE_APPEND)
        assert summary.added == []
        assert "8502" in summary.skipped[0]

    def test_replace(self, repo: ToolRepository) -> None:
        add(repo, "old", 8502)
        parsed = parse_backup(json.dumps({"tools": [
            {"name": "N1", "directory": "/1", "command": "c", "port": 8502},
            {"name": "N2", "directory": "/2", "command": "c", "port": 8502},
        ]}))
        summary = restore_backup(repo, parsed, mode=MODE_REPLACE)
        assert [t.name for t in repo.list_all()] == ["N1"]
        assert summary.added == ["N1"] and len(summary.skipped) == 1

    def test_settings_only_when_requested(self, repo: ToolRepository) -> None:
        parsed = parse_backup(json.dumps({"tools": [], "settings": {"health_interval": "2m"}}))
        restore_backup(repo, parsed)
        assert repo.get_setting("health_interval") == "60s"
        restore_backup(repo, parsed, include_settings=True)
        assert repo.get_setting("health_interval") == "120s"


class TestRepositoryBulk:
    def test_update_order_matches_by_id(self, repo: ToolRepository) -> None:
        a, b = add(repo, "A", 8501), add(repo, "B", 8502)
        repo.update_order([(b.id, 0, True, "在庫"), (a.id, 5, False, "")])
        assert [t.name for t in repo.list_all()] == ["B", "A"]
        assert repo.get_by_id(b.id).autostart is True
        assert repo.get_by_id(b.id).group_name == "在庫"
        assert repo.group_names() == ["在庫"]

    def test_delete_many(self, repo: ToolRepository) -> None:
        a, _, c = add(repo, "A", 8501), add(repo, "B", 8502), add(repo, "C", 8503)
        assert repo.delete_many([a.id, c.id, 999]) == 2
        assert [t.name for t in repo.list_all()] == ["B"]
        assert repo.delete_many([]) == 0
