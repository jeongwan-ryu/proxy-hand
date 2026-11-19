@echo off
rem 노트북 서비스 3개를 창 하나씩 띄운다 (시스템 설계 3.5)
rem 먼저 저장소 폴더에서 python -m venv .venv 후 pip install -r laptop\requirements.txt
chcp 65001 > nul
cd /d "%~dp0.."
set PY=.venv\Scripts\python.exe
start "hand_service" cmd /k %PY% -m laptop.hand_service.main
start "vision_service" cmd /k %PY% -m laptop.vision_service.main
start "control_service" cmd /k %PY% -m laptop.control_service.main
echo 브라우저로 http://localhost:8000 에 접속하세요.
