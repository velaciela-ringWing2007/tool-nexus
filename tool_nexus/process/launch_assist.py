"""ファイル選択と起動方式の推測（SPEC 6.9）.

パスやコマンドの手入力をなくすための補助。推測はフォームの入力欄に入れるだけで、
最終的なコマンドはユーザーが確認してから登録する（推測は外れうる）。

* ブラウザはローカルファイルのパスを渡せないため、サーバー側（同じPC）から
  OS標準のダイアログを出す（Windows: PowerShell の Windows Forms、Linux: zenity / kdialog）
* ファイルの中身は読むだけで、実行はしない
"""

from __future__ import annotations

import html
import re
import shlex
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from tool_nexus import osdep
from tool_nexus.core.constants import (
    KIND_EXE,
    KIND_LINK,
    KIND_PYTHON,
    KIND_SCRIPT,
    KIND_STREAMLIT,
    KIND_WEB,
    LINK_SUFFIXES,
    PORT_PLACEHOLDER,
    SCRIPT_SUFFIXES,
)
from tool_nexus.core.models import default_health_mode
from tool_nexus.process.base import ProcessQueryError
from tool_nexus.process.control import split_command

# プロジェクトのルートとみなす目印
PROJECT_MARKERS: tuple[str, ...] = (
    "pyproject.toml",
    "uv.lock",
    "requirements.txt",
    ".venv",
    ".git",
)
# venv のフォルダ名として優先する順。これ以外の名前でも pyvenv.cfg があれば venv とみなす。
PREFERRED_VENV_NAMES: tuple[str, ...] = (".venv", "venv", "env")
MAX_ROOT_DEPTH = 8
MAX_READ_BYTES = 512 * 1024


FRAMEWORK_STREAMLIT = "streamlit"
FRAMEWORK_FLASK = "flask"
FRAMEWORK_FASTAPI = "fastapi"
FRAMEWORK_NONE = "python"

_STREAMLIT_IMPORT = re.compile(r"^\s*(?:import\s+streamlit\b|from\s+streamlit\b)", re.MULTILINE)
_FASTAPI_ASSIGN = re.compile(r"^\s*([A-Za-z_]\w*)\s*=\s*FastAPI\s*\(", re.MULTILINE)
_FASTAPI_CALL = re.compile(r"\bFastAPI\s*\(")
_FLASK_CALL = re.compile(r"\bFlask\s*\(")


class AssistError(ValueError):
    """選択されたファイルから推測できない場合に送出する例外."""


@dataclass(slots=True)
class Suggestion:
    """登録フォームへ入れる推測結果."""

    name: str
    directory: str
    command: str
    kind: str
    health_mode: str
    notes: list[str] = field(default_factory=list)
    target: str = ""
    stop_command: str = ""


# ----------------------------------------------------------------------
# 推測
# ----------------------------------------------------------------------
def find_project_root(file: Path) -> Path:
    """ファイルの場所から上へたどり、目印のある最初のフォルダを返す。無ければファイルのフォルダ。"""
    start = file.parent
    for depth, folder in enumerate([start, *start.parents]):
        if depth >= MAX_ROOT_DEPTH:
            break
        if any((folder / marker).exists() for marker in PROJECT_MARKERS):
            return folder
    return start


# venv 内の python の場所（Windows / Linux）
VENV_PYTHONS: tuple[tuple[str, ...], ...] = (("Scripts", "python.exe"), ("bin", "python"))


def venv_python(folder: Path) -> Path | None:
    """venv なら中の python を返す。pyvenv.cfg が無いフォルダは venv とみなさない。"""
    if not (folder / "pyvenv.cfg").is_file():
        return None
    for parts in VENV_PYTHONS:
        candidate = folder.joinpath(*parts)
        if candidate.is_file():
            return candidate
    return None


def is_venv(folder: Path) -> bool:
    return venv_python(folder) is not None


def find_venv(file: Path, root: Path) -> Path | None:
    """ファイルの場所からルートまでをたどり、最初に見つかった venv を返す。"""
    folders = [file.parent, *file.parent.parents]
    for folder in folders:
        for name in PREFERRED_VENV_NAMES:
            if is_venv(folder / name):
                return folder / name
        try:
            others = sorted(p for p in folder.iterdir() if p.is_dir() and p.name not in PREFERRED_VENV_NAMES)
        except OSError:
            others = []
        for candidate in others:
            if is_venv(candidate):
                return candidate
        if folder == root:
            break
    return None


def detect_framework(source: str) -> tuple[str, str]:
    """ソースの中身から (フレームワーク, FastAPI のアプリ変数名) を推測する。"""
    if _STREAMLIT_IMPORT.search(source):
        return FRAMEWORK_STREAMLIT, ""
    match = _FASTAPI_ASSIGN.search(source)
    if match:
        return FRAMEWORK_FASTAPI, match.group(1)
    if _FASTAPI_CALL.search(source):
        return FRAMEWORK_FASTAPI, "app"
    if _FLASK_CALL.search(source):
        return FRAMEWORK_FLASK, ""
    return FRAMEWORK_NONE, ""


_POSIX_SAFE = re.compile(r"[\w@%+=:,./{}-]+")


def quote(token: str, *, posix: bool | None = None) -> str:
    """トークンを必要なときだけクォートする（split_command で元に戻せる形）。"""
    if posix is None:
        posix = osdep.POSIX_SPLIT
    if posix:
        # shlex.quote は {port} まで囲んでしまう（動作はするが読みにくい）ため、安全な文字だけなら囲まない
        return token if _POSIX_SAFE.fullmatch(token) else shlex.quote(token)
    return f'"{token}"' if any(ch.isspace() for ch in token) else token


def relative_to_root(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def python_launcher(
    file: Path, root: Path, *, which: Callable[[str], str | None] = shutil.which
) -> tuple[list[str], list[str]]:
    """使う Python の起動トークンと、ユーザーへの注記を返す。"""
    venv = find_venv(file, root)
    python = venv_python(venv) if venv is not None else None
    if venv is not None and python is not None:
        return [relative_to_root(python, root)], [
            f"venv を使います: {relative_to_root(venv, root)}"
        ]
    if (root / "uv.lock").is_file():
        return ["uv", "run", "python"], [
            "uv.lock があり venv が無いため uv run を使います。"
            "初回は依存関係の同期で起動に時間がかかることがあります"
            "（先に uv sync で .venv を作っておくと、そちらを直接使います）。"
        ]
    candidates = osdep.DEFAULT_PYTHONS
    launcher = next((name for name in candidates if which(name)), candidates[-1])
    return [launcher], [f"venv が見つからないため {launcher} を使います。必要ならコマンドを修正してください。"]


def module_path(script: Path, root: Path) -> str:
    """uvicorn に渡すモジュール名（app/main.py → app.main）。"""
    relative = script.relative_to(root).with_suffix("")
    return ".".join(relative.parts)


_HTML_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def html_title(path: Path) -> str:
    """HTML の <title>。無ければ空。"""
    if path.suffix.lower() not in (".html", ".htm"):
        return ""
    try:
        with open(path, "rb") as handle:
            head = handle.read(64 * 1024).decode("utf-8", errors="replace")
    except OSError:
        return ""
    match = _HTML_TITLE.search(head)
    return " ".join(html.unescape(match.group(1)).split()) if match else ""


def suggest_link(path: Path) -> Suggestion:
    """HTML / PDF などはリンクとして登録する（SPEC 6.10）。"""
    return Suggestion(
        name=html_title(path) or path.stem,
        directory="",
        command="",
        kind=KIND_LINK,
        health_mode=default_health_mode(KIND_LINK),
        notes=["リンクとして登録します。「開く」で TOOL NEXUS が配信し、同じフォルダのCSS・画像も表示されます。"],
        target=str(path),
    )


def static_server_command(*, which: Callable[[str], str | None] = shutil.which) -> str:
    """種別 web の既定コマンド。標準ライブラリの http.server で作業ディレクトリを配信する（SPEC 6.1）。"""
    candidates = osdep.DEFAULT_PYTHONS
    python = next((name for name in candidates if which(name)), None)
    if python is None:
        python = quote(sys.executable)  # 見つからなければ TOOL NEXUS 自身の Python
    return f"{python} -m http.server {PORT_PLACEHOLDER} --bind 127.0.0.1"


# ----------------------------------------------------------------------
# スクリプト（.ps1 / .sh）と停止コマンド（SPEC 6.9）
# ----------------------------------------------------------------------
SH_ON_WINDOWS_NOTE = (
    "Windows では bash が Git Bash か WSL かで動きが変わります（PATH の順で WSL の bash が先に見つかることが多い）。"
    "動かなければ、コマンドの bash を Git Bash の bash.exe のパスに書き換えてください。"
)
BACKGROUND_SCRIPT_NOTE = (
    "バックグラウンドで docker などを動かして自分はすぐ終わるスクリプトは、子の状態を追えません。"
    "死活監視を「HTTP」（ポートがある場合）か「監視しない」にして、停止コマンドを登録してください。"
)


def script_command(path: Path, base: Path | None) -> tuple[str, list[str]]:
    """スクリプトを動かすコマンドと注記。パスは base（作業ディレクトリ）の中なら相対パス、それ以外は絶対パス。"""
    shown = quote(relative_to_root(path, base) if base is not None else str(path))
    if path.suffix.lower() == ".ps1":
        return f"{osdep.POWERSHELL} -NoProfile -File {shown}", []
    return f"bash {shown}", [SH_ON_WINDOWS_NOTE] if osdep.IS_WINDOWS else []


def paired_stop_script(path: Path) -> Path | None:
    """同じフォルダに、ファイル名の start を stop に変えたスクリプトがあれば返す（大文字小文字は問わない）。"""
    stem = path.stem.lower()
    if "start" not in stem:
        return None
    wanted = (stem.replace("start", "stop"), path.suffix.lower())
    try:
        entries = sorted(path.parent.iterdir())
    except OSError:
        return None
    return next(
        (entry for entry in entries if entry.is_file() and (entry.stem.lower(), entry.suffix.lower()) == wanted),
        None,
    )


def suggest_script(path: Path) -> Suggestion:
    """.ps1 / .sh はスクリプトとして登録する。作業ディレクトリはそのフォルダ、名前はフォルダ名。"""
    directory = path.parent
    command, notes = script_command(path, directory)
    stop_command = ""
    stop_script = paired_stop_script(path)
    if stop_script is not None:
        stop_command, _ = script_command(stop_script, directory)
        notes.append(f"停止コマンドに {stop_script.name} を入れました。")
    notes.append(BACKGROUND_SCRIPT_NOTE)
    return Suggestion(
        name=directory.name or path.stem,
        directory=str(directory),
        command=command,
        kind=KIND_SCRIPT,
        health_mode=default_health_mode(KIND_SCRIPT),
        notes=notes,
        stop_command=stop_command,
    )


def suggest_stop_command(
    file: Path | str, directory: str, *, which: Callable[[str], str | None] = shutil.which
) -> tuple[str, list[str]]:
    """停止コマンドの欄で選んだファイルから、(停止コマンド, 注記) を返す。

    パスは作業ディレクトリの中なら相対パス、外（作業ディレクトリが未入力を含む）なら絶対パスにする。
    """
    path = Path(file)
    if not path.is_file():
        raise AssistError(f"ファイルが見つかりません: {path}")
    suffix = path.suffix.lower()
    if suffix in (".bat", ".cmd"):
        raise AssistError(".bat / .cmd は使えません（SPEC 6.9）。.ps1 / .sh にするか、コマンドを直接入力してください。")
    base = Path(directory.strip().strip('"')) if directory.strip() else None
    if suffix in SCRIPT_SUFFIXES:
        return script_command(path, base)
    if suffix == ".py":
        root = find_project_root(path)
        python, notes = python_launcher(path, root, which=which)
        # python_launcher はルートからの相対パスを返す。停止コマンドは作業ディレクトリで動くので書き直す
        tokens = [_rebase(token, root, base) for token in python] + [_rebase(str(path), root, base)]
        return " ".join(quote(token) for token in tokens), notes
    if osdep.is_executable_file(path):
        return quote(str(path)), []
    raise AssistError(f"選べるのは {osdep.EXECUTABLE_LABEL} だけです。")


def _rebase(token: str, root: Path, base: Path | None) -> str:
    """root からの相対パスのトークンを、base からの相対パス（base の外なら絶対パス）に書き直す。"""
    path = Path(token) if Path(token).is_absolute() else root / token
    if not path.exists():
        return token  # uv / py などのコマンド名
    return relative_to_root(path, base) if base is not None else str(path)


# ----------------------------------------------------------------------
# PowerShell の実行ポリシー（SPEC 6.1）
# ----------------------------------------------------------------------
POWERSHELL_NAMES: frozenset[str] = frozenset({"powershell", "powershell.exe", "pwsh", "pwsh.exe"})
BYPASS_OPTION = "-ExecutionPolicy Bypass"
_BYPASS = re.compile(r"\s+-ExecutionPolicy\s+Bypass(?=\s|$)", re.IGNORECASE)
_FIRST_TOKEN = re.compile(r"""^\s*(?:"[^"]*"|'[^']*'|\S+)""")


def is_powershell_command(command: str) -> bool:
    """powershell / pwsh で始まるコマンドか。"""
    try:
        argv = split_command(command or "")
    except ValueError:
        return False
    return bool(argv) and re.split(r"[\\/]", argv[0])[-1].lower() in POWERSHELL_NAMES


def has_bypass(command: str) -> bool:
    return is_powershell_command(command) and bool(_BYPASS.search(command))


def set_bypass(command: str, enabled: bool) -> str:
    """-ExecutionPolicy Bypass を実行ファイルの直後に足す／取り除く。PowerShell 以外のコマンドは変えない。"""
    if not is_powershell_command(command):
        return command
    cleaned = _BYPASS.sub("", command)
    if not enabled:
        return cleaned
    match = _FIRST_TOKEN.match(cleaned)
    assert match is not None  # is_powershell_command が True なら先頭のトークンがある
    return f"{cleaned[:match.end()]} {BYPASS_OPTION}{cleaned[match.end():]}"


def suggest_from_file(
    file: Path | str, *, which: Callable[[str], str | None] = shutil.which
) -> Suggestion:
    """選択されたファイルから、名前・作業ディレクトリ・コマンド・種別を推測する。"""
    path = Path(file)
    if not path.is_file():
        raise AssistError(f"ファイルが見つかりません: {path}")
    suffix = path.suffix.lower()
    if suffix in (".bat", ".cmd"):
        raise AssistError(".bat / .cmd は登録できません（SPEC 6.9）。")
    if suffix in LINK_SUFFIXES:
        return suggest_link(path)
    if suffix in SCRIPT_SUFFIXES:
        return suggest_script(path)
    if suffix != ".py" and not osdep.is_executable_file(path):
        raise AssistError(f"選べるのは {osdep.EXECUTABLE_LABEL} だけです。")

    if suffix != ".py":
        return Suggestion(
            name=path.stem,
            directory=str(path.parent),
            command=quote(str(path)),
            kind=KIND_EXE,
            health_mode=default_health_mode(KIND_EXE),
            notes=["HTTPで待ち受けるツールなら、死活監視を「HTTP」にしてポートを入力してください。"],
        )

    root = find_project_root(path)
    python, notes = python_launcher(path, root, which=which)
    script = relative_to_root(path, root)
    try:
        with open(path, "rb") as handle:
            source = handle.read(MAX_READ_BYTES).decode("utf-8", errors="replace")
    except OSError as exc:
        raise AssistError(f"ファイルを読めませんでした: {exc}") from exc

    framework, app_var = detect_framework(source)
    if framework == FRAMEWORK_STREAMLIT:
        kind, args = KIND_STREAMLIT, ["-m", "streamlit", "run", script]
        notes.append("Streamlit として登録します（ポートなどは起動時に自動で付与します）。")
    elif framework == FRAMEWORK_FLASK:
        kind = KIND_WEB
        args = ["-m", "flask", "--app", script, "run", "--host", "127.0.0.1", "--port", PORT_PLACEHOLDER]
        notes.append("Flask として登録します。")
    elif framework == FRAMEWORK_FASTAPI:
        kind = KIND_WEB
        args = [
            "-m", "uvicorn", f"{module_path(path, root)}:{app_var}",
            "--host", "127.0.0.1", "--port", PORT_PLACEHOLDER,
        ]
        notes.append("FastAPI として登録します（uvicorn が必要です）。")
    else:
        kind, args = KIND_PYTHON, [script]
        notes.append("Python スクリプトとして登録します。")

    return Suggestion(
        name=root.name or path.stem,
        directory=str(root),
        command=" ".join(quote(token) for token in [*python, *args]),
        kind=kind,
        health_mode=default_health_mode(kind),
        notes=notes,
    )


# ----------------------------------------------------------------------
# ダイアログ（OS標準。osdep 経由）
# ----------------------------------------------------------------------
def _initial_dir(value: str | None) -> str | None:
    if not value:
        return None
    path = Path(value.strip().strip('"'))
    folder = path if path.is_dir() else path.parent
    return str(folder) if folder.is_dir() else None


def pick_file(initial: str | None = None, *, picker=None) -> Path | None:
    """ファイル選択ダイアログを開く。キャンセルされたら None。"""
    return _run_dialog(picker or osdep.pick_file, initial)


def pick_folder(initial: str | None = None, *, picker=None) -> Path | None:
    """フォルダ選択ダイアログを開く。キャンセルされたら None。"""
    return _run_dialog(picker or osdep.pick_folder, initial)


def _run_dialog(picker, initial: str | None) -> Path | None:
    try:
        return picker(_initial_dir(initial))
    except ProcessQueryError as exc:
        raise AssistError(str(exc) or "ファイル選択のダイアログを開けませんでした。") from exc
