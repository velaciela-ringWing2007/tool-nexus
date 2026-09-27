"""動作設定の検証と正規化（SPEC 8.2）.

不正な値は保存しない。検証を通った値は保存用の文字列に正規化して返す。
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from constants import DEFAULT_SETTINGS
from health import parse_interval
from port_utils import PortError, parse_reserved_ports, validate_port_range

MIN_HEALTH_INTERVAL = 5.0
MAX_HEALTH_INTERVAL = 3600.0
MIN_HEALTH_TIMEOUT = 0.2
MAX_HEALTH_TIMEOUT = 30.0

SETTING_LABELS: dict[str, str] = {
    "health_interval": "死活監視の間隔",
    "health_timeout": "ヘルスチェックのタイムアウト",
    "port_range_low": "自動割当の下限",
    "port_range_high": "自動割当の上限",
    "reserved_ports": "予約済みポート",
    "default_log_dir": "ログの既定フォルダ",
}


class SettingsError(ValueError):
    """設定値が不正な場合に送出する例外. errors は {キー: メッセージ}."""

    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__("\n".join(f"{SETTING_LABELS.get(k, k)}: {v}" for k, v in errors.items()))
        self.errors = errors


def _format_seconds(seconds: float) -> str:
    return f"{int(seconds)}s" if float(seconds).is_integer() else f"{seconds:g}s"


def validate_settings(values: Mapping[str, object]) -> dict[str, str]:
    """設定値を検証し、保存用の文字列に正規化して返す。

    渡されなかったキーは対象外（部分的な更新ができる）。未知のキーは無視する。
    1つでも不正な値があれば SettingsError（全項目のエラーをまとめて持つ）。
    """
    raw = {key: str(value if value is not None else "").strip() for key, value in values.items()}
    result: dict[str, str] = {}
    errors: dict[str, str] = {}

    if "health_interval" in raw:
        seconds = parse_interval(raw["health_interval"], default=-1)
        if seconds < 0:
            errors["health_interval"] = "「60s」「2m」「90」のように入力してください。"
        elif not MIN_HEALTH_INTERVAL <= seconds <= MAX_HEALTH_INTERVAL:
            errors["health_interval"] = "5秒〜1時間の範囲で入力してください。"
        else:
            result["health_interval"] = _format_seconds(seconds)

    if "health_timeout" in raw:
        try:
            timeout = float(raw["health_timeout"])
        except ValueError:
            errors["health_timeout"] = "秒数（数値）で入力してください。"
        else:
            if not MIN_HEALTH_TIMEOUT <= timeout <= MAX_HEALTH_TIMEOUT:
                errors["health_timeout"] = "0.2〜30秒の範囲で入力してください。"
            else:
                result["health_timeout"] = str(timeout)

    if "port_range_low" in raw or "port_range_high" in raw:
        low = raw.get("port_range_low", DEFAULT_SETTINGS["port_range_low"])
        high = raw.get("port_range_high", DEFAULT_SETTINGS["port_range_high"])
        try:
            low_value, high_value = validate_port_range(low, high)
        except PortError as exc:
            errors["port_range_low"] = str(exc)
        else:
            result["port_range_low"], result["port_range_high"] = str(low_value), str(high_value)

    if "reserved_ports" in raw:
        try:
            ports = parse_reserved_ports(raw["reserved_ports"])
        except PortError as exc:
            errors["reserved_ports"] = str(exc)
        else:
            result["reserved_ports"] = ",".join(str(p) for p in sorted(ports))

    if "default_log_dir" in raw:
        folder = raw["default_log_dir"].strip('"')
        if folder and not Path(folder).is_absolute():
            errors["default_log_dir"] = "絶対パスで入力してください（空欄なら各ツールの作業ディレクトリ）。"
        else:
            result["default_log_dir"] = folder

    if errors:
        raise SettingsError(errors)
    return result
