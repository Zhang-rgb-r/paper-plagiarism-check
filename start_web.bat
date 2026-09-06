@echo off
chcp 65001 >nul
title 论文查重(本地)
python "%~dp0scripts\webapp.py" --port 8765
pause
