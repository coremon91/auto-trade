@echo off
REM 코인 스캐너 웹 대시보드 (Windows). 실행하면 브라우저에서 http://localhost:8000 이 열림. 종료: 이 창에서 Ctrl+C
cd /d %~dp0
python -m pip install -q -r requirements.txt
python analyzer\web.py %*
pause
