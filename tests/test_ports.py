"""port_utils のテスト."""

from __future__ import annotations

import socket
import sys

import pytest

from tool_nexus.core.constants import DEFAULT_SETTINGS, TOOL_NEXUS_PORT
from tool_nexus.core.ports import (
    PortError,
    assign_port,
    is_port_free,
    parse_reserved_ports,
    pick_free_port,
    validate_port_range,
)


def always_free(port: int) -> bool:
    return True


class TestIsPortFree:
    def test_listening_port_is_not_free(self) -> None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen()
            port = sock.getsockname()[1]
            assert is_port_free(port) is False
        assert is_port_free(port) is True


    @pytest.mark.skipif(sys.platform == "win32", reason="TIME_WAIT で bind が失敗するのは Linux の挙動")
    def test_time_wait_is_free(self) -> None:
        # サーバー側から先に閉じると、サーバー側のポートに TIME_WAIT が残る。
        # Linux は元のソケットにも SO_REUSEADDR があるときだけ TIME_WAIT を無視して bind できる。
        # Streamlit（uvicorn / tornado）や http.server はどれも付けているので、それに合わせる。
        server = socket.socket()
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        client = socket.create_connection(("127.0.0.1", port))
        conn, _ = server.accept()
        conn.close()
        server.close()
        client.close()
        assert is_port_free(port) is True


class TestPickFreePort:
    def test_avoids_ports_in_use(self) -> None:
        busy = {8500, 8501, 8502}
        port = pick_free_port(set(), 8500, 8503, is_free=lambda p: p not in busy)
        assert port == 8503

    def test_avoids_registered_ports(self) -> None:
        assert pick_free_port({8500, 8501}, 8500, 8502, is_free=always_free) == 8502

    def test_stays_in_range(self) -> None:
        for _ in range(50):
            assert 8600 <= pick_free_port(set(), 8600, 8610, is_free=always_free) <= 8610

    def test_no_free_port_raises(self) -> None:
        with pytest.raises(PortError):
            pick_free_port({8500}, 8500, 8501, is_free=lambda p: False)

    def test_small_range_with_single_free_port(self) -> None:
        # random.sample(..., 100) だと範囲が100未満で ValueError になっていた
        assert pick_free_port({8500, 8501, 8502}, 8500, 8503, is_free=always_free) == 8503

    def test_single_port_range(self) -> None:
        assert pick_free_port(set(), 8700, 8700, is_free=always_free) == 8700

    def test_never_returns_tool_nexus_port(self) -> None:
        with pytest.raises(PortError):
            pick_free_port(set(), TOOL_NEXUS_PORT, TOOL_NEXUS_PORT, is_free=always_free)

    def test_real_bind(self) -> None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen()
            busy = sock.getsockname()[1]
            port = pick_free_port(set(), busy, busy + 1)
        assert port == busy + 1


class TestValidatePortRange:
    def test_valid(self) -> None:
        assert validate_port_range("8500", 8999) == (8500, 8999)

    @pytest.mark.parametrize(
        ("low", "high"), [(9000, 8500), (1000, 8000), (8000, 49152), (50000, 50010), ("a", 1)]
    )
    def test_invalid(self, low, high) -> None:
        with pytest.raises(PortError):
            validate_port_range(low, high)

    def test_bounds_are_inclusive(self) -> None:
        assert validate_port_range(1024, 49151) == (1024, 49151)


class TestReservedPorts:
    def test_parse(self) -> None:
        assert parse_reserved_ports(" 8501, 8600 ,,8501、8700") == {8501, 8600, 8700}

    def test_empty(self) -> None:
        assert parse_reserved_ports("") == set()

    @pytest.mark.parametrize("raw", ["abc", "8501,x", "70000", "0"])
    def test_invalid(self, raw: str) -> None:
        with pytest.raises(PortError):
            parse_reserved_ports(raw)


class TestAssignPort:
    def test_excludes_reserved_registered_and_self(self) -> None:
        settings = DEFAULT_SETTINGS | {
            "port_range_low": "8498",
            "port_range_high": "8503",
            "reserved_ports": "8501",
        }
        seen = {assign_port(settings, {8500, 8502}, is_free=always_free) for _ in range(40)}
        # 8499 は TOOL NEXUS 自身、8501 は予約、8500/8502 は登録済み
        assert seen <= {8498, 8503}

    def test_default_settings_avoid_list_nexus(self) -> None:
        seen = {
            assign_port(
                DEFAULT_SETTINGS | {"port_range_low": "8500", "port_range_high": "8502"},
                set(),
                is_free=always_free,
            )
            for _ in range(40)
        }
        assert 8501 not in seen
