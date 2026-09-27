"""ポートの空き確認.

空き確認は実際に bind() して行う（LISTEN中かどうかを最も確実に判定できるため）。
"""

from __future__ import annotations

import socket


def is_port_free(port: int, host: str = "127.0.0.1") -> bool:
    """指定ポートに bind できるか（他のプロセスが使っていないか）を返す。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, int(port)))
        except OSError:
            return False
    return True
