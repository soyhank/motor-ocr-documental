"""Formatos nuevos: lectura genérica y aprendizaje automático de plantillas.

Cuando ningún formato conocido llega al umbral, el documento no se descarta:

1. Se lee con una plantilla genérica de sinónimos (``ORDEN N°``, ``Nro. de orden``,
   ``Fecha de emisión``, ``C. Unitario``, ``Tarifa``...). Para la tabla se prueban varias
   formas de leer las filas y para los totales todas las combinaciones de candidatos; gana
   la que hace cuadrar la aritmética (filas = costo, neto + IGV = total).
2. Los roles se resuelven con datos conocidos: el RUC y los nombres de Grupo RPP marcan al
   proveedor, el catálogo de agencias a la agencia, y el resto de RUC se asigna al nombre
   más cercano.
3. Si las validaciones aritméticas cuadran, se genera una plantilla propia para esa agencia
   (con las etiquetas exactas que usa y su RUC para reconocerla) y se guarda en
   ``plantillas/``. La próxima orden de esa agencia ya se reconoce sola.
4. Si no cuadran, el resultado queda como ``NEW_FORMAT_REVIEW``: el asesor corrige en
   pantalla y, al guardar la corrección, la plantilla se aprende de lo corregido.
"""
from __future__ import annotations

import copy
import itertools
import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path

from .catalogo import _catalogo, _clave
from .extraer import Linea, Token, buscar_anclas, extraer_campos, extraer_tabla, _valor_derecha
from .ingesta import Documento, agrupar_lineas
from .normalizar import normalizar, ruc_valido, sin_tildes
from .plantillas import DIR_PLANTILLAS

ID_GENERICA = "ORDEN_GENERICA"

# ------------------------------------------------------------------ sinónimos

_DP = r"\s*[:.]?"  # dos puntos (o punto) opcionales al final de la etiqueta


def _campo(nombre, patron, tipo="text", **extra):
    c = {"name": nombre, "tag": nombre.upper(), "type": tipo,
         "anchor": {"pattern": patron, "searchDirection": extra.pop("direction", "RIGHT"),
                    "maxDistance": extra.pop("maxDistance", 600)}}
    c.update(extra)
    return c


CAMPOS_CABECERA = [
    _campo("numero_orden", r"(ORDEN DE PUBLICIDAD|ORDEN DE MEDIOS|ORDEN DE COMPRA|ORDEN DE SERVICIO|ORDEN|O/C|N[°ºo]? DE ORDEN|NRO\.? DE ORDEN)\s*(N[°ºo]|NRO|NUM(ERO)?|#)?\.?\s*:?",
           valuePattern=r"([A-Z]{0,4}-?\d[\d-]*)", maxDistance=200),
    _campo("revision", r"(RV|REV|REVISION|VERSION)\s*[.:]?", maxDistance=60, valuePattern=r"^(\d+)"),
    _campo("fecha_emision", r"(FECHA DE EMISION|FECHA EMISION|FECHA DE ORDEN|FECHA ORDEN|FECHA)\s*:?", "date", maxDistance=220),
    _campo("periodo", r"(PERIODO|VIGENCIA|PERIODO DE (EMISION|TRANSMISION|PAUTA|CAMPA[ÑN]A)|FECHAS? DE (EMISION|TRANSMISION|PAUTA))\s*:?", "period"),
    _campo("campana_fechas", r"FECHAS? DE CAMPA[ÑN]I?A\s*:?", "period", splitAs="campana"),
    _campo("mes", r"(MES|MES DE (EMISION|PAUTA))\s*:?", "month", maxDistance=200),
    _campo("anunciante", r"(ANUNCIANTE|CLIENTE TITULAR|CLIENTE FINAL|ANUNCIADOR|MARCA|CLIENTE)\s*:?"),
    _campo("agencia", r"(AGENCIA|AGENCIA DE MEDIOS|CENTRAL DE MEDIOS)\s*:"),
    _campo("proveedor", r"(PROVEEDOR|SRES?|SE[ÑN]ORES|AT|ATENCION|A LA ATENCION DE|DIRIGIDO A|PARA)\s*[.:]"),
    _campo("medio", r"(MEDIO|EMISORA|RADIO|CANAL|ESTACION|VEHICULO)\s*:"),
    _campo("producto", r"PRODUCTO\s*:?"),
    _campo("campana", r"(CAMPA[ÑN]A|CAMPAIGN|CAMPA[ÑN]A PUBLICITARIA)\s*:"),
    _campo("motivo", r"(MOTIVO|VERSION|MATERIAL|SPOT|PIEZA)\s*:"),
    _campo("ciudad", r"(CIUDAD|ZONA|PLAZA|NIVEL|COBERTURA|AMBITO)\s*:"),
    _campo("condicion_pago", r"(CONDICION(ES)? DE PAGO( PROVEEDOR)?|CONDICION PAGO( PROVEEDOR)?|FORMA DE PAGO|PLAZO DE PAGO|CREDITO)\s*:"),
    _campo("moneda", r"(MONEDA|DIVISA)\s*:", "currency"),
    _campo("plan", r"PLAN\s*(N[°ºo]|NRO)?\.?\s*:", maxDistance=150, valuePattern=r"(\w[\w/-]*)"),
    _campo("presupuesto", r"PRESUPUESTO\s*(N[°ºo]|NRO)?\.?\s*:", maxDistance=150, valuePattern=r"(\w[\w/-]*)"),
    _campo("pauta", r"PAUTA\s*(N[°ºo]|NRO)?\.?\s*:", maxDistance=150, valuePattern=r"(\w[\w/-]*)"),
    _campo("contrato", r"CONTRATO\s*(N[°ºo]|NRO)?\.?\s*:"),
    _campo("referencia", r"(REFERENCIA|ATENCION A)\s*:"),
    _campo("emitido_por", r"(EMITIDA? POR|ELABORADO POR|PREPARADO POR|SOLICITADO POR|EJECUTIVO|PLANNER)\s*:"),
]

CAMPOS_TOTALES = {  # candidatos de cada total; se elige la combinación que cuadra
    "costo": r"(COSTO|SUBTOTAL|SUB TOTAL|VALOR BRUTO|TOTAL BRUTO|IMPORTE BRUTO)\s*:?",
    "descuento": r"(DESCUENTO|DSCTO|DCTO)\.?(\s*\d+([.,]\d+)?\s*%)?\s*:?",
    "recargo": r"RECARGO(\s*\d+([.,]\d+)?\s*%)?\s*:?",
    "neto": r"(TOTAL NETO|COSTO NETO|NETO|VALOR VENTA|VALOR NETO|SUBTOTAL NETO|BASE IMPONIBLE)\s*:?",
    "igv": r"(COSTO IGV|IGV|I\.G\.V\.?)(\s*\(?\d+([.,]\d+)?\s*%\)?)?(\s*APLICADO)?\s*:?",
    "total": r"(TOTAL ORDEN|COSTO TOTAL|TOTAL A PAGAR|IMPORTE TOTAL|TOTAL GENERAL|TOTAL FACTURA|TOTAL)\s*:?",
    "total_avisos": r"(TOTAL AVISOS|TOTAL SPOTS|TOTAL INSERCIONES|TOTAL DE AVISOS)\s*:?",
}

COLUMNAS = [  # el orden importa: las más específicas primero
    {"name": "descripcion", "pattern": r"(DESCRIPCION|PROGRAMA / MEDIO|PROGRAMA|ESPACIO|DETALLE|CONCEPTO|PROGRAMACION)", "type": "text"},
    {"name": "horario", "pattern": r"(HORARIO|HORA|FRANJA|ROTACION)\s*:?", "type": "schedule", "required": False},
    {"name": "duracion", "pattern": r"(DURACION|DUR\.?|SEG(UNDOS)?\.?|SEGS)", "type": "duration", "required": False},
    {"name": "costo_unitario", "pattern": r"(C\.?\s*UNITARIO|COSTO NETO X|COSTO X AVISO|COSTO UNIT(ARIO)?|COSTO AV(ISO)?|TARIFA( NETA)?|PRECIO( UNIT(ARIO)?)?|C/U|P\.?\s*UNIT\.?)", "type": "money", "required": False},
    {"name": "costo_total", "pattern": r"(C\.?\s*TOTAL|COSTO TOTAL( NETO)?|IMPORTE( TOTAL)?|VALOR TOTAL|COSTO\s+S/|SUB ?TOTAL|INVERSION)", "type": "money", "required": False},
    {"name": "total_avisos", "pattern": r"(TOTAL AV(ISOS)?|N[°ºO]?\s*(DE\s*)?SPOTS|N[°ºO]?\s*(DE\s*)?AVISOS|CANT(IDAD)?\.?|TOTAL|AVISOS|SPOTS|INSERCIONES|TOT\.?)", "type": "int", "required": False},
]

PARADA = r"(TOTALES|TOTAL\b|SUB ?TOTAL|TIPO DE CAMBIO|COSTO\b|IMPORTE TOTAL|VALOR VENTA|BASE IMPONIBLE|\bNETO\b|\bI\.?G\.?V\b)"


def plantilla_generica() -> dict:
    return {
        "templateId": ID_GENERICA,
        "version": 1,
        "status": "active",
        "documentType": "ORDEN_PUBLICIDAD",
        "description": "Lectura genérica por sinónimos para formatos todavía no registrados",
        "identification": {"minScoreThreshold": 0, "rules": []},
        "options": {"decimalSeparator": "auto"},
        "header": {"fields": copy.deepcopy(CAMPOS_CABECERA)},
        "detail": {
            "isTable": True, "headerAnchors": copy.deepcopy(COLUMNAS), "dayColumns": True,
            "rowMode": "anchor", "anchorColumn": "total_avisos", "stopAnchor": PARADA, "stopAnywhere": True,
            "descriptionFields": {
                "motivo": r"MOTIVO:\s*(.+)$",
                "horario": {"pattern": r"\(([0-9]{1,2}[:.][0-9]{2}\s*-\s*[0-9]{1,2}[:.][0-9]{2})\)", "type": "schedule"},
                "duracion": {"pattern": r"(\d+)\s*(?:\"|''|seg)", "type": "duration"},
            },
            "bonusPattern": "BONIF",
            "calendarFrom": ["mes", "periodo_inicio", "campana_inicio", "fecha_emision"],
        },
        "totals": {"searchBelowTable": True, "fields": []},
        "validations": [],
    }


# ------------------------------------------------------------------ tabla

def _linea_cabecera(doc: Documento) -> tuple[int, float] | None:
    """Renglón con más nombres de columna conocidos (y, de preferencia, números de día)."""
    rxs = [re.compile(c["pattern"], re.IGNORECASE) for c in COLUMNAS]
    mejor, puntos = None, 0
    for p in doc.paginas:
        lineas = agrupar_lineas(p.tokens)
        for i, l in enumerate(lineas):
            vecinas = [x for x in lineas[max(i - 2, 0):i + 3] if abs(x.cy - l.cy) <= 22]
            texto = sin_tildes(" ".join(x.texto for x in vecinas)).upper()
            n = sum(1 for rx in rxs if rx.search(texto))
            dias = sum(1 for x in vecinas for t in x.tokens if re.fullmatch(r"\d{1,2}", t.text) and 1 <= int(t.text) <= 31)
            pts = n * 10 + min(dias, 31)
            if n >= 2 and rxs[0].search(sin_tildes(l.texto).upper()) and pts > puntos:
                mejor, puntos = (p.numero, l.cy), pts
    return mejor


def _variantes_detalle(base: dict, cabecera: tuple[int, float] | None) -> list[dict]:
    out = []
    for modo in ("anchor", "line"):
        for ancla in (("total_avisos", "costo_total") if modo == "anchor" else (None,)):
            d = copy.deepcopy(base)
            d["rowMode"] = modo
            if ancla:
                d["anchorColumn"] = ancla
            if cabecera:
                pag, y = cabecera
                d["headerAnchors"][0]["region"] = {"page": pag, "y0": y - 4, "y1": y + 4}
            out.append(d)
    return out


# ------------------------------------------------------------------ totales

def _candidatos(lineas: list[Linea], patron: str, desde_y: tuple | None) -> list[tuple[float, Token, str]]:
    """Todos los importes a la derecha de cada aparición de la etiqueta (debajo de la tabla)."""
    out = []
    region = None
    if desde_y:
        region = {"page": desde_y[1], "y0": desde_y[0], "y1": 1001}
    for a in buscar_anclas(lineas, patron, False, region):
        toks = _valor_derecha(a, {"maxDistance": 420, "stopPattern": None})
        texto = " ".join(t.text for t in toks)
        for m in re.finditer(r"-?\d[\d.,]*", texto):
            v = normalizar(m.group(0), "money", {})
            if v is not None:
                out.append((v, a.tokens[0], " ".join(t.text for t in a.tokens)))
        pct = re.search(r"(\d+(?:[.,]\d+)?)\s*%", " ".join(t.text for t in a.tokens) + " " + texto)
        if pct:
            out.append((-float(pct.group(1).replace(",", ".")) / 100, a.tokens[0], "%"))
    return out


def _elegir_totales(lineas: list[Linea], filas: list[dict], limite: tuple | None) -> dict:
    """Prueba combinaciones de candidatos y se queda con la que más relaciones cumple."""
    cand = {k: _candidatos(lineas, p, limite) for k, p in CAMPOS_TOTALES.items()}
    suma = None
    vals = [(f.get("costo_total") or {}).get("value") for f in filas]
    if vals and all(v is not None for v in vals):
        suma = round(sum(vals), 2)

    def opciones(k, incluir_nada=True):
        xs = sorted({round(v, 2) for v, _, et in cand[k] if v >= 0 and et != "%"})
        return ([None] if incluir_nada else []) + xs[:12]

    pct = next((-v for v, _, et in cand["igv"] if v < 0), None)
    tasa = pct if pct and 0.05 < pct < 0.3 else 0.18
    netos = set(opciones("neto", False)) | set(opciones("costo", False)) | ({suma} if suma is not None else set())
    mejor, puntos = None, -1
    for neto in sorted(netos):
        for igv in opciones("igv"):
            for total in opciones("total"):
                p = 0
                if igv is not None and abs(neto * tasa - igv) <= 0.05:
                    p += 3
                if igv is not None and total is not None and abs(neto + igv - total) <= 0.05:
                    p += 3
                if suma is not None and abs(suma - neto) <= 0.05:
                    p += 2
                if neto in opciones("neto", False):
                    p += 1
                if p > puntos:
                    mejor, puntos = (neto, igv, total), p
    res = {}
    if mejor:
        neto, igv, total = mejor
        res["neto"] = neto
        if igv is not None:
            res["igv"], res["igv_pct"] = igv, tasa
        if total is not None:
            res["total"] = total
        costo = [v for v in opciones("costo", False) if v >= neto - 0.05]
        if costo:
            res["costo"] = min(costo)
        bruto = res.get("costo", suma)
        if bruto is not None:
            dif = round(bruto - neto, 2)
            desc = [v for v in opciones("descuento", False) if abs(v - max(dif, 0)) <= 0.05]
            res["descuento"] = desc[0] if desc else max(dif, 0.0)
            if dif < -0.05:
                rec = [v for v in opciones("recargo", False) if abs(v + dif) <= 0.05]
                res["recargo"] = rec[0] if rec else -dif
    av = [v for v in opciones("total_avisos", False) if float(v).is_integer()]
    if av:
        suma_av = sum((f.get("total_avisos") or {}).get("value") or 0 for f in filas)
        res["total_avisos"] = int(min(av, key=lambda v: abs(v - suma_av)))
    etiquetas = {}
    for k in res:
        base = "igv" if k == "igv_pct" else k
        for v, tok, et in cand.get(base, []):
            if et != "%" and round(v, 2) == res[k]:
                etiquetas[k] = (tok, et)
                break
    out = {}
    for k, v in res.items():
        src = None
        if k in etiquetas:
            tok = etiquetas[k][0]
            src = {"page": tok.page, "rawText": str(v), "bbox": {"x0": round(tok.x0, 1), "y0": round(tok.y0, 1),
                                                                    "x1": round(tok.x1, 1), "y1": round(tok.y1, 1)}}
        out[k] = {"value": v, "tag": f"TOT_{k.upper()}", "source": src, "_ancla": etiquetas.get(k, (None, None))[1]}
    return out


# ------------------------------------------------------------------ roles (agencia / anunciante / RPP)

def _rucs(doc: Documento) -> list[Token]:
    out = []
    for t in doc.tokens:
        for m in re.finditer(r"(?<!\d)(\d{11})(?!\d)", t.text):
            if ruc_valido(m.group(1)):
                out.append(Token(m.group(1), t.x0, t.y0, t.x1, t.y1, t.page, conf=t.conf))
    return out


def _buscar_catalogo(lineas: list[Linea], lista: str) -> tuple[str, Linea] | None:
    cat = _catalogo()
    formas = {}
    for campo, opciones in cat.items():
        for v, k in opciones:
            formas[k] = v
    datos_json = {}
    try:
        from .catalogo import RAIZ as R
        datos_json = json.loads((R / "catalogo.json").read_text(encoding="utf-8")).get("listas", {})
    except (OSError, ValueError):
        pass
    nombres = [(v, _clave(v)) for v in datos_json.get(lista, [])]
    for l in lineas:
        k = _clave(l.texto)
        for v, kv in sorted(nombres, key=lambda x: -len(x[1])):
            if len(kv) >= 5 and kv in k:
                return v, l
    return None


def resolver_roles(doc: Documento, header: dict, config: dict):
    lineas = [l for p in doc.paginas for l in agrupar_lineas(p.tokens)]
    rpp = config.get("empresaReceptora", {})
    rucs_rpp = set(rpp.get("rucs") or [rpp.get("ruc")])
    alias = [_clave(a) for a in (rpp.get("alias") or [])] + [_clave(rpp.get("nombre", "GRUPO RPP"))]
    rucs = _rucs(doc)

    def es_rpp(texto):
        k = _clave(texto or "")
        return any(a and a in k for a in alias)

    # proveedor = Grupo RPP
    if "proveedor_ruc" not in header and any(r.text in rucs_rpp for r in rucs):
        r = next(r for r in rucs if r.text in rucs_rpp)
        header["proveedor_ruc"] = {"value": r.text, "tag": "PROVEEDOR_RUC", "source": _src(r)}
    if not (header.get("proveedor") or {}).get("value") or not es_rpp(header["proveedor"]["value"]):
        for l in lineas:
            if es_rpp(l.texto):
                toks = [t for t in l.tokens if es_rpp(t.text) or _clave(t.text) in ("SAC", "SA", "SOCIEDAD", "ANONIMA", "CERRADA")]
                m = re.search(r"(GRUPO\s*RPP[\w .]*?(S\.?A\.?C\.?|SOCIEDAD AN[OÓ]NIMA CERRADA)?)(?=\s+R\.?U\.?C|\s*$|\s{2,})", l.texto, re.I)
                valor = m.group(1).strip() if m else " ".join(t.text for t in toks)
                header["proveedor"] = {"value": valor, "tag": "PROVEEDOR", "source": _src(l.tokens[0])}
                break

    # agencia: catálogo, etiqueta 'Agencia:' o 'Cliente:' cuyo valor es una agencia conocida
    ag = header.get("agencia")
    cli = header.get("anunciante")
    agencias = {k for v, k in (_catalogo().get("agencia") or [])}
    if cli and _clave(cli.get("value") or "") in agencias:
        header["agencia"], ag = cli, cli
        header.pop("anunciante")
        cli = None
        # el anunciante va con otra etiqueta ("Cliente Titular:", "Anunciante:")
        # el anunciante va con otra etiqueta ("Cliente Titular:", "Anunciante:") u otro "Cliente:"
        clave_ag = _clave(ag.get("value") or "")
        for a in buscar_anclas(lineas, r"(CLIENTE TITULAR|ANUNCIANTE|CLIENTE|MARCA)\s*:", False):
            toks = _valor_derecha(a, {"maxDistance": 320})
            texto = " ".join(t.text for t in toks).strip()
            if texto and _clave(texto) != clave_ag and not es_rpp(texto):
                header["anunciante"] = {"value": texto, "tag": "ANUNCIANTE", "source": _src(toks[0]),
                                        "_ancla": " ".join(t.text for t in a.tokens)}
                break
    if not ag or not ag.get("value"):
        hallado = _buscar_catalogo(lineas, "agencias")
        if hallado:
            v, l = hallado
            header["agencia"] = {"value": v, "tag": "AGENCIA", "source": _src(l.tokens[0])}
        else:
            m = _membrete(lineas, es_rpp)
            if m:
                header["agencia"] = {"value": " ".join(t.text for t in m), "tag": "AGENCIA", "source": _src(m[0])}

    # RUC de agencia y anunciante: el RUC válido (no de RPP) más cercano a cada nombre
    otros = [r for r in rucs if r.text not in rucs_rpp]
    for nombre, campo_ruc in (("agencia", "agencia_ruc"), ("anunciante", "anunciante_ruc")):
        if campo_ruc in header or not otros or not (header.get(nombre) or {}).get("source"):
            continue
        s = header[nombre]["source"]
        cy, x0 = (s["bbox"]["y0"] + s["bbox"]["y1"]) / 2, s["bbox"]["x0"]
        usados = {(header.get(c) or {}).get("value") for c in ("agencia_ruc", "anunciante_ruc")}
        libres = [r for r in otros if r.text not in usados and r.page == s["page"]]
        if not libres:
            continue
        r = min(libres, key=lambda r: abs(r.cy - cy) * 3 + abs(r.x0 - x0) * 0.3)
        if abs(r.cy - cy) <= 45:
            header[campo_ruc] = {"value": r.text, "tag": campo_ruc.upper(), "source": _src(r)}


_TITULOS = re.compile(r"(?i)\b(orden|p[aá]gina|hoja|fecha)\b|^lima\b")


def _grupos(l: Linea) -> list[list[Token]]:
    """Tramos de un renglón separados por huecos grandes (texto a la izquierda y a la derecha)."""
    out: list[list[Token]] = []
    for t in l.tokens:
        if out and t.x0 - out[-1][-1].x1 < max(t.h, out[-1][-1].h) * 2.5:
            out[-1].append(t)
        else:
            out.append([t])
    return out


def _membrete(lineas: list[Linea], es_rpp) -> list[Token] | None:
    """Razón social de la agencia: el texto de letra más grande de la parte de arriba de la
    primera página que no sea un título ('ORDEN DE PUBLICIDAD'), ni RPP, ni una dirección."""
    mejor, alto = None, 0.0
    for l in lineas:
        if l.page != 1 or l.cy > 180:
            continue
        for g in _grupos(l):
            texto = " ".join(t.text for t in g)
            if not re.search(r"[A-Za-z]{3}", texto) or ":" in texto or _TITULOS.search(texto) or es_rpp(texto) \
                    or re.match(r"(?i)(R\.?U\.?C|AV\.?|CALLE|JR\.?|TELF?|TEL[EÉ]FONO)\b", texto):
                continue
            h = sum(t.h for t in g) / len(g)
            if h > alto * 1.08 or (abs(h - alto) <= alto * 0.08 and mejor is not None and l.cy < mejor[0].cy):
                mejor, alto = g, h
    return mejor


def _recuadros(lineas: list[Linea], campos: list[dict], opciones: dict) -> dict:
    """Datos en recuadros con la etiqueta arriba del valor: un renglón con 3 o más etiquetas
    conocidas y, justo debajo, los valores alineados con cada una."""
    out: dict = {}
    for i, l in enumerate(lineas):
        hallados = []
        for c in campos:
            patron = re.sub(r"\\s\*:\??$|\\s\*\[:.\]\??$|\\s\*\[:.\]$|\\s\*:$", "", c["anchor"]["pattern"])
            for a in buscar_anclas([l], patron, False):
                hallados.append((a, c))
        rucs = [t for t in l.tokens if re.fullmatch(r"(?i)R\.?U\.?C\.?", t.text)]
        # una etiqueta por posición: la coincidencia más larga
        por_x: dict = {}
        for a, c in hallados:
            k = round(a.x0)
            if k not in por_x or len(a.tokens) > len(por_x[k][0].tokens):
                por_x[k] = (a, c)
        etiquetas = sorted(por_x.values(), key=lambda x: x[0].x0)
        if len(etiquetas) + len(rucs) < 3:
            continue
        cubiertos = sum(len(a.tokens) for a, _ in etiquetas) + len(rucs)
        if cubiertos < 0.7 * len(l.tokens):
            continue  # el renglón no es solo de etiquetas
        debajo = next((x for x in lineas[i + 1:i + 3] if x.page == l.page and 0 < x.cy - l.cy < 30), None)
        if debajo is None:
            continue
        cortes = sorted([a.x0 for a, _ in etiquetas] + [t.x0 for t in rucs])
        for a, c in etiquetas:
            if c["name"] in out:
                continue
            fin = next((x for x in cortes if x > a.x0 + 1), 1001)
            toks = [t for t in debajo.tokens if a.x0 - 4 <= t.cx < fin - 2]
            if not toks:
                continue
            crudo = " ".join(t.text for t in toks)
            valor = normalizar(crudo, c.get("type", "text"), opciones)
            if valor is None:
                continue
            out[c["name"]] = {"value": valor, "tag": c.get("tag", c["name"].upper()), "source": _src(toks[0]),
                              "_ancla": " ".join(t.text for t in a.tokens), "_abajo": True}
    return out


def _parece_etiqueta(v) -> bool:
    return isinstance(v, str) and bool(re.fullmatch(r"(?i)(RUC|R\.U\.C\.?|Medio|Periodo|Fecha|Anunciante|Proveedor|Cliente)"
                                                     r"(\s+(RUC|Medio|Periodo|Fecha|Anunciante|Proveedor|Cliente))*", v.strip()))


def _src(t: Token) -> dict:
    return {"page": t.page, "rawText": t.text,
            "bbox": {"x0": round(t.x0, 1), "y0": round(t.y0, 1), "x1": round(t.x1, 1), "y1": round(t.y1, 1)}}


# ------------------------------------------------------------------ lectura completa

_TIPOS_MEDIO = {"RADIO", "TV", "TELEVISION", "PRENSA", "DIGITAL", "CINE", "VIA PUBLICA", "EXTERIORES", "REVISTA", "DIARIO"}
_FECHA_SUELTA = re.compile(r"(\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|(ENERO|FEBRERO|MARZO|ABRIL|MAYO|JUNIO|JULIO|AGOSTO|SETIEMBRE|SEPTIEMBRE|OCTUBRE|NOVIEMBRE|DICIEMBRE)\s+\d{1,2},?\s+\d{4}|\d{1,2}\s+DE\s+[A-Z]+\s+DEL?\s+\d{4})", re.I)
_CORTE_RUC = re.compile(r"\s*-?\s*\bR\.?\s*U\.?\s*C\.?\s*(N[°ºo]\.?)?\s*:?")


def _pulir_cabecera(header: dict, lineas: list[Linea], opciones: dict):
    """Arreglos de la lectura por sinónimos: valores que se comen la etiqueta siguiente, el
    tipo de medio en lugar de la emisora, fecha de emisión sin etiqueta y mes del periodo."""
    for k, v in header.items():
        if isinstance(v.get("value"), str) and k != "proveedor":
            limpio = _CORTE_RUC.split(v["value"])[0].strip(" -:")
            if limpio and limpio != v["value"]:
                v["value"] = limpio
    medio = (header.get("medio") or {}).get("value")
    if medio and sin_tildes(medio).upper() in _TIPOS_MEDIO:
        otro = extraer_campos([_campo("medio", r"(EMISORA|ESTACION|CANAL|SE[ÑN]AL)\s*:")], lineas, opciones)
        if otro.get("medio") and sin_tildes(otro["medio"]["value"] or "").upper() not in _TIPOS_MEDIO:
            header["tipo_medio"] = header["medio"]
            header["medio"] = otro["medio"]
    if not (header.get("fecha_emision") or {}).get("value"):
        for l in lineas:
            if l.page != 1 or l.cy > 260 or re.search(r"(?i)periodo|vigencia|campa|\bdel\b|\bal\b", sin_tildes(l.texto)):
                continue
            m = _FECHA_SUELTA.search(sin_tildes(l.texto))
            if m:
                f = normalizar(m.group(0), "date", opciones)
                if f:
                    header["fecha_emision"] = {"value": f, "tag": "FECHA_EMISION", "source": _src(l.tokens[0])}
                    break
    if not (header.get("mes") or {}).get("value"):
        ini = (header.get("periodo_inicio") or header.get("campana_inicio") or {}).get("value")
        fin = (header.get("periodo_fin") or {}).get("value")
        if ini and (not fin or ini[:7] == fin[:7]):
            header["mes"] = {"value": ini[:7], "tag": "MES", "source": (header.get("periodo_inicio") or {}).get("source"),
                             "derived": True}


def _cabecera(doc: Documento, campos: list[dict], opciones: dict, config: dict) -> dict:
    from .pipeline import _expandir_periodos

    lineas = [l for p in doc.paginas for l in agrupar_lineas(p.tokens)]
    header = extraer_campos(campos, lineas, opciones)
    for k in [k for k, v in header.items() if _parece_etiqueta(v.get("value"))]:
        header.pop(k)  # leyó la etiqueta del recuadro vecino, no un valor
    for k, v in _recuadros(lineas, campos, opciones).items():
        if k not in header or header[k].get("value") in (None, ""):
            header[k] = v
    _expandir_periodos(header, campos)
    resolver_roles(doc, header, config)
    _pulir_cabecera(header, lineas, opciones)
    for k in [k for k, v in header.items() if v.get("value") in (None, "")]:
        header.pop(k)
    return header


def _armar(doc, header, det, plantilla_base, config):
    from .pipeline import _calendario, _programacion
    from .validar import validar

    lineas = [l for p in doc.paginas for l in agrupar_lineas(p.tokens)]
    tabla = extraer_tabla(doc, det, plantilla_base["options"])
    totals = _elegir_totales(lineas, tabla["rows"], _fin_de_filas(tabla))
    plantilla = dict(plantilla_base, detail=det)
    cal = _calendario(header, plantilla)
    filas = copy.deepcopy(tabla["rows"])
    _programacion(filas, cal)
    pie = tabla.get("footer") or {}
    if len(pie.get("_dias") or {}) < 3:
        pie = {}  # el renglón de corte no es un pie con totales por día ("Neto 4,056.00")
    datos = {"header": copy.deepcopy(header), "detail": filas, "totals": totals, "footer": pie,
             "_columnas": tabla.get("columns"), "_calendario": cal,
             "_ocr": any(p.origen == "ocr" for p in doc.paginas)}
    return plantilla, datos, validar(datos, plantilla, config)


def _fin_de_filas(tabla: dict) -> tuple | None:
    """(y, página) donde terminan las filas de la tabla: los totales se buscan desde ahí."""
    ys = []
    for f in tabla["rows"]:
        for v in f.values():
            src = v.get("source") if isinstance(v, dict) else None
            if src and src.get("bbox"):
                ys.append((src["page"], src["bbox"]["y1"]))
    if not ys:
        return tabla.get("limite")
    pag, y = max(ys)
    return (y + 1, pag)


def extraer_auto(doc: Documento, plantilla: dict, config: dict) -> dict:
    """Extracción con una plantilla aprendida: etiquetas exactas de esa agencia y la misma
    lógica de roles y de totales que la lectura genérica."""
    header = _cabecera(doc, plantilla["header"]["fields"], plantilla.get("options", {}), config)
    _, datos, _ = _armar(doc, header, plantilla["detail"], plantilla, config)
    return datos


def leer_generico(doc: Documento, config: dict):
    """Devuelve (plantilla_usada, datos, reporte) con la variante de tabla que mejor valida."""
    base = plantilla_generica()
    header = _cabecera(doc, base["header"]["fields"], base["options"], config)
    cabecera = _linea_cabecera(doc)
    mejor = None
    for det in _variantes_detalle(base["detail"], cabecera):
        plantilla, datos, rep = _armar(doc, header, det, base, config)
        puntos = (-rep["errors"], len(datos["detail"]) > 0, rep["passed"], -rep["warnings"])
        if mejor is None or puntos > mejor[0]:
            mejor = (puntos, plantilla, datos, rep)
    _, plantilla, datos, rep = mejor
    return plantilla, datos, rep


# ------------------------------------------------------------------ plantilla aprendida

def _nombre_plantilla(header: dict, doc: Documento) -> str:
    ag = (header.get("agencia") or {}).get("value") or doc.nombre
    s = unicodedata.normalize("NFKD", ag)
    s = "".join(c for c in s if not unicodedata.combining(c)).upper()
    s = re.sub(r"\b(S\.?A\.?C\.?|S\.?A\.?|S\.?R\.?L\.?|E\.?I\.?R\.?L\.?|SUCURSAL|DEL|DE|LA|EL)\b", " ", s)
    s = re.sub(r"[^A-Z0-9]+", "_", s).strip("_")[:28] or "NUEVA"
    return f"AUTO_{s}"


def plantilla_aprendida(doc: Documento, plantilla: dict, datos: dict, correcciones: dict | None = None) -> dict:
    """Plantilla propia para la agencia del documento: etiquetas exactas que usa, variante de
    tabla que cuadró y reglas para reconocerla (su RUC y frases fijas del encabezado)."""
    header = datos["header"]
    nueva = copy.deepcopy(plantilla)
    nueva["templateId"] = _nombre_plantilla(header, doc)
    nueva["status"] = "active"
    nueva["origin"] = "auto"
    nueva["learnedFrom"] = {"file": doc.nombre, "sha256": doc.sha256, "date": datetime.now().isoformat(timespec="seconds"),
                            "corrected": sorted(correcciones) if correcciones else []}
    nueva["description"] = f"Aprendida automáticamente de {doc.nombre}"
    # campos: solo los que aparecieron, con la etiqueta exacta
    campos = []
    for c in plantilla["header"]["fields"]:
        nombres = {c["name"]} | ({f"{c.get('splitAs', c['name'])}_inicio"} if c.get("type") == "period" else set())
        v = next((header[n] for n in nombres if n in header), None)
        if not v or not v.get("_ancla"):
            continue
        c2 = copy.deepcopy(c)
        c2["anchor"]["pattern"] = re.escape(v["_ancla"]).replace(r"\ ", " ")
        if v.get("_abajo"):
            c2["anchor"].update(searchDirection="DOWN", maxDistance=30, xSlack=4)
        valor = v.get("value")
        if isinstance(valor, str) and c.get("type", "text") == "text" and c["name"] in ("agencia", "anunciante", "medio", "proveedor"):
            c2["knownValues"] = [valor]  # forma exacta que usa esta agencia
        campos.append(c2)
    nueva["header"]["fields"] = campos
    # totales: etiquetas exactas que usó la combinación ganadora
    tot = []
    for k, v in datos["totals"].items():
        et = v.get("_ancla")
        if not et or k == "igv_pct":
            continue
        tipo = "int" if k == "total_avisos" else "money"
        tot.append({"name": k, "tag": f"TOT_{k.upper()}", "type": tipo,
                    "anchor": {"pattern": re.escape(et).replace(r"\ ", " "), "searchDirection": "RIGHT", "maxDistance": 420}})
        if k == "igv" and "igv_pct" in datos["totals"]:
            tot.append({"name": "igv_pct", "tag": "TOT_IGV_PCT", "type": "percent", "default": datos["totals"]["igv_pct"]["value"],
                        "anchor": {"pattern": re.escape(et).replace(r"\ ", " "), "searchDirection": "SELF"}})
    nueva["totals"] = {"searchBelowTable": True, "fields": tot}
    # reconocimiento: RUC de la agencia (si lo hay) y 2 o 3 etiquetas propias del formato
    reglas = []
    ruc = (header.get("agencia_ruc") or {}).get("value")
    if ruc:
        reglas.append({"type": "exact_text", "value": ruc, "weight": 40})
    nombre = (header.get("agencia") or {}).get("value")
    if nombre and len(nombre) >= 6:
        reglas.append({"type": "fuzzy_text", "value": nombre[:60], "weight": 30})
    etiquetas = [c["anchor"]["pattern"].replace("\\", "") for c in campos if len(c["anchor"]["pattern"]) >= 6][:6]
    resto = 100 - sum(r["weight"] for r in reglas)
    for e in etiquetas[:max(2, min(4, len(etiquetas)))]:
        reglas.append({"type": "exact_text", "value": e, "weight": max(resto // max(min(4, len(etiquetas)), 1), 5)})
    nueva["identification"] = {"minScoreThreshold": 70, "rules": reglas}
    nueva["detail"].pop("headerAnchors", None)
    nueva["detail"]["headerAnchors"] = _columnas_vistas(plantilla, datos)
    if nueva["detail"]["headerAnchors"]:
        nueva["detail"]["headerAnchors"][0].pop("region", None)
    return nueva


def _columnas_vistas(plantilla: dict, datos: dict) -> list[dict]:
    nombres = {c["name"] for c in (datos.get("_columnas") or []) if c.get("name") and not c.get("day")}
    return [copy.deepcopy(c) for c in plantilla["detail"]["headerAnchors"] if c["name"] in nombres or c["name"] == "descripcion"]


def guardar_plantilla(nueva: dict) -> str | None:
    """Escribe la plantilla en plantillas/ (nueva versión si ya existe). None si no se puede
    escribir (por ejemplo, en un despliegue de solo lectura)."""
    try:
        versiones = []
        for p in DIR_PLANTILLAS.glob(f"{nueva['templateId']}.v*.json"):
            m = re.search(r"\.v(\d+)\.json$", p.name)
            if m:
                versiones.append(int(m.group(1)))
        nueva["version"] = max(versiones) + 1 if versiones else 1
        destino = DIR_PLANTILLAS / f"{nueva['templateId']}.v{nueva['version']}.json"
        destino.write_text(json.dumps(_sin_privados(nueva), ensure_ascii=False, indent=2), encoding="utf-8")
        return str(destino.name)
    except OSError:
        return None


def _sin_privados(obj):
    if isinstance(obj, dict):
        return {k: _sin_privados(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [_sin_privados(v) for v in obj]
    return obj


# ------------------------------------------------------------------ aprender de una corrección

def _ubicar_valor(lineas: list[Linea], valor, tipo: str, opciones: dict):
    """Busca en el documento los tokens que forman `valor` (ya corregido). Devuelve
    (linea, i, j) del tramo o None."""
    objetivo = _clave(str(valor)) if tipo == "text" else valor
    for largo in range(1, 11):  # el tramo más corto primero: "2 de junio de 2025", no todo el renglón
      for l in lineas:
        n = len(l.tokens)
        for i in range(n):
            for j in [i + largo - 1] if i + largo - 1 < n else []:
                crudo = " ".join(t.text for t in l.tokens[i:j + 1])
                if tipo == "text":
                    if _clave(crudo) == objetivo:
                        return l, i, j
                else:
                    try:
                        if normalizar(crudo, tipo, opciones) == objetivo:
                            return l, i, j
                    except Exception:  # noqa: BLE001
                        pass
    return None


def _etiqueta_de(lineas: list[Linea], l: Linea, i: int):
    """Etiqueta del valor que empieza en l.tokens[i]: los tokens a su izquierda hasta un hueco
    grande, o el texto justo encima (recuadros). Devuelve (texto, abajo) o None."""
    izq = []
    k = i - 1
    while k >= 0:
        t, sig = l.tokens[k], l.tokens[k + 1]
        if sig.x0 - t.x1 > 3 * max(t.h, 1) and izq:
            break
        izq.insert(0, t)
        if t.text.endswith(":") and len(izq) >= 1 and k < i - 1:
            pass
        k -= 1
        if len(izq) >= 5 or (izq and izq[0].text.endswith(":") and k >= 0 and l.tokens[k].text.endswith(":")):
            break
    # se queda con lo que va después del último valor ajeno: desde el último token que termina en ':'
    if izq:
        corte = max((n for n, t in enumerate(izq[:-1]) if t.text.endswith(":")), default=-1)
        izq = izq[corte + 1:]
    lejos = 14 if izq and izq[-1].text.endswith(":") else 4  # 'Etiqueta:' puede estar más separada
    if izq and re.search(r"[A-Za-z]", " ".join(t.text for t in izq)) and l.tokens[i].x0 - izq[-1].x1 < lejos * max(izq[-1].h, 1):
        return " ".join(t.text for t in izq), False
    v = l.tokens[i]
    arriba = [x for x in lineas if x.page == l.page and 0 < l.cy - x.cy < 30]
    if arriba:
        x = max(arriba, key=lambda x: x.cy)
        toks = [t for t in x.tokens if abs(t.x0 - v.x0) < 25 or (t.x0 <= v.x0 <= t.x1)]
        if toks and re.search(r"[A-Za-z]", toks[0].text):
            # la etiqueta completa: tokens contiguos desde el primero
            k = x.tokens.index(toks[0])
            et = [x.tokens[k]]
            while k + 1 < len(x.tokens) and x.tokens[k + 1].x0 - x.tokens[k].x1 < 1.5 * max(x.tokens[k].h, 1):
                k += 1
                et.append(x.tokens[k])
            return " ".join(t.text for t in et), True
    return None


def aprender_de_correccion(doc: Documento, cabecera_corregida: dict, correcciones: list | None, config: dict) -> dict:
    """Plantilla para un formato nuevo a partir de los datos que el asesor dejó correctos.
    Los campos de cabecera corregidos se ubican en el documento para tomar su etiqueta."""
    plantilla, datos, _ = leer_generico(doc, config)
    lineas = [l for p in doc.paginas for l in agrupar_lineas(p.tokens)]
    tipos = {c["name"]: c.get("type", "text") for c in plantilla["header"]["fields"]}
    tipos.update({"periodo_inicio": "date", "periodo_fin": "date", "campana_inicio": "date", "campana_fin": "date"})
    header = datos["header"]
    campos_extra = []
    corregidos = {str(c.get("campo", "")).split(".", 1)[-1] for c in correcciones or [] if isinstance(c, dict)}
    for k, valor in (cabecera_corregida or {}).items():
        if valor in (None, ""):
            header.pop(k, None)
            continue
        actual = header.get(k)
        if actual and actual.get("value") == valor and (actual.get("_ancla") or k not in corregidos):
            continue  # sin cambios: queda lo que leyó el motor
        tipo = "date" if k.endswith(("_inicio", "_fin")) else tipos.get(k, "text")
        donde = _ubicar_valor(lineas, valor, tipo, plantilla.get("options", {}))
        entrada = {"value": valor, "tag": k.upper(), "source": None}
        if donde:
            l, i, j = donde
            entrada["source"] = _src(l.tokens[i])
            et = _etiqueta_de(lineas, l, i)
            if et:
                entrada["_ancla"], entrada["_abajo"] = et
                if k not in tipos:  # campo que la plantilla genérica no conocía
                    campos_extra.append({"name": k, "tag": k.upper(), "type": tipo,
                                         "anchor": {"pattern": re.escape(et[0]).replace(r"\ ", " "),
                                                    "searchDirection": "DOWN" if et[1] else "RIGHT", "maxDistance": 600}})
        header[k] = entrada
    base = copy.deepcopy(plantilla)
    base["header"]["fields"] = base["header"]["fields"] + campos_extra
    # los periodos corregidos: se aprende la etiqueta del campo 'periodo'
    for parte in ("periodo", "campana"):
        ini = header.get(f"{parte}_inicio")
        if ini and ini.get("_ancla"):
            nombre = "periodo" if parte == "periodo" else "campana_fechas"
            for c in base["header"]["fields"]:
                if c["name"] == nombre:
                    header.setdefault(nombre, {"value": None, "_ancla": ini["_ancla"], "_abajo": ini.get("_abajo")})
    nueva = plantilla_aprendida(doc, base, datos, {c.get("campo") for c in correcciones or [] if isinstance(c, dict)})
    return nueva
