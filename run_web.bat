@echo off
chcp 65001 >nul
set PYTHONUTF8=1
REM 코인 스캐너 웹 대시보드. 브라우저에서 http://localhost:8000 이 열림. 종료: 이 창에서 Ctrl+C
cd /d %~dp0
call scripts\find_python.bat || (pause & exit /b 1)
echo 파이썬: %PY%
%PY% -m pip install -q --disable-pip-version-check --no-warn-script-location -r requirements.txt || (echo [오류] 패키지 설치 실패 & pause & exit /b 1)
%PY% analyzer\web.py %*

pause
