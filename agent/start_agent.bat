@echo off
chcp 65001 > nul
title QR Se Print - Windows Print Agent
color 0A
cd /d "%~dp0"
echo ======================================================
echo       QR Se Print - Auto Print Desktop Agent
echo ======================================================
echo.
python print_agent.py
echo.
echo ======================================================
echo Agent stopped. Press any key to close this window.
echo ======================================================
pause
