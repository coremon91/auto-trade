@echo off
chcp 65001 >nul
set PYTHONUTF8=1
REM 국면별 백테스트. 처음 실행 시 데이터 수집에 30~60분, 이후엔 캐시 사용.
cd /d %~dp0
call scripts\find_python.bat || (pause & exit /b 1)
echo 파이썬: %PY%
%PY% -m pip install -q --disable-pip-version-check --no-warn-script-location -r requirements.txt || (echo [오류] 패키지 설치 실패 & pause & exit /b 1)
%PY% backtest\regime_bt.py all --workers 32 %*
echo 결과: backtest\results_regime\summary.md
pause
