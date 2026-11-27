@echo off
REM 급등 패턴 스캔 + 횡보 후 거래량 증가 스캔 (Windows). 바이낸스 공개 API 사용, 키 불필요.
REM 예) run_scan.bat --top 5 --pick 20      run_scan.bat --mode range --range-hours 48 --vol-mult 1.5
cd /d %~dp0
python -m pip install -q -r requirements.txt
python analyzer\pattern_scan.py %*
echo.
echo 결과: analyzer\out\scan_*.md, analyzer\out\range_*.md   패턴 기록: analyzer\patterns\
pause
