"""空きポートの割当と、ポート設定の検証.

空き確認は実際に bind() して行う（LISTEN中かどうかを最も確実に判定できるため）。
"""

from __future__ import annotations

import os
import random
import socket
from typing import Callable, Iterable

from tool_nexus.core.constants import LINK_SERVER_PORT, TOOL_NEXUS_PORT

# 自動割当の範囲として許すポート。
# 1023以下は well-known、49152以降は Windows の動的ポート範囲（OSが自動で使う）。
MIN_ASSIGNABLE_PORT = 1024
MAX_ASSIGNABLE_PORT = 49151


class PortError(ValueError):
    """ポートの割当や設定が行えない場合に送出する例外."""


def is_port_free(port: int, host: str = "127.0.0.1") -> bool:
    """指定ポートに bind できるか（他のプロセスが使っていないか）を返す。

    Linux では、閉じた接続が TIME_WAIT で残っている間（約60秒）は素の bind() が失敗し、
    停止直後のポートを「使用中」と誤判定する（ヘルスチェックの接続でも起きる。CI で確認）。
    SO_REUSEADDR を付けると TIME_WAIT は無視しつつ、LISTEN 中のポートは使用中と判定できる。
    Windows の SO_REUSEADDR は LISTEN 中のポートにまで bind できてしまうため付けない。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if os.name != "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, int(port)))
        except OSError:
            return False
    return True


def pick_free_port(
    used: Iterable[int],
    low: int = 8500,
    high: int = 8999,
    *,
    is_free: Callable[[int], bool] = is_port_free,
) -> int:
    """範囲内の空きポートを1つ返す。

    候補をサンプリングせず、未使用のものを全件シャッフルして順に試す
    （サンプリングは範囲が狭いと ValueError になり、空きがあっても見つからないことがある）。
    TOOL NEXUS 自身とリンク配信のポートは used に含まれていなくても常に除外する。
    """
    excluded = set(used) | {TOOL_NEXUS_PORT, LINK_SERVER_PORT}
    candidates = [port for port in range(low, high + 1) if port not in excluded]
    random.shuffle(candidates)
    for port in candidates:
        if is_free(port):
            return port
    raise PortError(f"{low}〜{high} に空きポートが見つかりませんでした。")


def validate_port_range(low: int | str, high: int | str) -> tuple[int, int]:
    """自動割当の範囲を検証して (low, high) を返す。"""
    try:
        low_value, high_value = int(str(low).strip()), int(str(high).strip())
    except ValueError as exc:
        raise PortError("ポート範囲は整数で入力してください。") from exc
    for value in (low_value, high_value):
        if not MIN_ASSIGNABLE_PORT <= value <= MAX_ASSIGNABLE_PORT:
            raise PortError(
                f"ポート範囲は {MIN_ASSIGNABLE_PORT}〜{MAX_ASSIGNABLE_PORT} で指定してください"
                "（49152以降はWindowsの動的ポート範囲のため使えません）。"
            )
    if low_value > high_value:
        raise PortError("ポート範囲の下限が上限を超えています。")
    return low_value, high_value


def parse_reserved_ports(raw: str) -> set[int]:
    """カンマ区切りの予約済みポートを解釈する。不正な値は PortError。"""
    ports: set[int] = set()
    for item in (raw or "").replace("、", ",").split(","):
        text = item.strip()
        if not text:
            continue
        try:
            port = int(text)
        except ValueError as exc:
            raise PortError(f"予約済みポートの値が不正です: {text}") from exc
        if not 1 <= port <= 65535:
            raise PortError(f"予約済みポートの値が範囲外です: {port}")
        ports.add(port)
    return ports


def assign_port(
    settings: dict[str, str],
    registered: Iterable[int],
    *,
    is_free: Callable[[int], bool] = is_port_free,
) -> int:
    """設定の範囲・予約済みポート・登録済みポートを考慮して空きポートを割り当てる。"""
    low, high = validate_port_range(settings["port_range_low"], settings["port_range_high"])
    reserved = parse_reserved_ports(settings.get("reserved_ports", ""))
    return pick_free_port(set(registered) | reserved, low, high, is_free=is_free)
