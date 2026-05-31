@echo off
chcp 65001 >nul
cd /d "%~dp0"
python news_pusher.py once >> news_pusher.log 2>&1
