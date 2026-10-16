@echo off
rem Abre el motor a internet desde esta PC con un tunel temporal de Cloudflare (gratis, sin cuenta).
rem   PUBLICAR.bat              -> acceso libre, sin contrasena
rem   PUBLICAR.bat MiClave123   -> pide esa contrasena (usuario cualquiera); la API usa X-API-Key: MiClave123
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

set "MOTOR_CLAVE_WEB="
set "MOTOR_API_KEY="
echo.
echo ============================================================
if "%~1"=="" (
  echo  Acceso libre: cualquiera con la direccion puede entrar.
) else (
  set "MOTOR_CLAVE_WEB=%~1"
  set "MOTOR_API_KEY=%~1"
  echo  Contrasena para entrar: %~1   ^(usuario: cualquiera^)
)
echo ============================================================
echo.

start "Motor OCR (no cerrar)" /min .venv\Scripts\python -X utf8 -m motor web --host 127.0.0.1 --puerto 8765
timeout /t 8 >nul
herramientas\cloudflared.exe tunnel --no-autoupdate --url http://127.0.0.1:8765
