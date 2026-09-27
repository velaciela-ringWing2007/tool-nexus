@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Python仮想環境がありません。
    echo setup.batを先に実行してください。
    pause
    exit /b 1
)

rem 待ち受けのアドレス・ポート（8499）などは .streamlit\config.toml で指定している。
rem このウィンドウを閉じるとTOOL NEXUSは終了するが、TOOL NEXUSから起動したツールは動き続ける。
start "" http://127.0.0.1:8499

".venv\Scripts\python.exe" -m streamlit run app.py
