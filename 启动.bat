@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo   自媒体智能运营助手
echo ============================================================
echo.
echo   正在启动，请稍候...
echo   浏览器会在几秒后自动打开。
echo.
echo   [重要] 这个黑窗口请不要关闭，关闭它网页就打不开了。
echo          用完后直接关掉这个窗口即可。
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 没有找到 python，请先安装 Python 3.11 并勾选 "Add to PATH"。
    pause
    exit /b 1
)

rem 8 秒后自动打开浏览器（此时服务已就绪）
start "" cmd /c "timeout /t 8 >nul & start "" http://127.0.0.1:8501"

python -m streamlit run app.py --server.headless=true --server.port=8501

echo.
echo 服务已停止。如果刚才网页打不开，请把上面的错误信息截图给我。
pause
