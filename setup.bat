@echo off
chcp 65001 >nul
cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
    echo Pythonが見つかりません。Python 3.12以上をインストールしてください。
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo 仮想環境を作成しています...
    python -m venv .venv
    if errorlevel 1 (
        echo 仮想環境の作成に失敗しました。
        pause
        exit /b 1
    )
)

".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 (
    echo pipの更新に失敗しました。
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo 依存パッケージのインストールに失敗しました。
    pause
    exit /b 1
)

echo データベースを初期化しています...
".venv\Scripts\python.exe" -c "from tool_nexus.core.constants import DATABASE_PATH; from tool_nexus.core.repositories import ToolRepository; ToolRepository(DATABASE_PATH).initialize(); print('OK:', DATABASE_PATH)"
if errorlevel 1 (
    echo データベースの初期化に失敗しました。
    pause
    exit /b 1
)

echo.
echo セットアップが完了しました。
echo start-tool-nexus.bat を実行するとアプリが起動します。
pause
