@echo off
rem Abre el motor en este equipo: http://127.0.0.1:8765
rem Para que otros equipos de la red lo usen: INICIAR.bat red  (y entrar a http://IP-de-este-equipo:8765)
cd /d "%~dp0"
if not exist .venv (
  python -m venv .venv
  .venv\Scripts\python -m pip install -r requirements.txt
)
start "" http://127.0.0.1:8765
if /i "%1"=="red" (
  .venv\Scripts\python -X utf8 -m motor web --host 0.0.0.0
) else (
  .venv\Scripts\python -X utf8 -m motor web
)
