@echo off
title Local AI Gateway - close this window to stop everything
cd /d "%~dp0"
engine\comfyui\.venv\Scripts\python.exe app\gateway.py
echo.
echo Gateway stopped.
pause
