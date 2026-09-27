"""process_utils のテスト.

実際にはプロセスを起こさず、argv と呼び出し内容を検証する。
PowerShell / taskkill はフェイクに差し替える。
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from constants import KIND_EXE, KIND_STREAMLIT
from process_utils import (
    LaunchError,
    PidStatus,
    ProcessNotIdentifiedError,
    ProcessQueryError,
    StopError,
    build_argv,
    get_creation_dates,
    get_process_creation_date,
    launch,
    normalize_creation_date,
    prepare_launch,
    resolve_log_path,
    split_command,
    stop,
    verify_pid,
)

JST = timezone(timedelta(hours=9))


def local_iso(dt: datetime) -> str:
    return dt.astimezone().replace(microsecond=0).isoformat()


# ----------------------------------------------------------------------
# split_command
# ----------------------------------------------------------------------
class TestSplitCommand:
    def test_quoted_path_with_spaces(self) -> None:
        argv = split_command(r'"C:\Program Files\Python\python.exe" -m streamlit run app.py')
        assert argv == [
            r"C:\Program Files\Python\python.exe", "-m", "streamlit", "run", "app.py"
        ]

    def test_unquoted_backslash_path_keeps_backslashes(self) -> None:
        argv = split_command(r".venv\Scripts\python.exe -m streamlit run C:\dev\x\app.py")
        assert argv == [r".venv\Scripts\python.exe", "-m", "streamlit", "run", r"C:\dev\x\app.py"]

    def test_argument_with_spaces(self) -> None:
        argv = split_command(r'python.exe -m streamlit run "C:\my tools\app.py" -- --title "a b"')
        assert argv == [
            "python.exe", "-m", "streamlit", "run", r"C:\my tools\app.py", "--", "--title", "a b"
        ]

    def test_single_quotes_are_stripped(self) -> None:
        assert split_command("tool.exe 'a b'") == ["tool.exe", "a b"]

    @pytest.mark.parametrize("command", ["", "   ", "\t\n"])
    def test_empty_or_blank(self, command: str) -> None:
        assert split_command(command) == []

    def test_quote_inside_token_is_kept(self) -> None:
        # 両端が同じクォートで囲まれたトークンだけを外す
        assert split_command('tool.exe --name="ab"') == ["tool.exe", '--name="ab"']

    def test_known_limit_quoted_space_inside_token(self) -> None:
        # 既知の制約: posix=False ではトークン途中から始まるクォート内の空白で分割される。
        # 空白を含む値は `--name "a b"` と別トークンに分けて書く。
        assert split_command('tool.exe --name="a b"') == ["tool.exe", '--name="a', 'b"']

    def test_unclosed_quote_raises(self) -> None:
        with pytest.raises(ValueError):
            split_command('"C:\\Program Files\\x.exe')


# ----------------------------------------------------------------------
# build_argv
# ----------------------------------------------------------------------
class TestBuildArgv:
    def test_streamlit_adds_server_options(self) -> None:
        argv = build_argv("python -m streamlit run app.py", kind=KIND_STREAMLIT, port=8502)
        assert argv == [
            "python", "-m", "streamlit", "run", "app.py",
            "--server.port", "8502",
            "--server.address", "127.0.0.1",
            "--server.headless", "true",
        ]

    def test_does_not_duplicate_existing_options(self) -> None:
        command = (
            "python -m streamlit run app.py --server.port 9000 "
            "--server.address=127.0.0.1 --server.headless false"
        )
        argv = build_argv(command, kind=KIND_STREAMLIT, port=8502)
        assert argv.count("--server.port") == 1
        assert "8502" not in argv
        assert not any(a == "--server.address" for a in argv)
        assert argv.count("--server.headless") == 1
        assert argv[argv.index("--server.headless") + 1] == "false"

    def test_streamlit_without_port_skips_port_option(self) -> None:
        argv = build_argv("streamlit run app.py", kind=KIND_STREAMLIT, port=None)
        assert "--server.port" not in argv
        assert "--server.address" in argv

    def test_exe_gets_no_server_options(self) -> None:
        argv = build_argv(r'"C:\Tools\my tool.exe" --flag', kind=KIND_EXE, port=8600)
        assert argv == [r"C:\Tools\my tool.exe", "--flag"]

    def test_empty_command_raises(self) -> None:
        with pytest.raises(LaunchError):
            build_argv("   ", kind=KIND_STREAMLIT, port=8502)

    def test_unclosed_quote_raises_launch_error(self) -> None:
        with pytest.raises(LaunchError, match="解釈"):
            build_argv('"C:\\x.exe', kind=KIND_EXE, port=None)


class TestPrepareLaunch:
    def test_relative_executable_is_resolved_against_directory(self, tmp_path: Path) -> None:
        exe = tmp_path / ".venv" / "Scripts" / "python.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"")
        argv, workdir = prepare_launch(
            command=r".venv\Scripts\python.exe -m streamlit run app.py",
            kind=KIND_STREAMLIT,
            port=8502,
            directory=tmp_path,
        )
        assert Path(argv[0]) == exe.resolve()
        assert workdir == tmp_path
        assert argv[-6:] == [
            "--server.port", "8502", "--server.address", "127.0.0.1",
            "--server.headless", "true",
        ]

    def test_missing_directory(self, tmp_path: Path) -> None:
        with pytest.raises(LaunchError, match="作業ディレクトリ"):
            prepare_launch(
                command=sys.executable, kind=KIND_EXE, port=None, directory=tmp_path / "nope"
            )

    def test_missing_executable(self, tmp_path: Path) -> None:
        with pytest.raises(LaunchError, match="実行ファイル"):
            prepare_launch(
                command=r".venv\Scripts\python.exe app.py",
                kind=KIND_EXE,
                port=None,
                directory=tmp_path,
            )

    def test_absolute_executable(self, tmp_path: Path) -> None:
        argv, _ = prepare_launch(
            command=f'"{sys.executable}" -c pass', kind=KIND_EXE, port=None, directory=tmp_path
        )
        assert Path(argv[0]) == Path(sys.executable).resolve()


class TestResolveLogPath:
    def test_explicit_relative(self, tmp_path: Path) -> None:
        path = resolve_log_path(
            directory=tmp_path, log_path=r"logs\app.log", default_log_dir="", tool_id=1
        )
        assert path == tmp_path / "logs" / "app.log"

    def test_default_log_dir(self, tmp_path: Path) -> None:
        path = resolve_log_path(
            directory=tmp_path, log_path="", default_log_dir=str(tmp_path / "L"), tool_id=3
        )
        assert path == tmp_path / "L" / "tool-3.log"

    def test_fallback_to_working_directory(self, tmp_path: Path) -> None:
        path = resolve_log_path(directory=tmp_path, log_path="", default_log_dir="", tool_id=3)
        assert path == tmp_path / "tool-nexus.log"


# ----------------------------------------------------------------------
# CreationDate の正規化
# ----------------------------------------------------------------------
class TestNormalizeCreationDate:
    # 実機（Windows PowerShell 5.1）で同一プロセスから得た3通りの表現
    CIM = "20260927193031.305749+540"
    DOTNET_JSON = r"\/Date(1790505031305)\/"
    ISO_O = "2026-09-27T19:30:31.3057490+09:00"

    def test_all_representations_agree(self) -> None:
        expected = local_iso(datetime(2026, 9, 27, 19, 30, 31, tzinfo=JST))
        assert normalize_creation_date(self.CIM) == expected
        assert normalize_creation_date(self.DOTNET_JSON) == expected
        assert normalize_creation_date(self.DOTNET_JSON.replace("\\/", "/")) == expected
        assert normalize_creation_date(self.ISO_O) == expected

    def test_truncates_subseconds(self) -> None:
        a = normalize_creation_date("2026-09-27T19:30:31.0000001+09:00")
        b = normalize_creation_date("2026-09-27T19:30:31.9999999+09:00")
        assert a == b

    def test_different_offsets_same_instant(self) -> None:
        assert normalize_creation_date("2026-09-27T10:30:31Z") == normalize_creation_date(
            "2026-09-27T19:30:31+09:00"
        )

    def test_negative_cim_offset(self) -> None:
        expected = local_iso(datetime(2026, 9, 27, 13, 30, 31, tzinfo=timezone.utc))
        assert normalize_creation_date("20260927083031.000000-300") == expected

    def test_datetime_input(self) -> None:
        dt = datetime(2026, 9, 27, 19, 30, 31, 999999, tzinfo=JST)
        assert normalize_creation_date(dt) == local_iso(dt)

    def test_already_normalized_is_stable(self) -> None:
        once = normalize_creation_date(self.CIM)
        assert normalize_creation_date(once) == once

    @pytest.mark.parametrize("value", [None, "", "  ", "garbage", "20261399999999.000000+540"])
    def test_unparseable(self, value: str | None) -> None:
        assert normalize_creation_date(value) is None


class FakeRunner:
    """subprocess.run の代わりに呼び出し内容を記録するフェイク."""

    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv: list[str], **kwargs) -> subprocess.CompletedProcess:
        self.calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


class TestGetProcessCreationDate:
    def test_parses_output_and_uses_list_argv(self) -> None:
        runner = FakeRunner(stdout="2026-09-27T19:30:31.3057490+09:00\r\n")
        result = get_process_creation_date(1234, runner=runner)
        assert result == normalize_creation_date("2026-09-27T19:30:31+09:00")
        argv, kwargs = runner.calls[0]
        assert argv[0] == "powershell.exe"
        assert "ProcessId=1234" in argv[-1]
        assert kwargs.get("shell") in (None, False)

    def test_not_found_returns_none(self) -> None:
        assert get_process_creation_date(1234, runner=FakeRunner(stdout="\r\n")) is None

    def test_failure_raises(self) -> None:
        with pytest.raises(ProcessQueryError):
            get_process_creation_date(1234, runner=FakeRunner(returncode=1, stderr="boom"))

    def test_unparseable_output_raises(self) -> None:
        with pytest.raises(ProcessQueryError):
            get_process_creation_date(1234, runner=FakeRunner(stdout="???"))


class TestGetCreationDates:
    def test_batches_into_one_call(self) -> None:
        runner = FakeRunner(
            stdout="10\t2026-09-27T19:30:31.3057490+09:00\r\n20\t20260927193032.000000+540\r\n"
        )
        result = get_creation_dates([20, 10, 10, 30], runner=runner)
        assert result == {
            10: normalize_creation_date("2026-09-27T19:30:31+09:00"),
            20: normalize_creation_date("2026-09-27T19:30:32+09:00"),
        }
        assert len(runner.calls) == 1
        assert "ProcessId=10 OR ProcessId=20 OR ProcessId=30" in runner.calls[0][0][-1]

    def test_empty_does_not_call_powershell(self) -> None:
        runner = FakeRunner()
        assert get_creation_dates([], runner=runner) == {}
        assert get_creation_dates([0, None], runner=runner) == {}
        assert runner.calls == []

    def test_failure_raises(self) -> None:
        with pytest.raises(ProcessQueryError):
            get_creation_dates([1], runner=FakeRunner(returncode=1))


# ----------------------------------------------------------------------
# verify_pid（停止前の照合と process モードの死活監視で共通）
# ----------------------------------------------------------------------
STARTED = "2026-09-27T13:45:01+09:00"


def lookup_returning(value: str | None):
    def lookup(pid: int) -> str | None:
        return value

    return lookup


def lookup_failing(pid: int) -> str | None:
    raise ProcessQueryError("powershell failed")


class TestVerifyPid:
    def test_match(self) -> None:
        assert verify_pid(10, STARTED, lookup=lookup_returning(STARTED)) is PidStatus.MATCH

    def test_match_across_representations(self) -> None:
        # 保存時はISO、照合時はCIM datetime で取れても一致とみなす
        lookup = lookup_returning("20260927134501.998000+540")
        assert verify_pid(10, STARTED, lookup=lookup) is PidStatus.MATCH

    def test_not_found(self) -> None:
        assert verify_pid(10, STARTED, lookup=lookup_returning(None)) is PidStatus.NOT_FOUND

    def test_pid_reused(self) -> None:
        other = "2026-09-27T15:00:00+09:00"
        assert verify_pid(10, STARTED, lookup=lookup_returning(other)) is PidStatus.MISMATCH

    def test_lookup_failure_is_unknown(self) -> None:
        assert verify_pid(10, STARTED, lookup=lookup_failing) is PidStatus.UNKNOWN

    @pytest.mark.parametrize(
        ("pid", "created_at"), [(None, STARTED), (10, None), (10, ""), (0, STARTED), (10, "x")]
    )
    def test_missing_record_is_unknown_without_lookup(self, pid, created_at) -> None:
        def lookup(pid: int) -> str | None:  # pragma: no cover - 呼ばれてはいけない
            raise AssertionError("lookup should not be called")

        assert verify_pid(pid, created_at, lookup=lookup) is PidStatus.UNKNOWN


# ----------------------------------------------------------------------
# stop
# ----------------------------------------------------------------------
class TestStop:
    def test_kills_tree_when_verified(self) -> None:
        runner = FakeRunner()
        stop(4321, STARTED, lookup=lookup_returning(STARTED), runner=runner)
        argv, kwargs = runner.calls[0]
        assert argv == ["taskkill", "/PID", "4321", "/T", "/F"]
        assert kwargs.get("shell") in (None, False)

    @pytest.mark.parametrize(
        ("lookup", "status"),
        [
            (lookup_returning(None), PidStatus.NOT_FOUND),
            (lookup_returning("2026-09-27T15:00:00+09:00"), PidStatus.MISMATCH),
            (lookup_failing, PidStatus.UNKNOWN),
        ],
    )
    def test_does_not_kill_when_not_identified(self, lookup, status) -> None:
        runner = FakeRunner()
        with pytest.raises(ProcessNotIdentifiedError) as excinfo:
            stop(4321, STARTED, lookup=lookup, runner=runner)
        assert excinfo.value.status is status
        assert "特定できませんでした" in str(excinfo.value)
        assert runner.calls == []

    def test_does_not_kill_without_created_at(self) -> None:
        runner = FakeRunner()
        with pytest.raises(ProcessNotIdentifiedError):
            stop(4321, None, lookup=lookup_returning(STARTED), runner=runner)
        assert runner.calls == []

    def test_taskkill_failure(self) -> None:
        runner = FakeRunner(returncode=128, stderr="ERROR: not found")
        with pytest.raises(StopError, match="not found"):
            stop(4321, STARTED, lookup=lookup_returning(STARTED), runner=runner)


# ----------------------------------------------------------------------
# launch（Popen はフェイク）
# ----------------------------------------------------------------------
class FakePopen:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv: list[str], **kwargs):
        self.calls.append((argv, kwargs))

        class _Proc:
            pid = 5555

        return _Proc()


class TestLaunch:
    def test_launch_detached_with_log_file(self, tmp_path: Path) -> None:
        popen = FakePopen()
        log_path = tmp_path / "logs" / "tool.log"
        result = launch(
            command=f'"{sys.executable}" -m streamlit run app.py',
            kind=KIND_STREAMLIT,
            port=8502,
            directory=tmp_path,
            log_path=log_path,
            popen=popen,
            lookup=lookup_returning(STARTED),
        )
        assert result.pid == 5555
        assert result.created_at == STARTED
        assert log_path.exists()

        argv, kwargs = popen.calls[0]
        assert "--server.port" in argv
        assert kwargs["cwd"] == tmp_path
        assert kwargs["stdout"] is not subprocess.PIPE
        assert kwargs["stderr"] is subprocess.STDOUT
        assert kwargs.get("shell") in (None, False)
        if sys.platform == "win32":
            flags = kwargs["creationflags"]
            assert flags & subprocess.CREATE_NO_WINDOW
            # DETACHED_PROCESS を併用すると孫プロセスがコンソールウィンドウを出してしまう
            assert not flags & subprocess.DETACHED_PROCESS

    def test_launch_without_creation_date(self, tmp_path: Path) -> None:
        result = launch(
            command=f'"{sys.executable}" -c pass',
            kind=KIND_EXE,
            port=None,
            directory=tmp_path,
            log_path=tmp_path / "t.log",
            popen=FakePopen(),
            lookup=lookup_failing,
        )
        assert result.created_at is None

    def test_launch_checks_before_popen(self, tmp_path: Path) -> None:
        popen = FakePopen()
        with pytest.raises(LaunchError):
            launch(
                command="missing-tool-xyz.exe",
                kind=KIND_EXE,
                port=None,
                directory=tmp_path,
                log_path=tmp_path / "t.log",
                popen=popen,
            )
        assert popen.calls == []
