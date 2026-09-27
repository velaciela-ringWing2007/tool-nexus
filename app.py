"""TOOL NEXUS - ローカルツールのランチャー（起動の入口）.

`streamlit run app.py` で起動する。画面の実装は tool_nexus/ui にある。
このファイルをリポジトリのルートに置くことで、tool_nexus パッケージをそのまま import できる。
"""

from tool_nexus.ui.main import main

if __name__ == "__main__":
    main()
