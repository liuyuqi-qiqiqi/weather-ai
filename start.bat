@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo   [W] AI Dynamic Weather Query Service
echo ============================================================

:: 1. 从 .env 文件自动加载环境变量
if exist ".env" (
    echo [OK] Loading .env configuration...
    for /f "tokens=1,2 delims==" %%a in (.env) do (
        set "line=%%a%%b"
        echo %%a | findstr /r "^#" >nul
        if errorlevel 1 (
            if not "%%b"=="" set "%%a=%%b"
        )
    )
    echo [OK] Environment variables loaded from .env
) else (
    echo [WARN] .env file not found
)

echo.
echo   Local:  http://localhost:5000
echo   Tunnel: cpolar http 5000
echo ============================================================
echo.
echo Starting server...
python test.py
pause
