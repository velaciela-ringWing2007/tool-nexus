"""スクリプト（.ps1 / .sh）の推測、対になる停止スクリプト、実行ポリシーの付け外し（SPEC 6.1・6.9）のテスト."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from tool_nexus import osdep
from tool_nexus.core.constants import HEALTH_PROCESS, KIND_SCRIPT
from tool_nexus.core.models import ValidationError, build_tool
from tool_nexus.process.control import split_command
from tool_nexus.process.launch_assist import (
    BACKGROUND_SCRIPT_NOTE,
    AssistError,
    has_bypass,
    is_powershell_command,
    paired_stop_script,
    set_bypass,
    suggest_from_file,
    suggest_stop_command,
)


def write(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class TestSuggestScript:
    def test_ps1(self, tmp_path: Path) -> None:
        script = write(tmp_path / "my db" / "start.ps1")
        s = suggest_from_file(script)
        assert s.kind == KIND_SCRIPT
        assert s.health_mode == HEALTH_PROCESS
        assert s.directory == str(tmp_path / "my db")
        assert s.name == "my db"  # スクリプトはフォルダ名
        assert split_command(s.command) == [osdep.POWERSHELL, "-NoProfile", "-File", "start.ps1"]
        assert "-ExecutionPolicy" not in s.command  # 既定では付けない
        assert BACKGROUND_SCRIPT_NOTE in s.notes

    def test_sh(self, tmp_path: Path) -> None:
        s = suggest_from_file(write(tmp_path / "svc" / "up.sh"))
        assert s.kind == KIND_SCRIPT
        assert split_command(s.command) == ["bash", "up.sh"]
        assert s.stop_command == ""
        assert any("Git Bash" in note for note in s.notes) is osdep.IS_WINDOWS

    def test_paired_stop_script(self, tmp_path: Path) -> None:
        start = write(tmp_path / "svc" / "Start-DB.ps1")
        write(tmp_path / "svc" / "stop-db.ps1")
        write(tmp_path / "svc" / "stop-db.sh")  # 拡張子が違うものは対にしない
        s = suggest_from_file(start)
        assert split_command(s.stop_command) == [osdep.POWERSHELL, "-NoProfile", "-File", "stop-db.ps1"]
        assert any("stop-db.ps1" in note for note in s.notes)

    def test_no_pair(self, tmp_path: Path) -> None:
        assert paired_stop_script(write(tmp_path / "run.ps1")) is None
        assert paired_stop_script(write(tmp_path / "start.ps1")) is None

    @pytest.mark.parametrize("name", ["run.bat", "run.cmd"])
    def test_bat_is_still_unsupported(self, tmp_path: Path, name: str) -> None:
        with pytest.raises(AssistError):
            suggest_from_file(write(tmp_path / name))


class TestSuggestStopCommand:
    def test_script_inside_workdir_is_relative(self, tmp_path: Path) -> None:
        stop = write(tmp_path / "scripts" / "stop.sh")
        command, _ = suggest_stop_command(stop, str(tmp_path))
        assert split_command(command) == ["bash", str(Path("scripts") / "stop.sh")]

    def test_script_outside_workdir_is_absolute(self, tmp_path: Path) -> None:
        stop = write(tmp_path / "other" / "stop.ps1")
        command, _ = suggest_stop_command(stop, str(tmp_path / "tool"))
        assert split_command(command)[-1] == str(stop)
        command, _ = suggest_stop_command(stop, "")  # 作業ディレクトリが未入力
        assert split_command(command)[-1] == str(stop)

    def test_python_with_venv_is_rebased_to_workdir(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        write(root / "pyproject.toml")
        (root / ".venv" / "Scripts").mkdir(parents=True)
        (root / ".venv" / "Scripts" / "python.exe").write_bytes(b"")
        (root / ".venv" / "pyvenv.cfg").write_text("home = x\n", encoding="utf-8")
        stop = write(root / "tools" / "stop.py")
        command, _ = suggest_stop_command(stop, str(root / "tools"))
        argv = split_command(command)
        # 作業ディレクトリ（tools）は venv の外なので、python は絶対パス、スクリプトは相対パス
        assert argv == [str(root / ".venv" / "Scripts" / "python.exe"), "stop.py"]

    @pytest.mark.parametrize("name", ["stop.bat", "stop.cmd", "notes.txt"])
    def test_unsupported(self, tmp_path: Path, name: str) -> None:
        with pytest.raises(AssistError):
            suggest_stop_command(write(tmp_path / name), str(tmp_path))

    @pytest.mark.skipif(sys.platform == "win32", reason="実行権限は POSIX のみ")
    def test_executable_is_absolute(self, tmp_path: Path) -> None:
        stop = write(tmp_path / "bin" / "stopper")
        os.chmod(stop, 0o755)
        command, _ = suggest_stop_command(stop, str(tmp_path))
        assert split_command(command) == [str(stop)]


class TestBypass:
    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("powershell -NoProfile -File start.ps1", "powershell -ExecutionPolicy Bypass -NoProfile -File start.ps1"),
            ("pwsh -File a.ps1", "pwsh -ExecutionPolicy Bypass -File a.ps1"),
            ('"C:\\Program Files\\PowerShell\\7\\pwsh.exe" -File a.ps1',
             '"C:\\Program Files\\PowerShell\\7\\pwsh.exe" -ExecutionPolicy Bypass -File a.ps1'),
        ],
    )
    def test_added_right_after_executable(self, command: str, expected: str) -> None:
        added = set_bypass(command, True)
        assert added == expected
        assert has_bypass(added)
        assert set_bypass(added, False) == command
        assert set_bypass(added, True) == added  # 二重に付けない

    def test_removed_ignoring_case(self) -> None:
        assert set_bypass("POWERSHELL.EXE -executionpolicy BYPASS -File a.ps1", False) == "POWERSHELL.EXE -File a.ps1"

    @pytest.mark.parametrize("command", ["bash start.sh", "python app.py -ExecutionPolicy Bypass", "", 'powershell "x'])
    def test_other_commands_are_untouched(self, command: str) -> None:
        assert set_bypass(command, True) == command
        assert set_bypass(command, False) == command
        assert not has_bypass(command)

    def test_is_powershell_command(self) -> None:
        assert is_powershell_command("powershell -File a.ps1")
        assert is_powershell_command("/usr/bin/pwsh -File a.ps1")
        assert not is_powershell_command("powershell_helper.exe")


class TestStopCommandField:
    def test_optional_and_validated(self, tmp_path: Path) -> None:
        base = dict(name="t", directory=str(tmp_path), command="bash up.sh", kind=KIND_SCRIPT)
        assert build_tool(**base).stop_command == ""
        assert build_tool(**base, stop_command="  docker compose down ").stop_command == "docker compose down"
        with pytest.raises(ValidationError):
            build_tool(**base, stop_command='bash "unclosed')

    def test_link_has_no_stop_command(self) -> None:
        tool = build_tool(name="l", directory="", command="", kind="link", target="https://example.com/",
                          stop_command="x")
        assert tool.stop_command == ""
