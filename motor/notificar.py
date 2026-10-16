"""Alertas cuando una orden falla: webhook (JSON; sirve para Slack, Teams o el sistema que lo
reciba) y correo por SMTP. Se envían en segundo plano para no demorar la respuesta.

Configuración en config.json -> "notificaciones" (o variables de entorno, que tienen prioridad):

    "notificaciones": {
      "eventos": ["ERROR", "VALIDATION_FAILED", "NEW_FORMAT_REVIEW"],   # estados que avisan
      "webhook": "https://...",                    # MOTOR_WEBHOOK_URL
      "correo": {
        "servidor": "smtp.gmail.com", "puerto": 587,      # MOTOR_SMTP_SERVIDOR / _PUERTO
        "usuario": "alertas@empresa.com",                 # MOTOR_SMTP_USUARIO
        "clave_env": "MOTOR_SMTP_CLAVE",                  # la clave nunca va en el archivo
        "de": "alertas@empresa.com", "para": ["asesor@empresa.com"]   # MOTOR_SMTP_PARA (coma)
      }
    }

Cada envío queda anotado en salida/notificaciones.log.
"""
from __future__ import annotations

import json
import os
import smtplib
import threading
import urllib.request
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from .validar import RAIZ, cargar_config

EVENTOS_POR_DEFECTO = ["ERROR", "VALIDATION_FAILED", "NEW_FORMAT_REVIEW"]
REGISTRO = RAIZ / "salida" / "notificaciones.log"


def _config() -> dict:
    c = dict(cargar_config().get("notificaciones") or {})
    if os.environ.get("MOTOR_WEBHOOK_URL"):
        c["webhook"] = os.environ["MOTOR_WEBHOOK_URL"]
    correo = dict(c.get("correo") or {})
    for clave, env in (("servidor", "MOTOR_SMTP_SERVIDOR"), ("puerto", "MOTOR_SMTP_PUERTO"),
                       ("usuario", "MOTOR_SMTP_USUARIO"), ("de", "MOTOR_SMTP_DE")):
        if os.environ.get(env):
            correo[clave] = os.environ[env]
    if os.environ.get("MOTOR_SMTP_PARA"):
        correo["para"] = [x.strip() for x in os.environ["MOTOR_SMTP_PARA"].split(",") if x.strip()]
    if correo:
        c["correo"] = correo
    return c


def _anotar(texto: str):
    try:
        REGISTRO.parent.mkdir(exist_ok=True)
        with REGISTRO.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {texto}\n")
    except OSError:
        pass


def mensaje(simple: dict) -> tuple[str, str]:
    """Asunto y cuerpo legibles para la alerta."""
    d = simple.get("documento") or {}
    c = simple.get("cabecera") or {}
    t = simple.get("totales") or {}
    v = simple.get("validacion") or {}
    asunto = f"[Motor OCR] {d.get('estadoTexto')}: {d.get('archivo')}"
    lineas = [f"Archivo: {d.get('archivo')}", f"Estado: {d.get('estadoTexto')}",
              f"Orden: {c.get('numero_orden') or '—'}   Agencia: {c.get('agencia') or '—'}   Anunciante: {c.get('anunciante') or '—'}",
              f"Total: {t.get('total') if t.get('total') is not None else '—'} {t.get('moneda') or ''}"]
    if d.get("error"):
        lineas.append(f"Error: {d['error'].get('message')}")
    for e in (v.get("errores") or [])[:10]:
        lineas.append(f"  ✗ {e.get('mensaje')}" + (f" (fila {e['fila']})" if e.get("fila") else "") +
                      (f": {e['detalle']}" if e.get("detalle") else ""))
    for o in (v.get("observaciones") or [])[:10]:
        lineas.append(f"  ! {o.get('mensaje')}" + (f": {o['detalle']}" if o.get("detalle") else ""))
    if d.get("formatoNuevo") and not d.get("plantillaAprendida"):
        lineas.append("Formato nuevo: revisar los datos en pantalla y guardar la corrección para que el motor lo aprenda.")
    return asunto, "\n".join(lineas)


def _webhook(url: str, simple: dict, asunto: str, cuerpo: str):
    payload = {"text": f"*{asunto}*\n{cuerpo}", "evento": (simple.get("documento") or {}).get("estado"),
               "orden": simple}
    req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status


def _correo(cfg: dict, asunto: str, cuerpo: str):
    clave = os.environ.get(cfg.get("clave_env") or "MOTOR_SMTP_CLAVE", "")
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = asunto, cfg.get("de") or cfg.get("usuario"), ", ".join(cfg["para"])
    msg.set_content(cuerpo)
    puerto = int(cfg.get("puerto", 587))
    with smtplib.SMTP(cfg["servidor"], puerto, timeout=20) as s:
        if cfg.get("tls", puerto == 587):
            s.starttls()
        if cfg.get("usuario") and clave:
            s.login(cfg["usuario"], clave)
        s.send_message(msg)


def enviar(simple: dict, esperar: bool = False) -> dict:
    """Envía la alerta si el estado de la orden es uno de los eventos configurados."""
    cfg = _config()
    estado = (simple.get("documento") or {}).get("estado")
    if estado not in (cfg.get("eventos") or EVENTOS_POR_DEFECTO):
        return {"enviado": False, "motivo": "estado sin alerta"}
    if not cfg.get("webhook") and not (cfg.get("correo") or {}).get("para"):
        return {"enviado": False, "motivo": "sin destinos configurados"}
    asunto, cuerpo = mensaje(simple)
    resultado: dict = {"enviado": True, "destinos": []}

    def trabajo():
        if cfg.get("webhook"):
            try:
                st = _webhook(cfg["webhook"], simple, asunto, cuerpo)
                resultado["destinos"].append({"webhook": st})
                _anotar(f"webhook {st}  {asunto}")
            except Exception as e:  # noqa: BLE001
                resultado["destinos"].append({"webhook": f"error: {e}"})
                _anotar(f"webhook ERROR {e}  {asunto}")
        if (cfg.get("correo") or {}).get("para") and (cfg.get("correo") or {}).get("servidor"):
            try:
                _correo(cfg["correo"], asunto, cuerpo)
                resultado["destinos"].append({"correo": "ok"})
                _anotar(f"correo ok -> {', '.join(cfg['correo']['para'])}  {asunto}")
            except Exception as e:  # noqa: BLE001
                resultado["destinos"].append({"correo": f"error: {e}"})
                _anotar(f"correo ERROR {e}  {asunto}")

    if esperar:
        trabajo()
    else:
        threading.Thread(target=trabajo, daemon=True).start()
    return resultado


def probar(destino: str | None = None) -> dict:
    """Manda una alerta de prueba a los destinos configurados (o a `destino` si es una URL)."""
    simple = {"documento": {"archivo": "prueba.pdf", "estado": "VALIDATION_FAILED", "estadoTexto": "No cuadra (prueba)"},
              "cabecera": {"numero_orden": "000000", "agencia": "Agencia de prueba"}, "totales": {"total": 0, "moneda": "PEN"},
              "validacion": {"ok": False, "errores": [{"mensaje": "Mensaje de prueba del Motor OCR"}], "observaciones": []}}
    if destino:
        os.environ["MOTOR_WEBHOOK_URL"] = destino
    return enviar(simple, esperar=True)
