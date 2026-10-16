@echo off
rem Abre el motor a internet desde esta PC con un tunel temporal de Cloudflare (gratis, sin cuenta).
rem   PUBLICAR.bat              -> contrasena aleatoria
rem   PUBLICAR.bat MiClave123   -> esa contrasena
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

if "%~1"=="" (
  for /f %%c in ('powershell -NoProfile -Command "-join ((48..57)+(97..122) | Get-Random -Count 10 | ForEach-Object {[char]$_})"') do set "MOTOR_CLAVE_WEB=%%c"
) else (
  set "MOTOR_CLAVE_WEB=%~1"
)
set "MOTOR_API_KEY=%MOTOR_CLAVE_WEB%"

echo.
echo ============================================================
echo  Contrasena para entrar: %MOTOR_CLAVE_WEB%
echo  (el navegador pide usuario y contrasena: usuario cualquiera)
echo  Para la API: cabecera X-API-Key: %MOTOR_CLAVE_WEB%
echo ============================================================
echo.

start "Motor OCR (no cerrar)" /min .venv\Scripts\python -X utf8 -m motor web --host 127.0.0.1 --puerto 8765
timeout /t 8 >nul
herramientas\cloudflared.exe tunnel --no-autoupdate --url http://127.0.0.1:8765
