@echo off
chcp 65001 >nul
set PYTHONUTF8=1
REM 급등 패턴 + 횡보 후 거래량 증가 스캔. 예) run_scan.bat --mode range --range-hours 48 --vol-mult 1.5
cd /d %~dp0
call scripts\find_python.bat || (pause & exit /b 1)
echo 파이썬: %PY%
%PY% -m pip install -q -r requirements.txt || (echo [오류] 패키지 설치 실패 & pause & exit /b 1)
%PY% analyzer\pattern_scan.py %*
echo 결과: analyzer\out\   패턴 기록: analyzer\patterns\
pause
