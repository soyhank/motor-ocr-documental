"""Etapa 7: validación aritmética y de reglas de negocio.

Las reglas declarativas usan una mini-expresión segura (sin eval):
    SUM(detail.costo_total) == totals.neto
    ROUND(totals.neto * COALESCE(totals.igv_pct, 0.18), 2) == totals.igv
    SUM(row.dias) == row.total_avisos          (scope = "row")
Si a una regla le falta algún valor se marca SKIPPED, no como error.
"""
from __future__ import annotations

import ast
import calendar
import json
import operator
import re
from datetime import date
from pathlib import Path

from .normalizar import DIAS_SEMANA, ruc_valido

RAIZ = Path(__file__).resolve().parent.parent


class _Faltante(Exception):
    pass


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}
_CMP = {ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">="}


def _sum(x):
    vals = [v for v in (x if isinstance(x, list) else [x]) if v is not None]
    if not vals:
        raise _Faltante()
    return sum(vals)


def _coalesce(*xs):
    for x in xs:
        if x is not None:
            return x
    raise _Faltante()


FUNCIONES = {
    "SUM": _sum,
    "COUNT": lambda x: len(x) if isinstance(x, list) else int(x is not None),
    "ROUND": lambda x, n=2: round(x, int(n)),
    "ABS": abs,
    "MAX": lambda *x: max(x[0]) if len(x) == 1 and isinstance(x[0], list) else max(x),
    "MIN": lambda *x: min(x[0]) if len(x) == 1 and isinstance(x[0], list) else min(x),
}


class Evaluador:
    def __init__(self, contexto: dict):
        self.ctx = contexto

    def valor(self, nodo):
        if isinstance(nodo, ast.Constant):
            return nodo.value
        if isinstance(nodo, ast.Name):
            if nodo.id not in self.ctx:
                raise _Faltante()
            return self.ctx[nodo.id]
        if isinstance(nodo, ast.Attribute):
            base = self.valor(nodo.value)
            if isinstance(base, list):  # detail.campo -> lista de valores por fila
                return [f.get(nodo.attr) if isinstance(f, dict) else None for f in base]
            if isinstance(base, dict):
                return base.get(nodo.attr)
            raise _Faltante()
        if isinstance(nodo, ast.BinOp) and type(nodo.op) in _OPS:
            a, b = self._req(nodo.left), self._req(nodo.right)
            return _OPS[type(nodo.op)](a, b)
        if isinstance(nodo, ast.UnaryOp) and isinstance(nodo.op, ast.USub):
            return -self._req(nodo.operand)
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name):
            nombre = nodo.func.id.upper()
            if nombre == "COALESCE":
                vals = []
                for a in nodo.args:
                    try:
                        vals.append(self.valor(a))
                    except _Faltante:
                        vals.append(None)
                return _coalesce(*vals)
            if nombre in FUNCIONES:
                return FUNCIONES[nombre](*[self.valor(a) for a in nodo.args])
        raise ValueError(f"Expresión no permitida: {ast.dump(nodo)}")

    def _req(self, nodo):
        v = self.valor(nodo)
        if v is None:
            raise _Faltante()
        return v


def evaluar_regla(regla: dict, contexto: dict) -> dict:
    expr = regla["rule"]
    tol = regla.get("tolerance", 0.0)
    res = {"rule": expr, "severity": regla.get("severity", "ERROR"),
           "description": regla.get("description", "")}
    try:
        arbol = ast.parse(expr, mode="eval").body
        ev = Evaluador(contexto)
        if isinstance(arbol, ast.Compare) and len(arbol.ops) == 1:
            izq, der = ev._req(arbol.left), ev._req(arbol.comparators[0])
            op = _CMP[type(arbol.ops[0])]
            tol_total = tol + regla.get("toleranceRel", 0) * max(abs(izq), abs(der))
            if op == "==":
                ok = abs(izq - der) <= tol_total + 1e-9
            elif op == "!=":
                ok = abs(izq - der) > tol_total
            else:
                ok = {"<": izq < der, "<=": izq <= der + tol_total, ">": izq > der, ">=": izq >= der - tol_total}[op]
            res.update(status="PASSED" if ok else "FAILED", passed=ok,
                       left=round(izq, 4) if isinstance(izq, float) else izq,
                       right=round(der, 4) if isinstance(der, float) else der,
                       difference=round(izq - der, 4) if isinstance(izq, (int, float)) else None)
        else:
            ok = bool(ev._req(arbol))
            res.update(status="PASSED" if ok else "FAILED", passed=ok)
    except _Faltante:
        res.update(status="SKIPPED", passed=None)
    except (ValueError, SyntaxError, ZeroDivisionError, TypeError) as e:
        res.update(status="ERROR", passed=False, detail=str(e))
    return res


# ---------------------------------------------------------------- contexto plano

def _plano(campos: dict | None) -> dict:
    return {k: (v.get("value") if isinstance(v, dict) else v) for k, v in (campos or {}).items()
            if not k.startswith("_")}


def contexto(datos: dict) -> dict:
    filas = []
    for f in datos.get("detail", []):
        p = _plano(f)
        p["dias"] = [d["avisos"] for d in f.get("programacion", [])] or [d["avisos"] for d in f.get("_dias", {}).values()]
        filas.append(p)
    return {
        "header": _plano(datos.get("header")),
        "totals": _plano(datos.get("totals")),
        "footer": _plano(datos.get("footer")),
        "detail": filas,
    }


# ---------------------------------------------------------------- reglas base

def reglas_base(tipo_documento: str) -> list[dict]:
    p = RAIZ / "plantillas" / "_reglas_base" / f"{tipo_documento}.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return []


def cargar_config() -> dict:
    p = RAIZ / "config.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


# ---------------------------------------------------------------- revisiones de negocio

def _r(regla, ok, severidad="WARNING", detalle=None, descripcion=""):
    return {"rule": regla, "status": "PASSED" if ok else "FAILED", "passed": ok, "severity": severidad,
            "detail": detalle, "description": descripcion}


def revisiones_negocio(datos: dict, plantilla: dict, config: dict) -> list[dict]:
    out = []
    h = _plano(datos.get("header"))
    desactivadas = set(plantilla.get("disableChecks", []))

    # RUC con dígito verificador
    if "ruc" not in desactivadas:
        for k, v in h.items():
            if k.endswith("ruc") and v:
                ok = ruc_valido(v)
                out.append(_r(f"RUC_VALIDO({k})", ok, "ERROR", None if ok else f"{v} no pasa el dígito verificador SUNAT",
                              "El RUC cumple el dígito verificador de SUNAT"))

    # la orden está dirigida a la empresa que la recibe
    receptor = config.get("empresaReceptora", {})
    if receptor and "receptor" not in desactivadas:
        ruc_p = h.get("proveedor_ruc")
        nombre_p = (h.get("proveedor") or "").upper()
        if ruc_p:
            ok = ruc_p in receptor.get("rucs", [receptor.get("ruc")])
            det = None if ok else f"RUC del proveedor {ruc_p} distinto de {receptor.get('ruc')}"
        elif nombre_p:
            ok = any(a in nombre_p.replace(" ", "") for a in receptor.get("alias", []))
            det = None if ok else f"Proveedor «{h.get('proveedor')}» no reconocido"
        else:
            ok, det = None, None
        if ok is not None:
            out.append(_r("PROVEEDOR_ES_RECEPTOR", ok, "ERROR", det,
                          f"La orden está emitida a {receptor.get('nombre')}"))

    filas = datos.get("detail", [])
    prog = [(f, d) for f in filas for d in f.get("programacion", [])]
    calendario_mes = datos.get("_calendario")

    # las letras de la cabecera coinciden con el mes/año inferido
    if calendario_mes and "calendario" not in desactivadas:
        cols = [c for c in datos.get("_columnas") or [] if c.get("day") and c.get("weekday")]
        if cols:
            a, m = calendario_mes
            malas = []
            for c in cols:
                if c["day"] > calendar.monthrange(a, m)[1]:
                    malas.append(f"día {c['day']} no existe")
                    continue
                esperado = date(a, m, c["day"]).weekday()
                if esperado not in DIAS_SEMANA.get(c["weekday"], set()):
                    malas.append(f"día {c['day']}={c['weekday']}")
            # en escaneados alguna letra suelta se lee mal; solo falla si no cuadra una parte clara del cuadro
            tolerancia = 0 if not datos.get("_ocr") else max(1, len(cols) // 4)
            ok = len(malas) <= tolerancia
            out.append(_r("CALENDARIO_COHERENTE", ok, "ERROR",
                          ", ".join(malas[:6]) or None,
                          f"Los días de la semana del cuadro corresponden a {m:02d}/{a}"))

    # avisos dentro del periodo declarado
    ini, fin = h.get("periodo_inicio"), h.get("periodo_fin")
    if prog and ini and fin and "periodo" not in desactivadas:
        fuera = sorted({d["fecha"] for _, d in prog if d.get("fecha") and not (ini <= d["fecha"] <= fin)})
        out.append(_r("AVISOS_DENTRO_DEL_PERIODO", not fuera, "WARNING",
                      (f"{len(fuera)} fecha(s) fuera de {ini} a {fin}: " + ", ".join(fuera[:5])) if fuera else None,
                      "Todas las fechas programadas caen dentro del periodo de la orden"))

    # el mes pautado está dentro de las fechas de campaña
    ci, cf = h.get("campana_inicio"), h.get("campana_fin")
    if prog and ci and cf and "campana" not in desactivadas:
        fuera = sorted({d["fecha"] for _, d in prog if d.get("fecha") and not (ci <= d["fecha"] <= cf)})
        out.append(_r("PAUTA_DENTRO_DE_CAMPANA", not fuera, "WARNING",
                      (f"{len(fuera)} fecha(s) fuera de la campaña {ci} a {cf} (de {fuera[0]} a {fuera[-1]})") if fuera else None,
                      "La pauta está dentro de las fechas de campaña"))

    # orden emitida antes de que empiece la pauta
    emi = h.get("fecha_emision")
    if prog and emi and "retroactiva" not in desactivadas:
        primera = min(d["fecha"] for _, d in prog if d.get("fecha"))
        ok = primera >= emi
        out.append(_r("ORDEN_ANTES_DE_LA_PAUTA", ok, "WARNING",
                      None if ok else f"La pauta empieza el {primera} y la orden se emitió el {emi}",
                      "La orden se emitió antes del primer aviso"))

    # totales diarios del pie = suma de las filas
    pie = datos.get("footer") or {}
    pie_dias = {d: v["avisos"] for d, v in (pie.get("_dias") or {}).items()}
    if pie_dias and filas and "pie" not in desactivadas:
        suma: dict[int, int] = {}
        for f in filas:
            for d in f.get("programacion", []):
                suma[d["dia"]] = suma.get(d["dia"], 0) + d["avisos"]
        dif = [f"día {d}: pie {pie_dias.get(d, 0)} vs filas {suma.get(d, 0)}"
               for d in sorted(set(pie_dias) | set(suma)) if pie_dias.get(d, 0) != suma.get(d, 0)]
        out.append(_r("TOTALES_DIARIOS_DEL_PIE", not dif, "ERROR", "; ".join(dif[:5]) or None,
                      "Los totales por día del pie suman lo mismo que las filas"))

    # cada fila pautada debe decir en qué horario sale
    if filas and "horario" not in desactivadas:
        sin = [str(i) for i, f in enumerate(filas, 1)
               if f.get("programacion") and not (f.get("horario") or {}).get("value")
               and not (f.get("es_paquete") or {}).get("value")]
        hay_col = any(c.get("name") == "horario" for c in datos.get("_columnas") or []) or any("horario" in f for f in filas)
        if hay_col:
            out.append(_r("HORARIO_PAUTADO", not sin, "WARNING",
                          f"fila(s) {', '.join(sin)} sin horario" if sin else None,
                          "Cada fila con avisos indica su horario de emisión"))

    # bonificación anotada en observaciones ("Bonificados del 20%") frente a la pauta
    obs = " ".join(str(v) for k, v in h.items() if k.startswith("observ") and v)
    m = re.search(r"bonific\w*\s+(?:del?\s+)?(\d+(?:[.,]\d+)?)\s*%", obs, re.IGNORECASE)
    if m and filas and "bonificacion" not in desactivadas:
        pct = float(m.group(1).replace(",", ".")) / 100
        bon = sum((f.get("total_avisos") or {}).get("value") or 0 for f in filas if (f.get("bonificado") or {}).get("value"))
        pag = sum((f.get("total_avisos") or {}).get("value") or 0 for f in filas if not (f.get("bonificado") or {}).get("value"))
        ok = pag > 0 and abs(bon - pag * pct) <= 1
        out.append(_r("BONIFICACION_DECLARADA", ok, "WARNING",
                      f"{bon} bonificados sobre {pag} pagados = {bon / pag:.1%} (declarado {pct:.0%})" if pag else None,
                      "Los avisos bonificados equivalen al porcentaje anotado"))

    # campos obligatorios
    for c in plantilla.get("header", {}).get("fields", []) + plantilla.get("totals", {}).get("fields", []):
        nombres = [c["name"]] if c.get("type") != "period" else [f"{c.get('splitAs', c['name'])}_inicio"]
        if c.get("required") and all(datos.get("header", {}).get(n) is None
                                     and datos.get("totals", {}).get(n) is None for n in nombres):
            out.append(_r(f"REQUERIDO({c['name']})", False, "ERROR", "No se encontró el campo", "Campo obligatorio presente"))
    if not filas and plantilla.get("detail", {}).get("required", True):
        out.append(_r("DETALLE_PRESENTE", False, "ERROR", "No se encontraron filas en la tabla", "La tabla tiene filas"))
    return out


def validar(datos: dict, plantilla: dict, config: dict) -> dict:
    ctx = contexto(datos)
    ejecutadas = []
    reglas = reglas_base(plantilla.get("documentType", "")) + plantilla.get("validations", [])
    omitidas = set(plantilla.get("disableRules", []))
    for regla in reglas:
        if regla.get("id") in omitidas:
            continue
        if regla.get("scope") == "row":
            for i, fila in enumerate(ctx["detail"]):
                if regla.get("skipIf") and any(fila.get(k) for k in regla["skipIf"]):
                    continue
                r = evaluar_regla(regla, {**ctx, "row": fila})
                if r["status"] == "SKIPPED":
                    continue
                r["row"] = i + 1
                ejecutadas.append(r)
        else:
            ejecutadas.append(evaluar_regla(regla, ctx))
    ejecutadas += revisiones_negocio(datos, plantilla, config)
    errores = [r for r in ejecutadas if r["passed"] is False and r["severity"] == "ERROR"]
    avisos = [r for r in ejecutadas if r["passed"] is False and r["severity"] != "ERROR"]
    return {
        "isValid": not errores,
        "errors": len(errores),
        "warnings": len(avisos),
        "passed": sum(1 for r in ejecutadas if r["passed"]),
        "skipped": sum(1 for r in ejecutadas if r["status"] == "SKIPPED"),
        "executedRules": ejecutadas,
    }
