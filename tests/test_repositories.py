"""repositories / models のテスト（一時SQLite DBを使用）."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from tool_nexus.core.constants import (
    DEFAULT_SETTINGS,
    HEALTH_HTTP,
    HEALTH_NONE,
    HEALTH_PROCESS,
    KIND_EXE,
    KIND_PYTHON,
    KIND_STREAMLIT,
    KIND_WEB,
)
from tool_nexus.core.database import connect
from tool_nexus.core.models import ValidationError, build_tool, can_auto_assign_port, default_health_mode
from tool_nexus.core.repositories import DuplicatePortError, ToolNotFoundError, ToolRepository

COMMAND = r".venv\Scripts\python.exe -m streamlit run app.py"
STARTED = "2026-09-27T13:45:01+09:00"


@pytest.fixture()
def repo(tmp_path: Path) -> ToolRepository:
    repository = ToolRepository(tmp_path / "test.sqlite3")
    repository.initialize()
    return repository


@pytest.fixture()
def workdir(tmp_path: Path) -> Path:
    path = tmp_path / "tool-a"
    path.mkdir()
    return path


def make_tool(workdir: Path, **overrides):
    params = {"name": "在庫チェッカー", "directory": str(workdir), "command": COMMAND, "port": 8502}
    return build_tool(**(params | overrides))


# ----------------------------------------------------------------------
# 検証
# ----------------------------------------------------------------------
class TestBuildTool:
    def test_defaults(self, workdir: Path) -> None:
        tool = make_tool(workdir)
        assert tool.kind == "streamlit"
        assert tool.health_mode == HEALTH_HTTP
        assert tool.port == 8502

    def test_http_requires_port(self, workdir: Path) -> None:
        with pytest.raises(ValidationError, match="ポートが必要"):
            make_tool(workdir, port="")

    def test_http_without_port_is_not_silently_switched(self, workdir: Path) -> None:
        with pytest.raises(ValidationError):
            make_tool(workdir, port=None, health_mode=HEALTH_HTTP)

    @pytest.mark.parametrize("mode", [HEALTH_PROCESS, HEALTH_NONE])
    def test_other_modes_allow_empty_port(self, workdir: Path, mode: str) -> None:
        assert make_tool(workdir, port="", health_mode=mode).port is None

    def test_exe_defaults_to_process_mode(self, workdir: Path) -> None:
        assert default_health_mode(KIND_EXE) == HEALTH_PROCESS
        tool = make_tool(workdir, kind=KIND_EXE, port=None, command="tool.exe")
        assert tool.health_mode == HEALTH_PROCESS

    @pytest.mark.parametrize(
        ("kind", "mode"),
        [(KIND_WEB, HEALTH_HTTP), (KIND_PYTHON, HEALTH_PROCESS), (KIND_EXE, HEALTH_PROCESS)],
    )
    def test_default_health_mode_by_kind(self, kind: str, mode: str) -> None:
        assert default_health_mode(kind) == mode

    def test_port_placeholder_requires_port(self, workdir: Path) -> None:
        with pytest.raises(ValidationError, match=r"\{port\}"):
            make_tool(
                workdir, kind=KIND_PYTHON, command="python app.py --port {port}",
                port=None, health_mode=HEALTH_PROCESS,
            )

    @pytest.mark.parametrize(
        ("kind", "command", "expected"),
        [
            (KIND_STREAMLIT, "streamlit run app.py", True),
            (KIND_WEB, "python -m flask run --port {port}", True),
            (KIND_PYTHON, "python app.py --port {port}", True),
            (KIND_WEB, "python -m flask run", False),
            (KIND_EXE, "tool.exe", False),
        ],
    )
    def test_can_auto_assign_port(self, kind: str, command: str, expected: bool) -> None:
        assert can_auto_assign_port(kind, command) is expected

    def test_exe_with_explicit_http_still_requires_port(self, workdir: Path) -> None:
        with pytest.raises(ValidationError):
            make_tool(workdir, kind=KIND_EXE, port=None, health_mode=HEALTH_HTTP)

    @pytest.mark.parametrize("port", ["abc", 0, 70000, "1.5", True])
    def test_invalid_port(self, workdir: Path, port) -> None:
        with pytest.raises(ValidationError):
            make_tool(workdir, port=port)

    def test_port_from_string(self, workdir: Path) -> None:
        assert make_tool(workdir, port=" 8600 ").port == 8600

    @pytest.mark.parametrize("field", ["name", "command", "directory"])
    def test_required_fields(self, workdir: Path, field: str) -> None:
        with pytest.raises(ValidationError):
            make_tool(workdir, **{field: "  "})

    def test_directory_must_exist(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError, match="見つかりません"):
            make_tool(tmp_path / "missing")

    def test_directory_check_can_be_skipped(self, tmp_path: Path) -> None:
        tool = make_tool(tmp_path / "missing", check_directory=False)
        assert tool.directory.endswith("missing")

    def test_unknown_kind_and_mode(self, workdir: Path) -> None:
        with pytest.raises(ValidationError):
            make_tool(workdir, kind="docker")
        with pytest.raises(ValidationError):
            make_tool(workdir, health_mode="ping")

    def test_unclosed_quote_in_command(self, workdir: Path) -> None:
        with pytest.raises(ValidationError, match="解釈できません"):
            make_tool(workdir, command='"C:\\Program Files\\x.exe')


# ----------------------------------------------------------------------
# CRUD
# ----------------------------------------------------------------------
class TestCrud:
    def test_create_and_get(self, repo: ToolRepository, workdir: Path) -> None:
        created = repo.create(make_tool(workdir, description="説明", autostart=True))
        assert created.id is not None
        assert created.created_at and created.updated_at

        loaded = repo.get_by_id(created.id)
        assert loaded is not None
        assert loaded.name == "在庫チェッカー"
        assert loaded.command == COMMAND
        assert loaded.port == 8502
        assert loaded.autostart is True
        assert loaded.description == "説明"
        assert loaded.last_pid is None
        assert loaded.last_pid_created_at is None

    def test_list_all_order(self, repo: ToolRepository, workdir: Path) -> None:
        repo.create(make_tool(workdir, name="b", port=8501, sort_order=1))
        repo.create(make_tool(workdir, name="a", port=8502, sort_order=1))
        repo.create(make_tool(workdir, name="z", port=8503, sort_order=0))
        assert [t.name for t in repo.list_all()] == ["z", "a", "b"]
        assert repo.count() == 3

    def test_nullable_port(self, repo: ToolRepository, workdir: Path) -> None:
        a = repo.create(make_tool(workdir, name="a", port=None, health_mode=HEALTH_PROCESS))
        b = repo.create(make_tool(workdir, name="b", port=None, health_mode=HEALTH_NONE))
        assert repo.get_by_id(a.id).port is None
        assert repo.get_by_id(b.id).port is None
        assert repo.used_ports() == set()

    def test_update(self, repo: ToolRepository, workdir: Path) -> None:
        created = repo.create(make_tool(workdir))
        updated = repo.update(replace(created, name="新しい名前", port=8600))
        assert updated.name == "新しい名前"
        assert updated.port == 8600
        assert updated.created_at == created.created_at

    def test_update_does_not_touch_start_record(
        self, repo: ToolRepository, workdir: Path
    ) -> None:
        created = repo.create(make_tool(workdir))
        repo.record_start(created.id, pid=100, created_at=STARTED)
        stale = replace(created, name="x")  # last_pid は None のまま
        updated = repo.update(stale)
        assert updated.last_pid == 100
        assert updated.last_pid_created_at == STARTED

    def test_update_missing(self, repo: ToolRepository, workdir: Path) -> None:
        with pytest.raises(ToolNotFoundError):
            repo.update(replace(make_tool(workdir), id=999))

    def test_delete(self, repo: ToolRepository, workdir: Path) -> None:
        created = repo.create(make_tool(workdir))
        assert repo.delete(created.id) is True
        assert repo.get_by_id(created.id) is None
        assert repo.delete(created.id) is False


class TestPortDuplicates:
    def test_create_with_used_port(self, repo: ToolRepository, workdir: Path) -> None:
        first = repo.create(make_tool(workdir, name="A"))
        with pytest.raises(DuplicatePortError) as excinfo:
            repo.create(make_tool(workdir, name="B"))
        assert excinfo.value.existing.id == first.id
        assert "A" in str(excinfo.value)
        assert repo.count() == 1

    def test_update_to_used_port(self, repo: ToolRepository, workdir: Path) -> None:
        repo.create(make_tool(workdir, name="A", port=8501))
        b = repo.create(make_tool(workdir, name="B", port=8502))
        with pytest.raises(DuplicatePortError):
            repo.update(replace(b, port=8501))
        assert repo.get_by_id(b.id).port == 8502

    def test_update_keeping_own_port(self, repo: ToolRepository, workdir: Path) -> None:
        a = repo.create(make_tool(workdir, name="A"))
        assert repo.update(replace(a, description="x")).port == a.port

    def test_find_and_used_ports(self, repo: ToolRepository, workdir: Path) -> None:
        a = repo.create(make_tool(workdir, name="A", port=8501))
        repo.create(make_tool(workdir, name="B", port=8502))
        assert repo.find_by_port(8501).id == a.id
        assert repo.find_by_port(8501, exclude_id=a.id) is None
        assert repo.find_by_port(8999) is None
        assert repo.used_ports() == {8501, 8502}


# ----------------------------------------------------------------------
# 起動記録
# ----------------------------------------------------------------------
class TestStartRecord:
    def test_record_start_saves_pair(self, repo: ToolRepository, workdir: Path) -> None:
        tool = repo.create(make_tool(workdir))
        repo.record_start(tool.id, pid=4321, created_at=STARTED)
        loaded = repo.get_by_id(tool.id)
        assert (loaded.last_pid, loaded.last_pid_created_at) == (4321, STARTED)
        assert loaded.last_started_at

    def test_record_start_without_created_at_saves_neither(
        self, repo: ToolRepository, workdir: Path
    ) -> None:
        tool = repo.create(make_tool(workdir))
        repo.record_start(tool.id, pid=1, created_at=STARTED)
        repo.record_start(tool.id, pid=4321, created_at=None)
        loaded = repo.get_by_id(tool.id)
        assert loaded.last_pid is None
        assert loaded.last_pid_created_at is None
        assert loaded.last_started_at  # 起動操作の記録は残す

    def test_clear_pid_clears_pair(self, repo: ToolRepository, workdir: Path) -> None:
        tool = repo.create(make_tool(workdir))
        repo.record_start(tool.id, pid=4321, created_at=STARTED)
        repo.clear_pid(tool.id)
        loaded = repo.get_by_id(tool.id)
        assert loaded.last_pid is None
        assert loaded.last_pid_created_at is None
        assert loaded.last_started_at

    def test_mark_seen(self, repo: ToolRepository, workdir: Path) -> None:
        tool = repo.create(make_tool(workdir))
        repo.mark_seen(tool.id)
        assert repo.get_by_id(tool.id).last_seen_at

    def test_record_for_missing_tool(self, repo: ToolRepository) -> None:
        with pytest.raises(ToolNotFoundError):
            repo.record_start(999, pid=1, created_at=STARTED)


# ----------------------------------------------------------------------
# 設定
# ----------------------------------------------------------------------
class TestSettings:
    def test_defaults(self, repo: ToolRepository) -> None:
        assert repo.get_settings() == DEFAULT_SETTINGS
        assert repo.get_setting("health_interval") == "60s"

    def test_set_and_get(self, repo: ToolRepository) -> None:
        repo.set_setting("port_range_low", "8600")
        repo.set_setting("port_range_low", "8700")
        assert repo.get_setting("port_range_low") == "8700"
        assert repo.get_settings()["port_range_high"] == "8999"

    def test_unknown_key(self, repo: ToolRepository) -> None:
        with pytest.raises(KeyError):
            repo.set_setting("nope", "1")
        with pytest.raises(KeyError):
            repo.get_setting("nope")

    def test_unknown_stored_keys_are_ignored(self, repo: ToolRepository) -> None:
        with connect(repo.db_path) as connection:
            connection.execute("INSERT INTO settings (key, value) VALUES ('legacy', 'x')")
            connection.commit()
        assert "legacy" not in repo.get_settings()


def test_initialize_is_idempotent(repo: ToolRepository, workdir: Path) -> None:
    repo.create(make_tool(workdir))
    repo.initialize()
    assert repo.count() == 1
