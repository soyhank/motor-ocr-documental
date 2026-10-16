"""Salida limpia para integrar con otros sistemas y revalidación tras una corrección manual.

La salida completa del motor trae trazabilidad (texto crudo, recuadro y página de cada dato,
puntajes, reglas ejecutadas). Para pasar la orden a otro sistema basta con:

    {
      "documento":  {id, archivo, estado, estadoTexto, plantilla, formatoNuevo, ...},
      "cabecera":   {numero_orden, fecha_emision, agencia, agencia_ruc, anunciante, ...},
      "detalle":    [{item, descripcion, horario, duracion, total_avisos, costo_unitario,
                      costo_total, bonificado, programacion: [{fecha, dia, avisos}]}],
      "totales":    {costo, descuento, recargo, neto, igv_pct, igv, total, moneda, total_avisos},
      "validacion": {ok, errores: [...], observaciones: [...]},
      "correcciones": [{campo, antes, despues}]      (solo si alguien corrigió algo)
    }
"""
from __future__ import annotations

import copy
from datetime import datetime

ESTADOS = {
    "COMPLETED": "Correcto",
    "COMPLETED_WITH_WARNINGS": "Con observaciones",
    "VALIDATION_FAILED": "No cuadra",
    "NEW_FORMAT_REVIEW": "Formato nuevo: revisar",
    "TEMPLATE_NOT_IDENTIFIED": "Formato nuevo",
    "ERROR": "Error de lectura",
}

_DETALLE = ("descripcion", "programa", "motivo", "seccion", "tema", "tipo_aviso", "horario", "duracion",
            "total_avisos", "costo_unitario", "costo_total", "bonificado", "es_paquete", "paquete")
_TOTALES_EN_CABECERA = ("moneda",)


def _v(campo):
    return campo.get("value") if isinstance(campo, dict) else campo


def _regla(r: dict) -> dict:
    out = {"regla": r.get("rule"), "mensaje": r.get("description")}
    if r.get("detail"):
        out["detalle"] = r["detail"]
    elif r.get("difference") is not None:
        out["detalle"] = f"{r.get('left')} frente a {r.get('right')} (diferencia {r['difference']})"
    if r.get("row"):
        out["fila"] = r["row"]
    return out


def salida_simple(res: dict) -> dict:
    """Resultado del motor -> JSON limpio (cabecera, detalle, totales y validación)."""
    datos = res.get("extractedData") or {}
    tpl = res.get("template") or {}
    rep = res.get("validationReport") or {}
    estado = res.get("status")
    error = res.get("error")
    doc = {
        "id": res.get("documentId"),
        "archivo": res.get("file"),
        "estado": estado,
        "estadoTexto": ESTADOS.get(estado, estado),
        "plantilla": tpl.get("templateId"),
        "versionPlantilla": tpl.get("version"),
        "formatoNuevo": bool(res.get("newFormat")),
        "plantillaAprendida": bool(tpl.get("learned")),
        "origen": "escaneado" if any(p.get("source") == "ocr" for p in res.get("pages") or []) else "digital",
        "paginas": len(res.get("pages") or []),
        "sha256": res.get("sha256"),
        "procesadoEn": datetime.now().isoformat(timespec="seconds"),
    }
    if error:
        doc["error"] = error if isinstance(error, dict) else {"code": "LECTURA", "message": str(error)}
    cabecera = {k: _v(v) for k, v in (datos.get("header") or {}).items()}
    totales = {k: _v(v) for k, v in (datos.get("totals") or {}).items()}
    for k in _TOTALES_EN_CABECERA:
        if k not in totales and k in cabecera:
            totales[k] = cabecera[k]
    detalle = []
    for i, f in enumerate(datos.get("detail") or [], 1):
        fila = {"item": i}
        for k in _DETALLE:
            if k in f and _v(f[k]) not in (None, ""):
                fila[k] = _v(f[k])
        extra = f.get("descripcion_extra")
        if extra:
            fila["descripcion_extra"] = extra
        fila["programacion"] = [{"fecha": p.get("fecha"), "dia": p.get("dia"), "avisos": p.get("avisos")}
                                for p in f.get("programacion") or []]
        detalle.append(fila)
    fallidas = [r for r in rep.get("executedRules") or [] if r.get("passed") is False]
    validacion = {
        "ok": bool(rep.get("isValid")) if rep else False,
        "errores": [_regla(r) for r in fallidas if r.get("severity") == "ERROR"],
        "observaciones": [_regla(r) for r in fallidas if r.get("severity") != "ERROR"],
    }
    out = {"documento": doc, "cabecera": cabecera, "detalle": detalle, "totales": totales, "validacion": validacion}
    if res.get("corrections"):
        out["correcciones"] = res["corrections"]
    return out


def datos_desde_simple(simple: dict) -> dict:
    """JSON limpio (quizá corregido a mano) -> estructura interna para volver a validar."""
    def campos(d):
        return {k: {"value": v} for k, v in (d or {}).items()}

    filas = []
    for f in simple.get("detalle") or []:
        fila = {k: {"value": v} for k, v in f.items() if k not in ("item", "programacion")}
        fila["programacion"] = [dict(p) for p in f.get("programacion") or [] if p.get("avisos")]
        filas.append(fila)
    return {"header": campos(simple.get("cabecera")), "detail": filas, "totals": campos(simple.get("totales")),
            "footer": {}}


def _plantilla_de(template_id: str | None):
    from .generica import ID_GENERICA, plantilla_generica
    from .pipeline import repo

    if template_id and template_id != ID_GENERICA:
        p = repo().obtener(template_id)
        if p:
            return p
    return plantilla_generica()


def revalidar_datos(datos: dict, template_id: str | None) -> dict:
    """Vuelve a correr las validaciones sobre datos internos (corregidos). Devuelve el reporte."""
    from .pipeline import _calendario
    from .validar import cargar_config, validar

    plantilla = _plantilla_de(template_id)
    datos = copy.deepcopy(datos)
    cal = _calendario(datos.get("header") or {}, plantilla)
    datos["_calendario"] = cal
    # la fecha de cada día de la pauta se recalcula por si se corrigió el mes o el periodo
    if cal:
        from datetime import date
        for f in datos.get("detail") or []:
            for p in f.get("programacion") or []:
                try:
                    p["fecha"] = date(cal[0], cal[1], int(p["dia"])).isoformat()
                except (ValueError, TypeError, KeyError):
                    pass
    return validar(datos, plantilla, cargar_config()), datos


def revalidar_simple(simple: dict) -> dict:
    """API: recibe el JSON limpio corregido y lo devuelve con la validación actualizada."""
    from .pipeline import _estado

    datos = datos_desde_simple(simple)
    rep, datos = revalidar_datos(datos, (simple.get("documento") or {}).get("plantilla"))
    res = {"documentId": (simple.get("documento") or {}).get("id"), "file": (simple.get("documento") or {}).get("archivo"),
           "status": _estado(rep), "template": {"templateId": (simple.get("documento") or {}).get("plantilla")},
           "extractedData": datos, "validationReport": rep, "corrections": simple.get("correcciones")}
    out = salida_simple(res)
    out["documento"] = {**(simple.get("documento") or {}), "estado": res["status"],
                        "estadoTexto": ESTADOS.get(res["status"]), "revalidadoEn": datetime.now().isoformat(timespec="seconds")}
    return out
