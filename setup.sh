#!/usr/bin/env bash
# Linux 用セットアップ: 仮想環境の作成・依存パッケージのインストール・DB初期化
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 が見つかりません。Python 3.12以上をインストールしてください。"
    exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
    echo "仮想環境を作成しています..."
    if ! python3 -m venv .venv; then
        echo "仮想環境の作成に失敗しました。Ubuntu では python3-venv が必要です（sudo apt install python3-venv）。"
        exit 1
    fi
fi

.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

echo "データベースを初期化しています..."
.venv/bin/python -c "from tool_nexus.core.constants import DATABASE_PATH; from tool_nexus.core.repositories import ToolRepository; ToolRepository(DATABASE_PATH).initialize(); print('OK:', DATABASE_PATH)"

if ! command -v zenity >/dev/null 2>&1 && ! command -v kdialog >/dev/null 2>&1; then
    echo
    echo "注意: ファイル選択（「ファイルから入力」）には zenity か kdialog が必要です。"
    echo "      例: sudo apt install zenity"
fi

echo
echo "セットアップが完了しました。./start-tool-nexus.sh で起動します。"
