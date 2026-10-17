"""Puntaje de certeza de una orden (0 a 100) y lista de datos dudosos.

Combina tres señales independientes:

* lectura: confianza del reconocedor en cada dato (1.0 en PDF digital; en escaneos, la del
  token más dudoso del dato). Un dato corregido a mano cuenta como 1.0.
* validación: las reglas aritméticas y de negocio (sumas de avisos, costos, IGV, totales,
  fechas, RUC). Que cuadren es la prueba más fuerte de que la lectura es correcta.
* identificación: qué tan seguro está el motor del formato (puntaje de la plantilla).

La calibración con el banco de escaneos simulados está en _scripts/calibrar_certeza.py.
"""
from __future__ import annotations

PESO_LECTURA, PESO_VALIDACION, PESO_FORMATO = 0.30, 0.50, 0.20
UMBRAL_DUDOSO = 0.85
NIVELES = ((90, "ALTA", "Lista para aprobar"), (75, "MEDIA", "Revisión rápida de los datos marcados"),
           (0, "BAJA", "Revisión completa"))


def _confianzas(datos: dict, corregidos: set[str]):
    """(clave, confianza) de cada dato leído del documento."""
    for sec, pref in (("header", "header"), ("totals", "totals")):
        for k, v in (datos.get(sec) or {}).items():
            src = isinstance(v, dict) and v.get("source")
            if src and src.get("confidence") is not None:
                yield f"{pref}.{k}", 1.0 if f"{pref}.{k}" in corregidos else float(src["confidence"])
    for i, f in enumerate(datos.get("detail") or [], 1):
        for k, v in f.items():
            src = isinstance(v, dict) and v.get("source")
            if k != "programacion" and src and src.get("confidence") is not None:
                yield f"fila{i}.{k}", 1.0 if f"fila{i}.{k}" in corregidos else float(src["confidence"])
        for p in f.get("programacion") or []:
            src = p.get("source")
            if src and src.get("confidence") is not None:
                c = f"fila{i}.d{p.get('dia')}"
                yield c, 1.0 if c in corregidos else float(src["confidence"])


def certeza(res: dict) -> dict:
    estado = res.get("status")
    if estado == "ERROR":
        return {"puntaje": 0, "nivel": "BAJA", "accion": "El archivo no se pudo leer", "lectura": 0, "validacion": 0,
                "formato": 0, "datos": 0, "dudosos": []}
    corregidos = {c.get("campo") for c in res.get("corrections") or []}
    conf = list(_confianzas(res.get("extractedData") or {}, corregidos))
    if conf:
        lectura = sum(c for _, c in conf) / len(conf)
    else:
        # JSON limpio revalidado por la API: no trae la confianza de cada dato, se conserva la anterior
        lectura = float(res.get("_lecturaPrevia", 0.0)) / 100
    # los datos dudosos pesan más que su promedio: uno ilegible basta para revisar
    dudosos = sorted(((k, c) for k, c in conf if c < UMBRAL_DUDOSO), key=lambda x: x[1])
    lectura = max(0.0, lectura - 0.02 * len(dudosos))

    rep = res.get("validationReport") or {}
    reglas = rep.get("executedRules") or []
    errores = sum(1 for r in reglas if r.get("passed") is False and r.get("severity") == "ERROR")
    avisos = sum(1 for r in reglas if r.get("passed") is False and r.get("severity") != "ERROR")
    if not reglas:
        validacion = 0.0
    else:
        validacion = max(0.0, 1.0 - 0.25 * errores - 0.03 * avisos)

    tpl = res.get("template") or {}
    if tpl.get("score") is not None:
        formato = min(1.0, float(tpl["score"]) / 100)
    elif tpl.get("templateId"):
        formato = 0.85  # orden revalidada: la plantilla ya se conocía
    else:
        formato = 0.0
    if res.get("newFormat") and not tpl.get("learned"):
        formato = min(formato, 0.6)

    puntaje = 100 * (PESO_LECTURA * lectura + PESO_VALIDACION * validacion + PESO_FORMATO * formato)
    if errores:
        puntaje = min(puntaje, 70)  # si algo no cuadra, nunca se aprueba sola
    puntaje = round(max(0.0, min(100.0, puntaje)), 1)
    nivel, accion = next((n, a) for u, n, a in NIVELES if puntaje >= u)
    if nivel == "ALTA" and avisos:
        accion = "Revisa las observaciones antes de aprobar"
    return {"puntaje": puntaje, "nivel": nivel, "accion": accion, "lectura": round(100 * lectura, 1),
            "validacion": round(100 * validacion, 1), "formato": round(100 * formato, 1), "datos": len(conf),
            "dudosos": [{"campo": k, "confianza": round(c, 3)} for k, c in dudosos[:30]]}
