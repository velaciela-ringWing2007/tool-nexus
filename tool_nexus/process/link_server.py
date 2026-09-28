"""リンク（ローカルのファイル／フォルダ）の配信（SPEC 6.10）.

ブラウザは http:// のページから file:/// へのリンクをブロックするため、
ローカルのファイルは TOOL NEXUS のプロセス内の小さなHTTPサーバーで配信する。

* 待ち受けは 127.0.0.1 のみ、GET / HEAD のみ
* URL は /links/<ID>/<相対パス>。起点はファイルならそのフォルダ、フォルダならそのフォルダ
* 起点フォルダの外は配信しない（..、エンコードされた ..、\\、シンボリックリンクで外に出るパスは 404）
* リクエストのたびにリンクを引くので、登録・削除はすぐに反映される
"""

from __future__ import annotations

import logging
import os
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import quote, unquote, urlsplit

from tool_nexus.core.constants import KIND_LINK, LINK_SERVER_PORT
from tool_nexus.core.models import Tool, is_url

logger = logging.getLogger("tool_nexus.link_server")

LINK_PREFIX = "/links/"
THREAD_NAME = "tool-nexus-link-server"

ToolLookup = Callable[[int], "Tool | None"]


def is_local_link(tool: Tool | None) -> bool:
    return tool is not None and tool.kind == KIND_LINK and bool(tool.target) and not is_url(tool.target)


def base_dir(tool: Tool | None) -> Path | None:
    """配信の起点フォルダ。ファイルならそのフォルダ、フォルダならそのフォルダ。無ければ None。"""
    if not is_local_link(tool):
        return None
    target = Path(tool.target)
    if target.is_dir():
        return target
    if target.is_file():
        return target.parent
    return None


def link_url(tool: Tool, *, port: int = LINK_SERVER_PORT) -> str | None:
    """「開く」のURL。URL のリンクはそのまま、ローカルは配信サーバーのURL。"""
    if tool.kind != KIND_LINK or not tool.target:
        return None
    if is_url(tool.target):
        return tool.target
    target = Path(tool.target)
    prefix = f"http://127.0.0.1:{int(port)}{LINK_PREFIX}{int(tool.id)}/"
    return prefix if target.is_dir() else prefix + quote(target.name)


def resolve_request(path: str, lookup: ToolLookup) -> Path | None:
    """リクエストのパスを、配信してよいローカルのパスに変換する。配信できなければ None。"""
    request_path = unquote(urlsplit(path).path)
    if not request_path.startswith(LINK_PREFIX):
        return None
    id_text, _, relative = request_path[len(LINK_PREFIX):].partition("/")
    if not id_text.isdigit():
        return None
    base = base_dir(lookup(int(id_text)))
    if base is None:
        return None

    # Windows では \ も区切りになるため / に揃えてから、.. やドライブ指定を拒否する
    parts = [part for part in relative.replace("\\", "/").split("/") if part not in ("", ".")]
    if any(part == ".." or ":" in part for part in parts):
        return None
    candidate = base.joinpath(*parts)
    try:
        resolved_base = base.resolve()
        resolved = candidate.resolve()
    except OSError:
        return None
    # シンボリックリンクで起点の外に出るものも拒否する
    if resolved != resolved_base and resolved_base not in resolved.parents:
        return None
    return candidate


class LinkRequestHandler(SimpleHTTPRequestHandler):
    """登録したリンクの起点フォルダの中だけを返すハンドラ（GET / HEAD のみ）."""

    server: "LinkServer"

    def send_head(self):  # noqa: D401 - http.server の拡張
        target = resolve_request(self.path, self.server.lookup)
        if target is None:
            self.send_error(404, "Not Found")
            return None
        self._target = target
        return super().send_head()

    def translate_path(self, path: str) -> str:
        return str(getattr(self, "_target", ""))

    def log_message(self, format: str, *args) -> None:
        logger.debug("%s - %s", self.address_string(), format % args)


class LinkServer(ThreadingHTTPServer):
    daemon_threads = True
    # Windows の SO_REUSEADDR は待ち受け中のポートにまで bind できてしまうため付けない
    allow_reuse_address = os.name != "nt"

    def __init__(self, port: int, lookup: ToolLookup) -> None:
        super().__init__(("127.0.0.1", int(port)), LinkRequestHandler)
        self.lookup = lookup


_lock = threading.Lock()
_server: LinkServer | None = None


def ensure_server(lookup: ToolLookup, *, port: int = LINK_SERVER_PORT) -> str | None:
    """配信サーバーを1つだけ起動する（Streamlit の再実行では作り直さない）。失敗したら理由を返す。"""
    global _server
    with _lock:
        if _server is not None:
            return None
        # モジュールが読み直された場合でも、既に動いている自分のスレッドがあればそれを使う
        if any(t.name == THREAD_NAME and t.is_alive() for t in threading.enumerate()):
            return None
        try:
            server = LinkServer(port, lookup)
        except OSError as exc:
            return f"リンクの配信サーバーを起動できませんでした（ポート {port}）: {exc}"
        threading.Thread(target=server.serve_forever, name=THREAD_NAME, daemon=True).start()
        _server = server
        return None


def stop_server() -> None:
    """配信サーバーを止める（テスト用）。"""
    global _server
    with _lock:
        if _server is not None:
            _server.shutdown()
            _server.server_close()
            _server = None
