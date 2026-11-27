@echo off
REM 국면별 백테스트 실행 (Windows). 처음 실행 시 데이터 수집에 30~60분 걸림. 이후엔 캐시 사용.
cd /d %~dp0
python -m pip install -q -r requirements.txt
python backtest\regime_bt.py all --workers 32 %*
echo.
echo 결과: backtest\results_regime\summary.md
pause
