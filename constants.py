"""アプリケーション全体で共有する定数."""

from __future__ import annotations

from pathlib import Path
from typing import Final

APP_NAME: Final[str] = "TOOL NEXUS"
APP_ICON: Final[str] = "⚡"

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent
DATA_DIR: Final[Path] = PROJECT_ROOT / "data"
DATABASE_PATH: Final[Path] = DATA_DIR / "tool_nexus.sqlite3"

# ツールの種別。streamlit のときだけ --server.* を自動付与する。
KIND_STREAMLIT: Final[str] = "streamlit"
KIND_EXE: Final[str] = "exe"
KIND_LABELS: Final[dict[str, str]] = {
    KIND_STREAMLIT: "Streamlit",
    KIND_EXE: "その他",
}
KIND_VALUES: Final[tuple[str, ...]] = tuple(KIND_LABELS)

# 死活監視モード。
HEALTH_HTTP: Final[str] = "http"
HEALTH_PROCESS: Final[str] = "process"
HEALTH_NONE: Final[str] = "none"
HEALTH_MODE_LABELS: Final[dict[str, str]] = {
    HEALTH_HTTP: "HTTP",
    HEALTH_PROCESS: "プロセス",
    HEALTH_NONE: "監視しない",
}
HEALTH_MODE_VALUES: Final[tuple[str, ...]] = tuple(HEALTH_MODE_LABELS)

# 種別ごとの死活監視モードの既定値。
# exe はポートを持たないことが多いため、http の「ポート必須」を踏まないよう process にする。
DEFAULT_HEALTH_MODE_BY_KIND: Final[dict[str, str]] = {
    KIND_STREAMLIT: HEALTH_HTTP,
    KIND_EXE: HEALTH_PROCESS,
}

# ログ出力先が未指定のとき、作業ディレクトリ配下に作るファイル名。
DEFAULT_LOG_FILENAME: Final[str] = "tool-nexus.log"

MAX_NAME_LENGTH: Final[int] = 200
MIN_PORT: Final[int] = 1
MAX_PORT: Final[int] = 65535

# TOOL NEXUS 自身のポート（.streamlit/config.toml と合わせる）。
# 自動割当の範囲外に置き、さらに設定に関わらず常に割当候補から除外する
# （停止中に他のツールへ割り当てると、TOOL NEXUS 自身が起動できなくなるため）。
TOOL_NEXUS_PORT: Final[int] = 8499

# ログ画面で表示する末尾の行数と、読み込む最大バイト数。
LOG_TAIL_LINES: Final[int] = 300
LOG_TAIL_BYTES: Final[int] = 256 * 1024

# settings テーブルの既定値。値は文字列で保存する。
DEFAULT_SETTINGS: Final[dict[str, str]] = {
    "health_interval": "60s",
    "port_range_low": "8500",
    "port_range_high": "8999",
    "default_log_dir": "",
    "health_timeout": "2.0",
    # 8501 は LIST NEXUS が使用（同じPCで両方動かす前提）
    "reserved_ports": "8501",
}


def kind_label(value: str) -> str:
    """種別の内部値を表示ラベルへ変換する。未知の値はそのまま返す。"""
    return KIND_LABELS.get(value, value)


def health_mode_label(value: str) -> str:
    """死活監視モードの内部値を表示ラベルへ変換する。未知の値はそのまま返す。"""
    return HEALTH_MODE_LABELS.get(value, value)
