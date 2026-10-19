@echo off
cd /d "%~dp0"
title Painel Amigas Fashion
python servidor.py
if errorlevel 1 py servidor.py
pause
