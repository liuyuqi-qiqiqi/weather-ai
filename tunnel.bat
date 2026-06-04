@echo off
chcp 65001 >nul
echo ============================================================
echo   Starting cpolar tunnel - Weather AI
echo   Local:  http://localhost:5000
echo   Tunnel: check cpolar dashboard for public URL
echo ============================================================
echo.
"C:\Users\liuyu\cpolar\cpolar\cpolar.exe" http 5000
pause
