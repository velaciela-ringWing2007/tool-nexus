#!/usr/bin/env bash
# Linux 用の起動スクリプト。待ち受けのアドレス・ポート（8499）は .streamlit/config.toml で指定している。
# この端末を閉じると TOOL NEXUS は終了するが、TOOL NEXUS から起動したツールは動き続ける。
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
    echo "Python仮想環境がありません。./setup.sh を先に実行してください。"
    exit 1
fi

URL="http://127.0.0.1:8499"
if command -v xdg-open >/dev/null 2>&1; then
    # サーバーが立ち上がってからブラウザを開く
    (sleep 3 && xdg-open "$URL" >/dev/null 2>&1) &
else
    echo "ブラウザで $URL を開いてください。"
fi

exec .venv/bin/python -m streamlit run app.py
