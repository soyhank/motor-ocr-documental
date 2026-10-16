"""Orquestador del pipeline determinista (sin llamadas a LLM en producción)."""
from __future__ import annotations

import json
import re
import time
from datetime import date, datetime
from pathlib import Path

from rapidfuzz import fuzz

from .extraer import extraer_campos, extraer_tabla
from .ingesta import EXT_IMAGEN, Documento, agrupar_lineas, ingerir
from .plantillas import RAIZ, Repositorio, identificar
from .validar import cargar_config, validar

EXT_AUDIO = {".mp3", ".wav", ".m4a", ".ogg", ".aac", ".flac", ".wma"}
DIR_COLA = RAIZ / "cola_aprendizaje"

_REPO: Repositorio | None = None


def repo() -> Repositorio:
    global _REPO
    if _REPO is None:
        _REPO = Repositorio()
    return _REPO


def _id_documento(sha: str) -> str:
    return f"DOC-{datetime.now():%Y%m%d}-{sha[:8].upper()}"


def _expandir_periodos(campos: dict, plantilla_campos: list[dict]):
    """Un campo de tipo 'period' se convierte en <nombre>_inicio y <nombre>_fin."""
    for c in plantilla_campos:
        if c.get("type") != "period" or c["name"] not in campos:
            continue
        v = campos.pop(c["name"])
        base = c.get("splitAs", c["name"])
        per = v["value"] or {}
        for parte, clave in (("inicio", "inicio"), ("fin", "fin")):
            campos[f"{base}_{parte}"] = {"value": per.get(clave), "tag": f"{v['tag']}_{parte.upper()}",
                                         "source": v.get("source")}


def _calendario(header: dict, plantilla: dict) -> tuple[int, int] | None:
    for clave in plantilla.get("detail", {}).get("calendarFrom", ["mes", "periodo_inicio", "fecha_emision"]):
        v = (header.get(clave) or {}).get("value")
        if not v:
            continue
        m = re.match(r"(\d{4})-(\d{2})", v)
        if m:
            return int(m.group(1)), int(m.group(2))
    return None


def _programacion(filas: list[dict], cal: tuple[int, int] | None):
    for f in filas:
        prog = []
        for dia, d in sorted(f.get("_dias", {}).items()):
            fecha = None
            if cal:
                try:
                    fecha = date(cal[0], cal[1], dia).isoformat()
                except ValueError:
                    fecha = None
            prog.append({"dia": dia, "fecha": fecha, "letra": d.get("letra"), "avisos": d["avisos"],
                         "source": d.get("source")})
        f["programacion"] = prog


def _limpiar(obj):
    if isinstance(obj, dict):
        return {k: _limpiar(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [_limpiar(v) for v in obj]
    return obj


def _resumen(datos: dict) -> dict:
    plano = {}
    for sec in ("header", "totals"):
        for k, v in (datos.get(sec) or {}).items():
            if isinstance(v, dict) and "value" in v:
                plano[k] = v["value"]
    plano["filas"] = len(datos.get("detail", []))
    plano["avisos"] = sum((f.get("total_avisos") or {}).get("value") or 0 for f in datos.get("detail", []))
    return plano


def extraer_con_plantilla(doc: Documento, plantilla: dict) -> dict:
    opciones = plantilla.get("options", {})
    lineas = [l for p in doc.paginas for l in agrupar_lineas(p.tokens)]
    campos_h = plantilla.get("header", {}).get("fields", [])
    header = extraer_campos(campos_h, lineas, opciones)
    _expandir_periodos(header, campos_h)

    tabla = {"rows": [], "footer": None, "columns": None, "limite": None}
    if plantilla.get("detail", {}).get("isTable"):
        tabla = extraer_tabla(doc, plantilla["detail"], opciones)

    cfg_t = plantilla.get("totals", {})
    limite = None
    if cfg_t.get("searchBelowTable", True) and tabla.get("limite"):
        y, pag = tabla["limite"]
        limite = (y, 1001, pag)
    totals = extraer_campos(cfg_t.get("fields", []), lineas, opciones, limite)
    pie = tabla.get("footer") or {}
    for destino, origen in (cfg_t.get("fromFooter") or {}).items():
        if destino not in totals and pie.get(origen):
            totals[destino] = {**pie[origen], "tag": f"TOT_{destino.upper()}"}

    cal = _calendario(header, plantilla)
    _programacion(tabla["rows"], cal)
    datos = {"header": header, "detail": tabla["rows"], "totals": totals, "footer": pie,
             "_columnas": tabla.get("columns"), "_calendario": cal,
             "_ocr": any(p.origen == "ocr" for p in doc.paginas)}
    return datos


def procesar(ruta: str | Path, datos_bytes: bytes | None = None, con_imagen: bool = False,
             forzar_plantilla: dict | None = None) -> dict:
    t0 = time.perf_counter()
    ruta = Path(ruta)
    try:
        doc = ingerir(ruta, datos_bytes, con_imagen=con_imagen)
    except Exception as e:  # noqa: BLE001
        return {"documentId": None, "file": ruta.name, "status": "ERROR", "error": str(e)}
    res = {
        "documentId": _id_documento(doc.sha256),
        "file": doc.nombre,
        "sha256": doc.sha256,
        "pages": [{"page": p.numero, "source": p.origen, "tokens": len(p.tokens)} for p in doc.paginas],
    }
    if forzar_plantilla:
        plantilla = forzar_plantilla
        ident = {"winner": {"templateId": plantilla["templateId"], "version": plantilla.get("version", 1),
                            "score": None}, "candidates": []}
    else:
        ident = identificar(doc, repo())
        plantilla = repo().obtener(ident["winner"]["templateId"]) if ident["winner"] else None
    res["template"] = {
        "templateId": ident["winner"]["templateId"] if ident["winner"] else None,
        "version": ident["winner"]["version"] if ident["winner"] else None,
        "score": ident["winner"]["score"] if ident["winner"] else None,
        "candidates": [{k: c[k] for k in ("templateId", "score", "threshold")} for c in ident["candidates"][:5]],
    }
    if plantilla is None:
        res["status"] = "TEMPLATE_NOT_IDENTIFIED"
        res["learningQueue"] = encolar_aprendizaje(doc)
        res["metrics"] = {"ms": round((time.perf_counter() - t0) * 1000)}
        res["_doc"] = doc
        return res

    datos = extraer_con_plantilla(doc, plantilla)
    reporte = validar(datos, plantilla, cargar_config())
    res["documentType"] = plantilla.get("documentType")
    res["extractedData"] = _limpiar({k: v for k, v in datos.items() if not k.startswith("_")})
    res["calendar"] = {"year": datos["_calendario"][0], "month": datos["_calendario"][1]} if datos["_calendario"] else None
    res["summary"] = _resumen(res["extractedData"])
    res["validationReport"] = reporte
    res["status"] = _estado(reporte)
    res["metrics"] = {"ms": round((time.perf_counter() - t0) * 1000)}
    res["_doc"] = doc
    return res


def _estado(reporte: dict) -> str:
    if reporte["errors"]:
        return "VALIDATION_FAILED"
    if reporte["warnings"]:
        return "COMPLETED_WITH_WARNINGS"
    return "COMPLETED"


def encolar_aprendizaje(doc: Documento) -> str:
    """Guarda el volcado espacial del documento para calibrar una plantilla nueva."""
    from .calibrar import volcado, borrador_plantilla

    DIR_COLA.mkdir(exist_ok=True)
    base = DIR_COLA / f"{datetime.now():%Y%m%d-%H%M%S}_{doc.sha256[:8]}"
    base.with_suffix(".tokens.json").write_text(json.dumps(volcado(doc), ensure_ascii=False, indent=1), encoding="utf-8")
    base.with_suffix(".borrador.json").write_text(
        json.dumps(borrador_plantilla(doc), ensure_ascii=False, indent=2), encoding="utf-8")
    return str(base.relative_to(RAIZ))


# ---------------------------------------------------------------- lote

def duracion_audio(ruta: Path) -> float | None:
    try:
        from mutagen import File
        f = File(str(ruta))
        return round(float(f.info.length), 2) if f and f.info else None
    except Exception:  # noqa: BLE001
        return None


def cruzar_materiales(resultados: list[dict], audios: list[Path]):
    """Compara la duración de cada audio con las filas cuyo motivo se parece al nombre del archivo."""
    for a in audios:
        dur = duracion_audio(a)
        nombre = re.sub(r"[_\-]+", " ", a.stem).upper()
        for r in resultados:
            filas = (r.get("extractedData") or {}).get("detail", [])
            for i, f in enumerate(filas):
                texto = " ".join(str((f.get(k) or {}).get("value") or "") for k in ("motivo", "descripcion")).upper()
                if not texto.strip() or fuzz.token_set_ratio(nombre, texto) < 85:
                    continue
                seg = (f.get("duracion") or {}).get("value")
                ok = dur is not None and seg is not None and abs(dur - seg) <= 1.0
                regla = {"rule": f"MATERIAL_DURACION({a.name})", "row": i + 1, "severity": "WARNING",
                         "status": "PASSED" if ok else "FAILED", "passed": ok,
                         "description": "La duración del audio coincide con la duración pautada",
                         "detail": f"audio {dur} s, orden {seg} s"}
                rep = r["validationReport"]
                rep["executedRules"].append(regla)
                if ok:
                    rep["passed"] += 1
                else:
                    rep["warnings"] += 1
                    r["status"] = _estado(rep)
                r.setdefault("materials", []).append({"file": a.name, "seconds": dur, "row": i + 1, "match": ok})


def cruzar_duplicados(resultados: list[dict]):
    por_hash: dict[str, list] = {}
    por_orden: dict[tuple, list] = {}
    for r in resultados:
        if r.get("sha256"):
            por_hash.setdefault(r["sha256"], []).append(r)
        s = r.get("summary") or {}
        if s.get("numero_orden"):
            clave = (s.get("agencia_ruc") or s.get("agencia") or "", str(s["numero_orden"]).lstrip("0"),
                     str(s.get("revision") or ""))
            por_orden.setdefault(clave, []).append(r)
    for grupo, motivo in [(g, "archivo idéntico") for g in por_hash.values()] + \
                         [(g, "mismo número de orden y revisión") for g in por_orden.values()]:
        if len(grupo) < 2:
            continue
        for r in grupo:
            otros = [o["file"] for o in grupo if o is not r]
            if "validationReport" not in r:
                continue
            rep = r["validationReport"]
            if any(x["rule"] == "DUPLICADO" and motivo in (x.get("detail") or "") for x in rep["executedRules"]):
                continue
            rep["executedRules"].append({"rule": "DUPLICADO", "severity": "WARNING", "status": "FAILED", "passed": False,
                                         "description": "La orden no está repetida en el lote",
                                         "detail": f"{motivo}: {', '.join(otros)}"})
            rep["warnings"] += 1
            r["status"] = _estado(rep)


def procesar_lote(entradas: list[Path], con_imagen: bool = False) -> list[dict]:
    docs = [p for p in entradas if p.suffix.lower() == ".pdf" or p.suffix.lower() in EXT_IMAGEN]
    audios = [p for p in entradas if p.suffix.lower() in EXT_AUDIO]
    resultados = [procesar(p, con_imagen=con_imagen) for p in docs]
    cruzar_duplicados(resultados)
    if audios:
        cruzar_materiales(resultados, audios)
    return resultados
