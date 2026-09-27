"""settings_utils のテスト."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tool_nexus.core.settings import SettingsError, validate_settings


class TestHealthInterval:
    @pytest.mark.parametrize(
        ("value", "expected"), [("60s", "60s"), ("2m", "120s"), ("90", "90s"), ("1h", "3600s"), ("7.5", "7.5s")]
    )
    def test_normalized(self, value: str, expected: str) -> None:
        assert validate_settings({"health_interval": value}) == {"health_interval": expected}

    @pytest.mark.parametrize("value", ["abc", "", "1s", "2h", "0"])
    def test_invalid(self, value: str) -> None:
        with pytest.raises(SettingsError) as excinfo:
            validate_settings({"health_interval": value})
        assert "health_interval" in excinfo.value.errors


class TestHealthTimeout:
    def test_valid(self) -> None:
        assert validate_settings({"health_timeout": " 2 "}) == {"health_timeout": "2.0"}

    @pytest.mark.parametrize("value", ["x", "0.1", "31"])
    def test_invalid(self, value: str) -> None:
        with pytest.raises(SettingsError):
            validate_settings({"health_timeout": value})


class TestPorts:
    def test_range(self) -> None:
        assert validate_settings({"port_range_low": "8600", "port_range_high": "8700"}) == {
            "port_range_low": "8600",
            "port_range_high": "8700",
        }

    @pytest.mark.parametrize(("low", "high"), [("9000", "8000"), ("80", "8000"), ("8000", "50000")])
    def test_invalid_range(self, low: str, high: str) -> None:
        with pytest.raises(SettingsError):
            validate_settings({"port_range_low": low, "port_range_high": high})

    def test_reserved_ports_are_sorted_and_deduplicated(self) -> None:
        assert validate_settings({"reserved_ports": "8600, 8501,8501"}) == {"reserved_ports": "8501,8600"}

    def test_reserved_ports_empty(self) -> None:
        assert validate_settings({"reserved_ports": ""}) == {"reserved_ports": ""}

    def test_reserved_ports_invalid(self) -> None:
        with pytest.raises(SettingsError):
            validate_settings({"reserved_ports": "8501,abc"})


class TestLogDir:
    def test_empty(self) -> None:
        assert validate_settings({"default_log_dir": ""}) == {"default_log_dir": ""}

    def test_absolute(self, tmp_path: Path) -> None:
        folder = tmp_path / "logs"  # 存在しなくてよい（起動時に作る）
        assert validate_settings({"default_log_dir": f'"{folder}"'}) == {"default_log_dir": str(folder)}

    def test_relative_is_rejected(self) -> None:
        with pytest.raises(SettingsError):
            validate_settings({"default_log_dir": "logs"})


def test_all_errors_are_reported_together() -> None:
    with pytest.raises(SettingsError) as excinfo:
        validate_settings({"health_interval": "x", "health_timeout": "x", "reserved_ports": "x"})
    assert set(excinfo.value.errors) == {"health_interval", "health_timeout", "reserved_ports"}
    assert "死活監視の間隔" in str(excinfo.value)


def test_unknown_keys_are_ignored() -> None:
    assert validate_settings({"nope": "1"}) == {}


@pytest.mark.skipif(sys.platform != "win32", reason="Windows のパス表記")
def test_windows_absolute_log_dir() -> None:
    assert validate_settings({"default_log_dir": r"C:\logs"}) == {"default_log_dir": r"C:\logs"}
