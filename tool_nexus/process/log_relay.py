"""ツールの出力に時刻を付けてログへ書く中継プロセス（SPEC 6.8）.

TOOL NEXUS はツールを直接ではなく、このスクリプトの子として起動する。

    python log_relay.py --log <ログファイル> [--label <見出し>] -- <ツールの argv...>

* ツールの標準出力・標準エラー出力を1行ずつ受け取り、`2026-10-01 10:15:03.412 | ` を付けて追記する
* 起動の区切り行（日時・PID・コマンド）と、ツールが自分で終わったときの終了の区切り行を書く。
  区切り行の見出しは既定で「起動」。停止コマンドを実行するときは `--label 停止コマンド` を渡す
* 終了コードはツールのものをそのまま返す

単独のスクリプトとして動かすため、標準ライブラリだけを使い、TOOL NEXUS の他のモジュールは import しない。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime
from typing import BinaryIO, Iterable

RELAY_SEPARATOR = "--"
LOG_OPTION = "--log"
LABEL_OPTION = "--label"
DEFAULT_LABEL = "起動"
_USAGE = "usage: log_relay.py --log <file> [--label <text>] -- <command...>"

# 行頭に既に日時があるか（Streamlit などは自分で時刻を付けて出力する。二重に付けない）
_HAS_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}")


def now_label(*, millis: bool) -> str:
    now = datetime.now()
    return now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] if millis else now.strftime("%Y-%m-%d %H:%M:%S")


def decode_line(raw: bytes) -> str:
    """ツールの出力1行を文字列にする。UTF-8 で読めなければ cp932（Windows の古い既定）で読む。"""
    text = raw.rstrip(b"\r\n")
    try:
        return text.decode("utf-8")
    except UnicodeDecodeError:
        return text.decode("cp932", errors="replace")


def format_command(argv: Iterable[str]) -> str:
    """区切り行に出すコマンド（空白を含む引数はクォートする）。"""
    return " ".join(f'"{arg}"' if any(ch.isspace() for ch in arg) else arg for arg in argv)


def start_marker(pid: int, argv: list[str], label: str = DEFAULT_LABEL) -> str:
    return f"===== {now_label(millis=False)} {label} (PID {pid}) =====\n  {format_command(argv)}\n"


def exit_marker(code: int) -> str:
    return f"===== {now_label(millis=False)} 終了（終了コード {code}） =====\n"


def stop_marker(reason: str = "停止") -> str:
    """TOOL NEXUS から止めたときの区切り行（中継プロセスごと止まるため TOOL NEXUS が書く）。"""
    return f"===== {now_label(millis=False)} {reason} =====\n"


def stamp(line: str) -> str:
    """1行に時刻を付ける。行頭に既に日時がある行（Streamlit のログなど）はそのままにする。"""
    if _HAS_TIMESTAMP.match(line):
        return line
    return f"{now_label(millis=True)} | {line}"


def relay(stream: BinaryIO, log: BinaryIO) -> None:
    """出力を1行ずつ時刻付きでログへ書く。1行ごとに書き出す（溜めない）。"""
    for raw in iter(stream.readline, b""):
        log.write(f"{stamp(decode_line(raw))}\n".encode("utf-8"))
        log.flush()


def parse_args(args: list[str]) -> tuple[str, list[str], str]:
    """`--log <path> [--label <text>] -- <argv...>` を (ログファイル, ツールの argv, 見出し) に分ける。"""
    if RELAY_SEPARATOR not in args:
        raise SystemExit(_USAGE)
    split = args.index(RELAY_SEPARATOR)
    head, argv = args[:split], args[split + 1:]
    if len(head) not in (2, 4) or head[0] != LOG_OPTION or not argv:
        raise SystemExit(_USAGE)
    if len(head) == 4 and (head[2] != LABEL_OPTION or not head[3]):
        raise SystemExit(_USAGE)
    return head[1], argv, head[3] if len(head) == 4 else DEFAULT_LABEL


def unwrap_command(argv: list[str]) -> list[str] | None:
    """中継プロセスの argv からツールの argv を取り出す。中継プロセスでなければ None（検出用。SPEC 6.6）。"""
    if not any(os.path.basename(arg) == "log_relay.py" for arg in argv[:3]):
        return None
    if RELAY_SEPARATOR not in argv:
        return None
    inner = argv[argv.index(RELAY_SEPARATOR) + 1:]
    return inner or None


def main(args: list[str] | None = None) -> int:
    log_path, argv, label = parse_args(list(sys.argv[1:] if args is None else args))
    with open(log_path, "ab") as log:
        try:
            child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except OSError as exc:
            log.write(start_marker(os.getpid(), argv, label).encode("utf-8"))
            log.write(f"{now_label(millis=True)} | 起動できませんでした: {exc}\n".encode("utf-8"))
            log.write(exit_marker(127).encode("utf-8"))
            return 127
        log.write(start_marker(os.getpid(), argv, label).encode("utf-8"))
        log.flush()
        relay(child.stdout, log)
        code = child.wait()
        log.write(exit_marker(code).encode("utf-8"))
        return code


if __name__ == "__main__":
    sys.exit(main())
