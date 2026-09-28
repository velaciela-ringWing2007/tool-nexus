"""ログのノイズを抑えるフィルタ.

Windows の asyncio（ProactorEventLoop）は、ブラウザを閉じて WebSocket が突然切れると、
後片付けの `sock.shutdown(socket.SHUT_RDWR)` で ConnectionResetError（WinError 10054）を起こし、
「Exception in callback _ProactorBasePipeTransport._call_connection_lost(None)」として
トレースバックをログに出す。動作には影響しないが、毎回出ると本物のエラーに気づきにくくなるため、
**この組み合わせだけ**を捨てる。ほかのエラーはそのまま出す。
"""

from __future__ import annotations

import logging

_DISCONNECT_ERRORS = (ConnectionResetError, ConnectionAbortedError)


class ClientDisconnectFilter(logging.Filter):
    """ブラウザを閉じたときの接続リセットのログだけを捨てる."""

    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        if isinstance(exc, _DISCONNECT_ERRORS) and "_call_connection_lost" in record.getMessage():
            return False
        return True


def install() -> None:
    """asyncio のロガーにフィルタを付ける（何度呼んでも1つだけ）。"""
    logger = logging.getLogger("asyncio")
    # モジュールが読み直されてもクラスが変わるだけなので、名前で重複を判定する
    if not any(type(f).__name__ == ClientDisconnectFilter.__name__ for f in logger.filters):
        logger.addFilter(ClientDisconnectFilter())
