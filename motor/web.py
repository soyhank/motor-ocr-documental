"""Interfaz web: subir órdenes, revisar lo extraído sobre el documento y descargar Excel.

Sin estado en el servidor: cada petición es independiente (funciona igual en local y en
Vercel). El navegador guarda los resultados y las imágenes de cada página.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import secrets
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel

from . import notificar
from .calibrar import borrador_plantilla, prompt_llm
from .exportar import libro
from .ingesta import ingerir
from .pipeline import EXT_AUDIO, _estado, _resumen, cruzar_duplicados, procesar, repo
from .plantillas import RAIZ
from .salida import revalidar_datos, revalidar_simple, salida_simple
from .validar import cargar_config

DESCRIPCION_API = """
Lee órdenes de publicidad (PDF digitales, escaneados o fotos), las valida y devuelve un JSON
limpio con **cabecera**, **detalle** y **totales** listo para otro sistema.

* `POST /api/v1/ordenes`: sube un archivo y recibe la orden extraída y validada.
* `POST /api/v1/ordenes/validar`: envía la orden corregida a mano y recibe la validación al día.
* `GET /api/v1/plantillas`: formatos de agencia que el motor ya reconoce.

Si la variable de entorno `MOTOR_API_KEY` (o `config.json -> api.claves`) está definida, cada
llamada a `/api/v1` debe llevar la cabecera `X-API-Key`.
"""

app = FastAPI(title="Nítida · Motor OCR Documental", version="3.0", description=DESCRIPCION_API)


# ------------------------------------------------------------------ inicio de sesión
# Usuario y contraseña por defecto: admin / 123456789. Se cambian con MOTOR_USUARIO y
# MOTOR_CONTRASENA; MOTOR_LOGIN=0 lo desactiva (por ejemplo detrás de otro control de acceso).
# La sesión es una cookie firmada (HMAC-SHA256) que dura 12 horas. /api/v1 usa X-API-Key.
COOKIE = "nitida_sesion"
DURACION_SESION = 12 * 3600
LIBRES = ("/login", "/api/login", "/api/salud", "/api/v1", "/favicon.svg", "/icono.svg")
_intentos: dict[str, list[float]] = {}


def _credenciales() -> tuple[str, str]:
    return os.environ.get("MOTOR_USUARIO", "admin"), os.environ.get("MOTOR_CONTRASENA", "123456789")


def _login_activo() -> bool:
    return os.environ.get("MOTOR_LOGIN", "1") != "0"


def _secreto() -> bytes:
    """Clave para firmar la sesión: MOTOR_SECRETO o una aleatoria guardada en salida/ (así un
    reinicio del servidor no cierra las sesiones abiertas)."""
    if os.environ.get("MOTOR_SECRETO"):
        return os.environ["MOTOR_SECRETO"].encode()
    if not hasattr(_secreto, "valor"):
        ruta = RAIZ / "salida" / ".secreto_sesion"
        try:
            _secreto.valor = ruta.read_bytes()
        except OSError:
            _secreto.valor = secrets.token_bytes(32)
            try:
                ruta.parent.mkdir(exist_ok=True)
                ruta.write_bytes(_secreto.valor)
            except OSError:
                pass  # disco de solo lectura (Vercel): la clave vive mientras viva el proceso
    return _secreto.valor


def _firmar(usuario: str, vence: int) -> str:
    cuerpo = f"{usuario}|{vence}"
    firma = hmac.new(_secreto(), cuerpo.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{cuerpo}|{firma}".encode()).decode()


def _usuario_de(cookie: str | None) -> str | None:
    if not cookie:
        return None
    try:
        usuario, vence, firma = base64.urlsafe_b64decode(cookie.encode()).decode().rsplit("|", 2)
    except Exception:  # noqa: BLE001
        return None
    esperada = hmac.new(_secreto(), f"{usuario}|{vence}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(firma, esperada) or int(vence) < time.time():
        return None
    return usuario


@app.middleware("http")
async def _sesion(request, call_next):
    ruta = request.url.path
    if not _login_activo() or ruta.startswith(LIBRES):
        return await call_next(request)
    usuario = _usuario_de(request.cookies.get(COOKIE))
    if usuario:
        request.state.usuario = usuario
        return await call_next(request)
    if ruta.startswith("/api") or ruta in ("/docs", "/openapi.json", "/redoc"):
        return JSONResponse({"detail": "Inicia sesión para continuar"}, status_code=401)
    return RedirectResponse("/login" + (f"?next={quote(ruta)}" if ruta != "/" else ""), status_code=303)


class Ingreso(BaseModel):
    usuario: str
    contrasena: str
    recordar: bool = True


@app.get("/login")
def pagina_login():
    return FileResponse(RAIZ / "web" / "login.html")


@app.post("/api/login")
def api_login(body: Ingreso, request: Request):
    ip = request.client.host if request.client else "?"
    ahora = time.time()
    fallos = [t for t in _intentos.get(ip, []) if ahora - t < 300]
    if len(fallos) >= 8:
        espera = int(300 - (ahora - fallos[0])) + 1
        return JSONResponse({"detail": f"Demasiados intentos. Prueba de nuevo en {espera} s."}, status_code=429)
    usuario, clave = _credenciales()
    ok = hmac.compare_digest(body.usuario.strip().lower(), usuario.lower()) & hmac.compare_digest(body.contrasena, clave)
    if not ok:
        _intentos[ip] = [*fallos, ahora]
        return JSONResponse({"detail": "Usuario o contraseña incorrectos", "restantes": 8 - len(fallos) - 1},
                            status_code=401)
    _intentos.pop(ip, None)
    r = JSONResponse({"ok": True, "usuario": usuario})
    segura = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    r.set_cookie(COOKIE, _firmar(usuario, int(ahora) + DURACION_SESION), httponly=True, samesite="lax",
                 secure=segura, max_age=DURACION_SESION if body.recordar else None)
    return r


@app.post("/api/logout")
def api_logout():
    r = JSONResponse({"ok": True})
    r.delete_cookie(COOKIE)
    return r


@app.get("/api/sesion")
def api_sesion(request: Request):
    return {"usuario": getattr(request.state, "usuario", None), "login": _login_activo()}


@app.get("/icono.svg")
@app.get("/favicon.svg")
def icono():
    return FileResponse(RAIZ / "web" / "icono.svg", media_type="image/svg+xml")


DIR_MUESTRAS = RAIZ / "muestras"
ANCHO_VISTA = 1400  # px de ancho de la imagen que se manda al navegador


def _publico(r: dict) -> dict:
    return {k: v for k, v in r.items() if not k.startswith("_")}


def _imagen_vista(png: bytes | None) -> str | None:
    if not png:
        return None
    try:
        import cv2
        import numpy as np

        img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return None
        if img.shape[1] > ANCHO_VISTA:
            f = ANCHO_VISTA / img.shape[1]
            img = cv2.resize(img, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
        ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 72])
        if not ok:
            return None
        return "data:image/jpeg;base64," + base64.b64encode(jpg.tobytes()).decode()
    except Exception:  # noqa: BLE001
        return None


def _procesar_archivo(nombre: str, datos: bytes, con_imagen: bool = True, avisar: bool = True) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        ruta = Path(tmp) / Path(nombre).name
        ruta.write_bytes(datos)
        r = procesar(ruta, datos, con_imagen=con_imagen)
    doc = r.pop("_doc", None)
    if doc is not None and con_imagen:
        for p, info in zip(doc.paginas, r.get("pages", [])):
            info["image"] = _imagen_vista(p.imagen_png)
    r = _publico(r)
    if r.get("documentId") is None:
        r["documentId"] = None
    r["salida"] = salida_simple(r)
    if avisar:
        r["alerta"] = notificar.enviar(r["salida"])
    return r


@app.get("/")
def inicio():
    return FileResponse(RAIZ / "web" / "index.html")


@app.get("/api/salud")
def salud():
    return {"ok": True, "plantillas": len(repo().todas())}


@app.post("/api/procesar")
async def api_procesar(archivos: list[UploadFile] = File(...), notificar_: int = Query(1, alias="notificar")):
    """Procesa los archivos de la petición y devuelve el resultado completo de cada uno
    (incluida la imagen de cada página). El cruce entre documentos se hace en /api/cruzar."""
    out = []
    for a in archivos:
        datos = await a.read()
        if Path(a.filename).suffix.lower() in EXT_AUDIO:
            continue
        try:
            out.append(_procesar_archivo(a.filename, datos, avisar=bool(notificar_)))
        except Exception as e:  # noqa: BLE001
            r = {"documentId": None, "file": a.filename, "status": "ERROR",
                 "error": {"code": "INTERNO", "message": f"Error inesperado: {e}"}}
            r["salida"] = salida_simple(r)
            out.append(r)
    return JSONResponse(out)


class Revalidacion(BaseModel):
    result: dict
    corrections: list[dict] = []


def _datos_internos(result: dict) -> dict:
    d = result.get("extractedData") or {}
    return {"header": d.get("header") or {}, "detail": d.get("detail") or [], "totals": d.get("totals") or {},
            "footer": d.get("footer") or {}}


@app.post("/api/revalidar")
def api_revalidar(body: Revalidacion):
    """Corrección manual en pantalla: vuelve a validar con los valores editados."""
    r = body.result
    rep, datos = revalidar_datos(_datos_internos(r), (r.get("template") or {}).get("templateId"))
    r = {**r, "extractedData": {k: datos[k] for k in ("header", "detail", "totals", "footer") if k in datos},
         "validationReport": rep, "status": _estado(rep), "corrections": body.corrections}
    if r.get("newFormat") and not (r.get("template") or {}).get("learned") and rep["isValid"]:
        r["status"] = _estado(rep)  # ya cuadra: se puede guardar como formato aprendido
    r["summary"] = _resumen(r["extractedData"])
    r["salida"] = salida_simple(r)
    return JSONResponse({k: r[k] for k in ("status", "validationReport", "extractedData", "summary", "salida", "corrections")})


@app.post("/api/aprender")
async def api_aprender(archivo: UploadFile = File(...), resultado: str = Form(...)):
    """Formato nuevo corregido a mano: aprende la plantilla de esa agencia con lo corregido."""
    from .generica import aprender_de_correccion, guardar_plantilla

    r = json.loads(resultado)
    datos = await archivo.read()
    with tempfile.TemporaryDirectory() as tmp:
        ruta = Path(tmp) / Path(archivo.filename).name
        ruta.write_bytes(datos)
        doc = ingerir(ruta, datos)
    cab = {k: (v.get("value") if isinstance(v, dict) else v) for k, v in ((r.get("extractedData") or {}).get("header") or {}).items()}
    nueva = aprender_de_correccion(doc, cab, r.get("corrections"), cargar_config())
    guardada = guardar_plantilla(nueva)
    if not guardada:
        raise HTTPException(503, "Este servidor no permite guardar plantillas (solo lectura)")
    repo().recargar()
    return {"templateId": nueva["templateId"], "version": nueva["version"], "savedAs": guardada,
            "campos": len(nueva["header"]["fields"]), "reglas": nueva["identification"]["rules"]}


# ------------------------------------------------------------------ API para integrar con otros sistemas

def _clave_api(x_api_key: str | None = Header(None)):
    claves = set(filter(None, [os.environ.get("MOTOR_API_KEY")] + list((cargar_config().get("api") or {}).get("claves") or [])))
    if claves and x_api_key not in claves:
        raise HTTPException(401, "Falta la cabecera X-API-Key o no es válida")


@app.get("/api/v1/salud", tags=["API v1"], dependencies=[Depends(_clave_api)])
def v1_salud():
    return {"ok": True, "plantillas": len(repo().todas()), "version": app.version}


@app.post("/api/v1/ordenes", tags=["API v1"], dependencies=[Depends(_clave_api)])
async def v1_ordenes(archivo: UploadFile = File(..., description="PDF, JPG, PNG o TIFF de la orden"),
                     formato: str = Query("simple", description="'simple' (cabecera, detalle, totales) o 'completo' (con trazabilidad)"),
                     notificar_: bool = Query(True, alias="notificar", description="enviar alerta si la orden falla")):
    """Procesa una orden y devuelve el JSON limpio (o el completo con `formato=completo`)."""
    datos = await archivo.read()
    try:
        r = _procesar_archivo(archivo.filename, datos, con_imagen=False, avisar=notificar_)
    except Exception as e:  # noqa: BLE001
        r = {"documentId": None, "file": archivo.filename, "status": "ERROR",
             "error": {"code": "INTERNO", "message": f"Error inesperado: {e}"}}
        r["salida"] = salida_simple(r)
    if formato == "completo":
        return JSONResponse(r)
    return JSONResponse(r["salida"])


@app.post("/api/v1/ordenes/validar", tags=["API v1"], dependencies=[Depends(_clave_api)])
def v1_validar(orden: dict):
    """Recibe la orden en el formato limpio (por ejemplo, corregida por un asesor) y devuelve
    la misma orden con la validación y el estado recalculados."""
    out = revalidar_simple(orden)
    return JSONResponse(out)


@app.get("/api/v1/plantillas", tags=["API v1"], dependencies=[Depends(_clave_api)])
def v1_plantillas():
    return api_plantillas()


@app.post("/api/notificaciones/prueba")
def api_prueba_alerta(webhook: str | None = None):
    """Envía una alerta de prueba a los destinos configurados."""
    return notificar.probar(webhook)


class Lote(BaseModel):
    results: list[dict]
    audios: list[dict] = []  # [{"file": "...", "seconds": 25.1}]


def _cruzar_audios(resultados: list[dict], audios: list[dict]):
    from rapidfuzz import fuzz
    import re

    for a in audios:
        dur = a.get("seconds")
        nombre = re.sub(r"[_\-]+", " ", Path(a["file"]).stem).upper()
        for r in resultados:
            filas = (r.get("extractedData") or {}).get("detail", [])
            for i, f in enumerate(filas):
                texto = " ".join(str((f.get(k) or {}).get("value") or "") for k in ("motivo", "descripcion")).upper()
                if not texto.strip() or fuzz.token_set_ratio(nombre, texto) < 85:
                    continue
                seg = (f.get("duracion") or {}).get("value")
                ok = dur is not None and seg is not None and abs(dur - seg) <= 1.0
                rep = r.get("validationReport")
                if not rep:
                    continue
                if any(x["rule"] == f"MATERIAL_DURACION({a['file']})" and x.get("row") == i + 1 for x in rep["executedRules"]):
                    continue
                rep["executedRules"].append({
                    "rule": f"MATERIAL_DURACION({a['file']})", "row": i + 1, "severity": "WARNING",
                    "status": "PASSED" if ok else "FAILED", "passed": ok,
                    "description": "La duración del audio coincide con la duración pautada",
                    "detail": f"audio {dur} s, orden {seg} s"})
                if ok:
                    rep["passed"] += 1
                else:
                    rep["warnings"] += 1
                    r["status"] = _estado(rep)
                r.setdefault("materials", []).append({"file": a["file"], "seconds": dur, "row": i + 1, "match": ok})


@app.post("/api/cruzar")
def api_cruzar(lote: Lote):
    """Revisiones entre documentos: duplicados y duración de los audios frente a la pauta."""
    resultados = lote.results
    cruzar_duplicados(resultados)
    if lote.audios:
        _cruzar_audios(resultados, lote.audios)
    return JSONResponse([{"documentId": r.get("documentId"), "status": r.get("status"),
                          "validationReport": r.get("validationReport"), "materials": r.get("materials")}
                         for r in resultados])


@app.post("/api/excel")
def api_excel(lote: Lote):
    if not lote.results:
        raise HTTPException(400, "No hay documentos procesados")
    buf = io.BytesIO()
    libro(lote.results).save(buf)
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": 'attachment; filename="ordenes_extraidas.xlsx"'})


@app.post("/api/calibracion")
async def api_calibracion(archivo: UploadFile = File(...), tipo: str = "borrador"):
    """Borrador de plantilla (o texto de calibración) para un documento de formato nuevo."""
    datos = await archivo.read()
    with tempfile.TemporaryDirectory() as tmp:
        ruta = Path(tmp) / Path(archivo.filename).name
        ruta.write_bytes(datos)
        doc = ingerir(ruta, datos)
    borr = borrador_plantilla(doc)
    if tipo == "prompt":
        ejemplo = next(iter(repo().todas()), None)
        ejemplo = {k: v for k, v in (ejemplo or {}).items() if not k.startswith("_")}
        return Response(prompt_llm(doc, borr, ejemplo), media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="prompt_calibracion.md"'})
    return Response(json.dumps(borr, ensure_ascii=False, indent=2), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{borr["templateId"]}.json"'})


@app.get("/api/muestras")
def api_muestras():
    """Nombres de los archivos de ejemplo incluidos en el proyecto."""
    if not DIR_MUESTRAS.exists():
        return []
    return [{"name": p.name, "size": p.stat().st_size} for p in sorted(DIR_MUESTRAS.iterdir()) if p.is_file()]


@app.get("/api/muestras/{nombre}")
def api_muestra(nombre: str):
    ruta = DIR_MUESTRAS / Path(nombre).name
    if not ruta.exists():
        raise HTTPException(404)
    return FileResponse(ruta)


@app.get("/api/plantillas")
def api_plantillas():
    return [{"templateId": p["templateId"], "version": p.get("version", 1), "description": p.get("description"),
             "aprendida": p.get("origin") == "auto", "aprendidaDe": (p.get("learnedFrom") or {}).get("file"),
             "archivo": p.get("_archivo"), "campos": len(p.get("header", {}).get("fields", [])) +
             len(p.get("totals", {}).get("fields", [])),
             "columnas": len(p.get("detail", {}).get("headerAnchors", []))} for p in repo().todas()]
