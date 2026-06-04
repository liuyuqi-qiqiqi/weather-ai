@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo   [W] AI Dynamic Weather Query Service
echo   Local: http://localhost:5000
echo   Tunnel: use cpolar to map localhost:5000
echo ============================================================

:: 检查环境变量
if "%DASHSCOPE_API_KEY%"=="" (
    echo [WARN] DASHSCOPE_API_KEY not set - Qwen AI will be unavailable
)
if "%HEFENG_API_KEY%"=="" (
    echo [WARN] HEFENG_API_KEY not set - Weather API will fallback
)

echo.
echo Starting server...
python test.py
pause
