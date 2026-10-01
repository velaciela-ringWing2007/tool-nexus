"""ログの中継プロセス（SPEC 6.8）と、起動時の環境変数（SPEC 6.2）のテスト."""

from __future__ import annotations

import io
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tool_nexus.process import log_relay
from tool_nexus.process.control import RELAY_SCRIPT, child_env, relay_argv, tool_command

TIMESTAMPED = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} \| (.*)$")


class TestPieces:
    def test_decode_utf8_and_cp932(self) -> None:
        assert log_relay.decode_line("起動 ✅\r\n".encode("utf-8")) == "起動 ✅"
        assert log_relay.decode_line("日本語\r\n".encode("cp932")) == "日本語"

    def test_relay_adds_timestamp_per_line(self) -> None:
        log = io.BytesIO()
        log_relay.relay(io.BytesIO(b"one\r\ntwo\nlast-without-newline"), log)
        lines = log.getvalue().decode("utf-8").splitlines()
        assert [TIMESTAMPED.match(line).group(1) for line in lines] == ["one", "two", "last-without-newline"]

    def test_lines_with_their_own_timestamp_are_kept(self) -> None:
        # Streamlit などは自分で時刻を付ける。二重に付けない
        assert log_relay.stamp("2026-10-01 10:41:26.501 Uvicorn server started") == (
            "2026-10-01 10:41:26.501 Uvicorn server started"
        )
        assert log_relay.stamp("2026-10-01T10:41:26 INFO x") == "2026-10-01T10:41:26 INFO x"
        assert TIMESTAMPED.match(log_relay.stamp("  URL: http://127.0.0.1:8981"))
        assert TIMESTAMPED.match(log_relay.stamp("2026 is a year"))

    def test_markers(self) -> None:
        start = log_relay.start_marker(123, ["python", "my app.py"])
        assert re.match(r"^===== \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} 起動 \(PID 123\) =====\n  python \"my app.py\"\n$", start)
        assert "終了（終了コード 3）" in log_relay.exit_marker(3)
        assert log_relay.stop_marker("停止（再起動）").rstrip().endswith("停止（再起動） =====")

    def test_parse_args(self) -> None:
        assert log_relay.parse_args(["--log", "a.log", "--", "python", "x.py", "--", "y"]) == (
            "a.log", ["python", "x.py", "--", "y"]
        )
        for bad in (["--log", "a.log"], ["--", "python"], ["--log", "a.log", "--"]):
            with pytest.raises(SystemExit):
                log_relay.parse_args(bad)

    def test_unwrap_command(self) -> None:
        relay = ["python.exe", str(RELAY_SCRIPT), "--log", "a.log", "--", "C:\\v\\python.exe", "-m", "streamlit"]
        assert log_relay.unwrap_command(relay) == ["C:\\v\\python.exe", "-m", "streamlit"]
        assert log_relay.unwrap_command(["python", "-m", "streamlit", "--", "x"]) is None


class TestControl:
    def test_relay_argv(self, tmp_path: Path) -> None:
        argv = relay_argv(["python", "app.py"], tmp_path / "t.log")
        assert Path(argv[0]).is_file()
        assert argv[1:] == [str(RELAY_SCRIPT), "--log", str(tmp_path / "t.log"), "--", "python", "app.py"]

    def test_child_env_respects_user_settings(self) -> None:
        env = child_env({"PATH": "x", "PYTHONIOENCODING": "cp932"})
        assert env["PYTHONUNBUFFERED"] == "1"
        assert env["PYTHONIOENCODING"] == "cp932"  # ユーザーが設定していればそちらを優先
        assert env["PATH"] == "x"

    def test_tool_command_for_detection(self, tmp_path: Path) -> None:
        line = subprocess.list2cmdline(relay_argv(["C:\\my tools\\python.exe", "-m", "streamlit", "run", "app.py"], tmp_path / "t.log"))
        if sys.platform != "win32":
            import shlex

            line = shlex.join(relay_argv(["/my tools/python", "-m", "streamlit", "run", "app.py"], tmp_path / "t.log"))
        command = tool_command(line)
        assert "log_relay" not in command
        assert command.endswith("-m streamlit run app.py")
        assert tool_command("python -m streamlit run app.py") == "python -m streamlit run app.py"


class TestRealRelay:
    """実際に中継プロセスを動かして、子プロセスの出力がどう記録されるかを確かめる."""

    def run(self, tmp_path: Path, code: str) -> tuple[int, list[str]]:
        script = tmp_path / "tool.py"
        script.write_text(code, encoding="utf-8")
        log = tmp_path / "tool.log"
        result = subprocess.run(
            [sys.executable, str(RELAY_SCRIPT), "--log", str(log), "--", sys.executable, str(script)],
            env=child_env(),
            timeout=30,
        )
        return result.returncode, log.read_text(encoding="utf-8").splitlines()

    def test_lines_markers_and_exit_code(self, tmp_path: Path) -> None:
        code, lines = self.run(
            tmp_path,
            "import sys\nprint('起動しました ✅')\nprint('エラー出力', file=sys.stderr)\nsys.exit(3)\n",
        )
        assert code == 3  # 終了コードを引き継ぐ
        assert re.match(r"^===== .* 起動 \(PID \d+\) =====$", lines[0])
        assert "tool.py" in lines[1]
        bodies = [TIMESTAMPED.match(line).group(1) for line in lines[2:-1]]
        assert sorted(bodies) == sorted(["起動しました ✅", "エラー出力"])  # 絵文字でも落ちない・標準エラーも記録
        assert lines[-1].endswith("終了（終了コード 3） =====")

    def test_output_is_written_immediately(self, tmp_path: Path) -> None:
        # PYTHONUNBUFFERED により、ツールが動いている間に1行目がログへ出る（SPEC 6.2）
        script = tmp_path / "slow.py"
        script.write_text("import time\nprint('first')\ntime.sleep(3)\nprint('second')\n", encoding="utf-8")
        log = tmp_path / "slow.log"
        proc = subprocess.Popen(
            [sys.executable, str(RELAY_SCRIPT), "--log", str(log), "--", sys.executable, str(script)],
            env=child_env(),
        )
        try:
            import time

            deadline = time.monotonic() + 2.5
            while time.monotonic() < deadline and "| first" not in (log.read_text(encoding="utf-8") if log.exists() else ""):
                time.sleep(0.1)
            assert "| first" in log.read_text(encoding="utf-8")
            assert "| second" not in log.read_text(encoding="utf-8")
        finally:
            proc.wait(timeout=30)

    def test_missing_command_is_logged(self, tmp_path: Path) -> None:
        log = tmp_path / "x.log"
        result = subprocess.run(
            [sys.executable, str(RELAY_SCRIPT), "--log", str(log), "--", str(tmp_path / "no-such-tool.exe")],
            timeout=30,
        )
        assert result.returncode == 127
        text = log.read_text(encoding="utf-8")
        assert "起動できませんでした" in text and "終了コード 127" in text
