@echo off
REM 사용 가능한 파이썬을 찾아 PY 변수에 넣음. 못 찾으면 안내 후 errorlevel 1.
set "PY="
py -3 -c "import sys" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "import sys" >nul 2>&1 && set "PY=python"
if not defined PY python3 -c "import sys" >nul 2>&1 && set "PY=python3"
if defined PY exit /b 0
echo.
echo [오류] 파이썬을 찾지 못했습니다.
echo   1. https://www.python.org/downloads/ 에서 Python 3.11 이상 설치
echo      설치 첫 화면에서 "Add python.exe to PATH" 반드시 체크
echo   2. 설정 ^> 앱 ^> 고급 앱 설정 ^> 앱 실행 별칭 에서
echo      "앱 설치 관리자 python.exe / python3.exe" 를 끔 (Microsoft Store 바로가기)
echo   3. 이 창을 닫고 새 명령 프롬프트에서 다시 실행
echo.
exit /b 1
