"""Interfaz web: subir órdenes, revisar lo extraído sobre el documento y descargar Excel.

Sin estado en el servidor: cada petición es independiente (funciona igual en local y en
Vercel). El navegador guarda los resultados y las imágenes de cada página.
"""
from __future__ import annotations

import base64
import io
import json
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from .calibrar import borrador_plantilla, prompt_llm
from .exportar import libro
from .ingesta import ingerir
from .pipeline import EXT_AUDIO, _estado, cruzar_duplicados, procesar, repo
from .plantillas import RAIZ

app = FastAPI(title="Motor OCR Documental")
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


def _procesar_archivo(nombre: str, datos: bytes) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        ruta = Path(tmp) / Path(nombre).name
        ruta.write_bytes(datos)
        r = procesar(ruta, datos, con_imagen=True)
    doc = r.pop("_doc", None)
    if doc is not None:
        for p, info in zip(doc.paginas, r.get("pages", [])):
            info["image"] = _imagen_vista(p.imagen_png)
    return _publico(r)


@app.get("/")
def inicio():
    return FileResponse(RAIZ / "web" / "index.html")


@app.get("/api/salud")
def salud():
    return {"ok": True, "plantillas": len(repo().todas())}


@app.post("/api/procesar")
async def api_procesar(archivos: list[UploadFile] = File(...)):
    """Procesa los archivos de la petición y devuelve el resultado completo de cada uno
    (incluida la imagen de cada página). El cruce entre documentos se hace en /api/cruzar."""
    out = []
    for a in archivos:
        datos = await a.read()
        if Path(a.filename).suffix.lower() in EXT_AUDIO:
            continue
        try:
            out.append(_procesar_archivo(a.filename, datos))
        except Exception as e:  # noqa: BLE001
            out.append({"documentId": None, "file": a.filename, "status": "ERROR", "error": str(e)})
    return JSONResponse(out)


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
             "archivo": p.get("_archivo"), "campos": len(p.get("header", {}).get("fields", [])) +
             len(p.get("totals", {}).get("fields", [])),
             "columnas": len(p.get("detail", {}).get("headerAnchors", []))} for p in repo().todas()]
