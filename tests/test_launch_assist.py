"""launch_assist のテスト（一時ディレクトリに疑似プロジェクトを作る）."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from constants import HEALTH_HTTP, HEALTH_PROCESS, KIND_EXE, KIND_PYTHON, KIND_STREAMLIT, KIND_WEB
from launch_assist import (
    AssistError,
    detect_framework,
    find_project_root,
    find_venv,
    pick_file,
    pick_folder,
    suggest_from_file,
)
from process_utils import split_command


def no_py(name: str) -> str | None:
    return None


def has_py(name: str) -> str | None:
    return r"C:\Windows\py.exe" if name == "py" else None


def make_venv(folder: Path) -> Path:
    (folder / "Scripts").mkdir(parents=True)
    (folder / "Scripts" / "python.exe").write_bytes(b"")
    (folder / "pyvenv.cfg").write_text("home = C:\\Python313\n", encoding="utf-8")
    return folder


def write(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ----------------------------------------------------------------------
# ルート・venv
# ----------------------------------------------------------------------
class TestFindProjectRoot:
    def test_marker_in_parent(self, tmp_path: Path) -> None:
        write(tmp_path / "proj" / "pyproject.toml")
        script = write(tmp_path / "proj" / "src" / "app.py")
        assert find_project_root(script) == tmp_path / "proj"

    def test_no_marker_uses_file_folder(self, tmp_path: Path) -> None:
        script = write(tmp_path / "loose" / "tool.py")
        # tmp_path の上位に目印があっても、深さ制限の範囲で見つからなければファイルのフォルダ
        root = find_project_root(script)
        assert root in {tmp_path / "loose", *[p for p in script.parents]}

    def test_venv_folder_is_a_marker(self, tmp_path: Path) -> None:
        make_venv(tmp_path / "proj" / ".venv")
        script = write(tmp_path / "proj" / "app.py")
        assert find_project_root(script) == tmp_path / "proj"


class TestFindVenv:
    def test_dot_venv(self, tmp_path: Path) -> None:
        venv = make_venv(tmp_path / ".venv")
        assert find_venv(write(tmp_path / "app.py"), tmp_path) == venv

    def test_other_name_with_pyvenv_cfg(self, tmp_path: Path) -> None:
        venv = make_venv(tmp_path / "myenv312")
        assert find_venv(write(tmp_path / "app.py"), tmp_path) == venv

    def test_folder_without_pyvenv_cfg_is_ignored(self, tmp_path: Path) -> None:
        (tmp_path / "venv" / "Scripts").mkdir(parents=True)
        (tmp_path / "venv" / "Scripts" / "python.exe").write_bytes(b"")
        assert find_venv(write(tmp_path / "app.py"), tmp_path) is None

    def test_preferred_name_wins(self, tmp_path: Path) -> None:
        make_venv(tmp_path / "aaa")
        venv = make_venv(tmp_path / ".venv")
        assert find_venv(write(tmp_path / "app.py"), tmp_path) == venv

    def test_venv_at_root_from_subfolder(self, tmp_path: Path) -> None:
        venv = make_venv(tmp_path / ".venv")
        assert find_venv(write(tmp_path / "src" / "pkg" / "app.py"), tmp_path) == venv

    def test_does_not_look_above_root(self, tmp_path: Path) -> None:
        make_venv(tmp_path / ".venv")
        root = tmp_path / "proj"
        write(root / "pyproject.toml")
        assert find_venv(write(root / "app.py"), root) is None


# ----------------------------------------------------------------------
# フレームワーク判定
# ----------------------------------------------------------------------
class TestDetectFramework:
    @pytest.mark.parametrize(
        "source", ["import streamlit as st\n", "from streamlit import title\n", "  import streamlit\n"]
    )
    def test_streamlit(self, source: str) -> None:
        assert detect_framework(source) == ("streamlit", "")

    def test_streamlit_in_comment_or_string_is_ignored(self) -> None:
        assert detect_framework("# import streamlit\nprint('import streamlit')\n")[0] == "python"

    def test_flask(self) -> None:
        assert detect_framework("from flask import Flask\napp = Flask(__name__)\n") == ("flask", "")

    def test_fastapi_variable_name(self) -> None:
        assert detect_framework("from fastapi import FastAPI\napi = FastAPI()\n") == ("fastapi", "api")

    def test_fastapi_default_variable(self) -> None:
        assert detect_framework("x = make(FastAPI(title='t'))\n") == ("fastapi", "app")

    def test_plain(self) -> None:
        assert detect_framework("print('hello')\n") == ("python", "")


# ----------------------------------------------------------------------
# 推測全体
# ----------------------------------------------------------------------
class TestSuggestFromFile:
    def test_streamlit_with_venv(self, tmp_path: Path) -> None:
        root = tmp_path / "在庫チェッカー"
        make_venv(root / ".venv")
        script = write(root / "app.py", "import streamlit as st\n")
        s = suggest_from_file(script, which=no_py)
        assert s.name == "在庫チェッカー"
        assert s.directory == str(root)
        assert s.kind == KIND_STREAMLIT
        assert s.health_mode == HEALTH_HTTP
        assert s.command == r".venv\Scripts\python.exe -m streamlit run app.py"

    def test_paths_with_spaces_are_quoted(self, tmp_path: Path) -> None:
        root = tmp_path / "my tools"
        make_venv(root / "my env")
        script = write(root / "sub dir" / "main app.py", "print(1)\n")
        write(root / "requirements.txt")
        s = suggest_from_file(script, which=no_py)
        assert split_command(s.command) == [
            r"my env\Scripts\python.exe", r"sub dir\main app.py"
        ]
        assert s.kind == KIND_PYTHON
        assert s.health_mode == HEALTH_PROCESS

    def test_flask_uses_port_placeholder(self, tmp_path: Path) -> None:
        make_venv(tmp_path / ".venv")
        script = write(tmp_path / "web.py", "from flask import Flask\napp = Flask(__name__)\n")
        s = suggest_from_file(script, which=no_py)
        assert s.kind == KIND_WEB
        assert s.health_mode == HEALTH_HTTP
        assert s.command.endswith("-m flask --app web.py run --host 127.0.0.1 --port {port}")

    def test_fastapi_module_path(self, tmp_path: Path) -> None:
        make_venv(tmp_path / ".venv")
        write(tmp_path / "pyproject.toml")
        script = write(tmp_path / "app" / "main.py", "from fastapi import FastAPI\napi = FastAPI()\n")
        s = suggest_from_file(script, which=no_py)
        assert "-m uvicorn app.main:api --host 127.0.0.1 --port {port}" in s.command

    def test_uv_without_venv(self, tmp_path: Path) -> None:
        write(tmp_path / "uv.lock")
        script = write(tmp_path / "app.py", "import streamlit\n")
        s = suggest_from_file(script, which=no_py)
        assert s.command == "uv run python -m streamlit run app.py"
        assert any("uv" in note for note in s.notes)

    def test_venv_preferred_over_uv(self, tmp_path: Path) -> None:
        write(tmp_path / "uv.lock")
        make_venv(tmp_path / ".venv")
        s = suggest_from_file(write(tmp_path / "app.py"), which=no_py)
        assert s.command.startswith(r".venv\Scripts\python.exe")

    def test_system_python_prefers_py_launcher(self, tmp_path: Path) -> None:
        write(tmp_path / "requirements.txt")
        script = write(tmp_path / "tool.py", "print(1)\n")
        assert suggest_from_file(script, which=has_py).command == "py tool.py"
        assert suggest_from_file(script, which=no_py).command == "python tool.py"

    def test_exe(self, tmp_path: Path) -> None:
        exe = write(tmp_path / "My Tool" / "tool.exe")
        s = suggest_from_file(exe)
        assert s.kind == KIND_EXE
        assert s.directory == str(tmp_path / "My Tool")
        assert split_command(s.command) == [str(exe)]
        assert s.health_mode == HEALTH_PROCESS

    @pytest.mark.parametrize("name", ["run.bat", "run.cmd", "notes.txt"])
    def test_unsupported(self, tmp_path: Path, name: str) -> None:
        with pytest.raises(AssistError):
            suggest_from_file(write(tmp_path / name))

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(AssistError):
            suggest_from_file(tmp_path / "nope.py")


# ----------------------------------------------------------------------
# ダイアログ（PowerShell はフェイク）
# ----------------------------------------------------------------------
class FakeRunner:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, "")


class TestDialogs:
    def test_pick_file(self, tmp_path: Path) -> None:
        runner = FakeRunner(stdout=f"{tmp_path}\\app.py\r\n")
        assert pick_file(str(tmp_path), runner=runner) == tmp_path / "app.py"
        argv, kwargs = runner.calls[0]
        assert "-STA" in argv
        assert "OpenFileDialog" in argv[-1]
        assert kwargs["timeout"] >= 60

    def test_cancel(self) -> None:
        assert pick_file(runner=FakeRunner(stdout="\r\n")) is None
        assert pick_folder(runner=FakeRunner(stdout="")) is None

    def test_initial_dir_is_escaped(self, tmp_path: Path) -> None:
        folder = tmp_path / "it's"
        folder.mkdir()
        runner = FakeRunner()
        pick_folder(str(folder), runner=runner)
        assert "it''s" in runner.calls[0][0][-1]

    def test_missing_initial_dir_is_ignored(self, tmp_path: Path) -> None:
        runner = FakeRunner()
        pick_file(str(tmp_path / "missing" / "x.py"), runner=runner)
        assert "InitialDirectory" not in runner.calls[0][0][-1]

    def test_failure(self) -> None:
        with pytest.raises(AssistError):
            pick_file(runner=FakeRunner(returncode=1))
