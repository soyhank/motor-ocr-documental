"""Mide el mapeo automático de formatos nuevos ("dejando uno fuera").

1. Cada orden de muestras/ se lee como si su formato no existiera (plantilla genérica) y se
   compara con lo que extrae su plantilla verificada.
2. Por cada formato se aprende una plantilla con la primera orden y se aplica a las demás
   órdenes del mismo formato, como pasaría con la segunda orden de una agencia nueva.

Uso: python _scripts/evaluar_generica.py [--detalle]
"""
from __future__ import annotations

import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "_scripts"))

from benchmark_escaneos import comparar, valores  # noqa: E402
from motor.generica import leer_generico, plantilla_aprendida  # noqa: E402
from motor.ingesta import ingerir  # noqa: E402
from motor.pipeline import _limpiar, _estado, procesar  # noqa: E402
from motor.validar import cargar_config  # noqa: E402

# lo que se compara: datos de negocio (no direcciones, observaciones ni campos propios de un formato)
CLAVES = {"header.numero_orden", "header.fecha_emision", "header.agencia", "header.agencia_ruc", "header.anunciante",
          "header.proveedor_ruc", "header.medio", "header.producto", "header.periodo_inicio", "header.periodo_fin",
          "header.mes", "totals.neto", "totals.igv", "totals.total", "totals.descuento"}


def _como_resultado(plantilla, datos, rep, nombre):
    return {"file": nombre, "status": _estado(rep), "template": {"templateId": plantilla["templateId"]},
            "extractedData": _limpiar({k: v for k, v in datos.items() if not k.startswith("_")})}


def _filtrar(v: dict, ref: dict | None = None) -> dict:
    out = {k: x for k, x in v.items() if k.startswith("_") or k in CLAVES or k.startswith("fila")}
    if ref is not None and "totals.descuento" not in ref:
        out.pop("totals.descuento", None)  # la orden no trae descuento: leer 0 no es error
    return out


def main(args):
    detalle = "--detalle" in args
    config = cargar_config()
    pdfs = sorted(p for p in (RAIZ / "muestras").iterdir() if p.suffix.lower() == ".pdf")
    por_formato: dict[str, list] = {}
    tot_ok = tot = 0
    print("1) Formato desconocido (plantilla genérica)")
    for p in pdfs:
        ref_r = procesar(p)
        ref = _filtrar(valores(ref_r))
        doc = ingerir(p)
        plantilla, datos, rep = leer_generico(doc, config)
        obt = _filtrar(valores(_como_resultado(plantilla, datos, rep, p.name)), ref)
        ok, n, fallos = comparar(ref, obt)
        tot_ok += ok
        tot += n
        valido = "cuadra" if rep["isValid"] else f"{rep['errors']} errores"
        print(f"  {p.stem[:38]:38} {ok:3}/{n:3}  {valido}")
        if detalle:
            for f in fallos:
                print("      ", f)
        agencia = (ref_r["extractedData"]["header"].get("agencia") or {}).get("value") or ref_r["template"]["templateId"]
        por_formato.setdefault(agencia, []).append((p, doc, plantilla, datos, rep, ref))
    print(f"  total: {tot_ok}/{tot} = {tot_ok / max(tot, 1):.1%}\n")

    print("2) Siguientes órdenes de la misma agencia (plantilla aprendida de la primera)")
    ok2 = n2 = 0
    for fmt, items in por_formato.items():
        if len(items) < 2:
            continue
        p0, doc0, pl0, datos0, rep0, _ = items[0]
        nueva = plantilla_aprendida(doc0, pl0, datos0)
        for p, doc, *_, ref in items[1:]:
            r = procesar(p, forzar_plantilla=nueva)
            # además: ¿la reconocería sola entre las plantillas existentes?
            from motor.plantillas import puntuar
            puntaje = puntuar(doc, nueva)[0]
            obt = _filtrar(valores(r), ref)
            ok, n, fallos = comparar(ref, obt)
            ok2 += ok
            n2 += n
            print(f"  {fmt[:26]:26} {p.stem[:30]:30} {ok:3}/{n:3}  reconocida con {puntaje} puntos  {r['status']}")
            if detalle:
                for f in fallos:
                    print("      ", f)
    print(f"  total: {ok2}/{n2} = {ok2 / max(n2, 1):.1%}")


if __name__ == "__main__":
    main(sys.argv[1:])
