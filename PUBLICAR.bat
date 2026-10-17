@echo off
rem Abre el motor a internet desde esta PC con un tunel temporal de Cloudflare (gratis, sin cuenta).
rem   PUBLICAR.bat              -> se entra con usuario admin y contrasena 123456789
rem   PUBLICAR.bat MiClave123   -> usuario admin y contrasena MiClave123; la API usa X-API-Key: MiClave123
rem La direccion https://....trycloudflare.com aparece abajo, en las lineas de cloudflared.
rem Funciona mientras esta ventana y la PC esten encendidas; al cerrarla, la direccion deja de existir.
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv (
  python -m venv .venv
  .venv\Scripts\python -m pip install -r requirements.txt
)
rem cloudflared portatil oficial (firmado por Cloudflare, Inc.), se descarga una sola vez
if not exist herramientas\cloudflared.exe (
  mkdir herramientas 2>nul
  curl -L -o herramientas\cloudflared.exe https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe
)

set "MOTOR_API_KEY="
set "MOTOR_USUARIO=admin"
set "MOTOR_CONTRASENA=123456789"
if not "%~1"=="" (
  set "MOTOR_CONTRASENA=%~1"
  set "MOTOR_API_KEY=%~1"
)
echo.
echo ============================================================
echo  Usuario: admin    Contrasena: %MOTOR_CONTRASENA%
echo ============================================================
echo.

start "Nitida (no cerrar)" /min .venv\Scripts\python -X utf8 -m motor web --host 127.0.0.1 --puerto 8765
timeout /t 8 >nul
herramientas\cloudflared.exe tunnel --no-autoupdate --url http://127.0.0.1:8765
