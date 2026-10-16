"""Subsistema de auto-calibración offline.

1. volcado(): grafo espacial crudo (tokens + renglones) del documento desconocido.
2. borrador_plantilla(): plantilla inicial generada por heurística (etiquetas "X:", cabecera
   de la tabla con columnas de días, pie de totales). No usa ningún LLM.
3. prompt_llm(): texto listo para que un modelo offline o un agente de soporte afine la
   plantilla. Es opcional y nunca se ejecuta en producción.
4. probar(): sandbox. Corre la plantilla sobre documentos de ejemplo y, si todas las
   validaciones aritméticas cuadran, la promueve al repositorio.
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

from .ingesta import Documento, agrupar_lineas
from .normalizar import DIAS_SEMANA, sin_tildes
from .plantillas import DIR_PLANTILLAS


def volcado(doc: Documento) -> dict:
    return {
        "documentId": doc.sha256[:12],
        "file": doc.nombre,
        "pages": [{
            "page": p.numero, "source": p.origen,
            "lines": [{"y": round(l.cy, 1), "text": l.texto,
                       "tokens": [t.to_dict() for t in l.tokens]} for l in agrupar_lineas(p.tokens)],
        } for p in doc.paginas],
    }


def _slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_") or "campo"


_TIPOS = [
    (r"ruc", "ruc"), (r"fecha|emision", "date"), (r"periodo|vigencia", "period"), (r"mes\b", "month"),
    (r"moneda", "currency"), (r"total|costo|neto|igv|importe|monto|precio|unit", "money"),
]


def _tipo(nombre: str) -> str:
    for rx, t in _TIPOS:
        if re.search(rx, nombre):
            return t
    return "text"


def borrador_plantilla(doc: Documento, template_id: str | None = None) -> dict:
    lineas = [l for p in doc.paginas for l in agrupar_lineas(p.tokens)]

    # cabecera de tabla: renglón con la secuencia de días más larga
    fila_dias, mejor = None, 0
    for l in lineas:
        nums = [t for t in l.tokens if re.fullmatch(r"\d{1,2}", t.text)]
        seq = 0
        for a, b in zip(nums, nums[1:]):
            if int(b.text) == int(a.text) + 1:
                seq += 1
        if seq > mejor:
            fila_dias, mejor = l, seq
    detalle = None
    y_tabla = None
    if fila_dias is not None and mejor >= 6:
        banda = [l for l in lineas if l.page == fila_dias.page and abs(l.cy - fila_dias.cy) <= 30]
        cabeceras = []
        for l in banda:
            for t in l.tokens:
                if re.fullmatch(r"\d{1,2}", t.text) or sin_tildes(t.text).upper() in DIAS_SEMANA:
                    continue
                cabeceras.append(t)
        cabeceras.sort(key=lambda t: t.x0)
        cols, previo = [], None
        for t in cabeceras:  # une palabras contiguas ("C." + "Total")
            if previo and t.x0 - previo["x1"] < 8 and abs(t.cy - previo["cy"]) < 6:
                previo["text"] += " " + t.text
                previo["x1"] = t.x1
            else:
                previo = {"text": t.text, "x1": t.x1, "cy": t.cy}
                cols.append(previo)
        y_tabla = fila_dias.cy
        stop = next((l for l in lineas if l.page == fila_dias.page and l.cy > fila_dias.cy + 20
                     and re.match(r"(?i)total", sin_tildes(l.texto))), None)
        detalle = {
            "isTable": True,
            "headerAnchors": [{"name": _slug(c["text"]), "pattern": re.escape(c["text"]),
                               "type": _tipo(_slug(c["text"]))} for c in cols],
            "dayColumns": True,
            "rowMode": "line",
            "stopAnchor": re.escape(stop.tokens[0].text) if stop else "TOTAL",
        }

    # etiquetas "Algo:" fuera de la tabla
    campos, vistos = [], set()
    for l in lineas:
        for i, t in enumerate(l.tokens):
            if not t.text.endswith(":") or len(t.text) < 3:
                continue
            j = i
            while j > 0 and not l.tokens[j - 1].text.endswith(":") and t.x0 - l.tokens[j - 1].x1 < 6 and i - j < 3:
                j -= 1
            etiqueta = " ".join(x.text for x in l.tokens[j:i + 1])
            nombre = _slug(etiqueta)
            if nombre in vistos:
                continue
            vistos.add(nombre)
            campos.append({"name": nombre, "anchor": {"pattern": re.escape(etiqueta), "searchDirection": "RIGHT",
                                                      "maxDistance": 300}, "type": _tipo(nombre),
                           "_y": l.cy})
    header = [c for c in campos if y_tabla is None or c["_y"] < y_tabla]
    totales = [c for c in campos if y_tabla is not None and c["_y"] > y_tabla]
    for c in campos:
        c.pop("_y")

    # identificación: frases del encabezado del documento
    arriba = [l.texto for l in lineas if l.page == 1 and l.cy < 200 and len(l.texto) > 8][:4]
    reglas = [{"type": "exact_text", "value": s[:60], "weight": round(100 / max(len(arriba), 1))} for s in arriba]

    return {
        "templateId": template_id or f"BORRADOR_{doc.sha256[:8].upper()}",
        "version": 1,
        "status": "draft",
        "documentType": "ORDEN_PUBLICIDAD",
        "description": f"Borrador automático a partir de {doc.nombre}",
        "identification": {"minScoreThreshold": 80, "rules": reglas},
        "options": {"decimalSeparator": "auto"},
        "header": {"fields": header},
        "detail": detalle or {"isTable": False},
        "totals": {"searchBelowTable": True, "fields": totales},
        "validations": [],
    }


def prompt_llm(doc: Documento, borrador: dict, ejemplo: dict | None = None) -> str:
    v = volcado(doc)
    renglones = "\n".join(
        f"p{pg['page']} y={l['y']:6.1f} | " + "  ".join(f"{t['text']}@{t['bbox']['x0']:.0f}-{t['bbox']['x1']:.0f}"
                                                    for t in l["tokens"])
        for pg in v["pages"] for l in pg["lines"])
    return f"""Eres un calibrador de plantillas del Motor OCR Documental. Debes devolver SOLO un JSON
de plantilla válido (mismo esquema que el ejemplo) para el documento cuyo volcado espacial
aparece abajo. Coordenadas normalizadas 0..1000 (x0-x1 de cada token, y del renglón).

Campos canónicos de una orden de publicidad (usa estos nombres cuando existan):
numero_orden, revision, fecha_emision, agencia, agencia_ruc, anunciante, anunciante_ruc,
cliente_titular, proveedor, proveedor_ruc, medio, producto, campana, motivo, periodo (type period),
campana_fechas (type period, splitAs campana), mes (type month), plan, presupuesto, pauta,
contrato, ciudad, condicion_pago, moneda, emitido_por.
Columnas: descripcion, horario, duracion, total_avisos, costo_unitario, costo_total.
Totales: costo, recargo, descuento, neto, igv_pct, igv, total, total_avisos.

Reglas: no inventes campos que no estén en el documento; usa valuePattern para recortar;
el sandbox probará la plantilla y solo la promoverá si los totales cuadran.

BORRADOR HEURÍSTICO:
{json.dumps(borrador, ensure_ascii=False, indent=1)}

PLANTILLA DE EJEMPLO YA VALIDADA:
{json.dumps(ejemplo, ensure_ascii=False, indent=1) if ejemplo else "(ninguna)"}

VOLCADO ESPACIAL:
{renglones}
"""


def probar(plantilla: dict, rutas: list[Path], promover: bool = False) -> dict:
    """Sandbox: aplica la plantilla a los documentos y decide si se promueve."""
    from .pipeline import procesar

    resultados = []
    for r in rutas:
        res = procesar(r, forzar_plantilla=plantilla)
        res.pop("_doc", None)
        resultados.append(res)
    aprobada = all(r.get("validationReport", {}).get("isValid") for r in resultados) and all(
        r.get("summary", {}).get("filas", 0) > 0 for r in resultados)
    destino = None
    if aprobada and promover:
        plantilla = dict(plantilla)
        plantilla["status"] = "active"
        plantilla.pop("_archivo", None)
        existentes = [json.loads(p.read_text(encoding="utf-8")) for p in DIR_PLANTILLAS.glob("*.json")]
        versiones = [e.get("version", 1) for e in existentes if e.get("templateId") == plantilla["templateId"]]
        if versiones:
            plantilla["version"] = max(versiones) + 1
        destino = DIR_PLANTILLAS / f"{plantilla['templateId']}.v{plantilla['version']}.json"
        destino.write_text(json.dumps(plantilla, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"approved": aprobada, "promotedTo": str(destino) if destino else None,
            "results": [{"file": r["file"], "status": r["status"],
                         "errors": r.get("validationReport", {}).get("errors"),
                         "failed": [x["rule"] for x in r.get("validationReport", {}).get("executedRules", [])
                                    if x["passed"] is False]} for r in resultados]}
