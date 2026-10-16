"""Mide la exactitud del OCR: compara cada escaneo simulado con su PDF digital.

Cuenta como "campo" cada dato de cabecera y totales, cada celda numérica de las filas
(avisos, costo unitario, costo total, duración) y cada día con avisos.

Uso: python _scripts/benchmark_escaneos.py [--detalle] [nombre_parcial ...]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from motor.pipeline import procesar  # noqa: E402

CAMPOS_FILA = ("total_avisos", "costo_unitario", "costo_total", "duracion", "horario")
IGNORAR = {"condiciones_paquete", "acuerdo", "agencia_telefono", "materiales", "burst", "observaciones",
           "agencia_direccion", "anunciante_direccion", "proveedor_direccion"}


def valores(r: dict) -> dict:
    """Aplana el resultado en {clave: valor} comparable."""
    out = {}
    d = r.get("extractedData") or {}
    for sec in ("header", "totals"):
        for k, v in (d.get(sec) or {}).items():
            if k not in IGNORAR:
                out[f"{sec}.{k}"] = v.get("value")
    for i, f in enumerate(d.get("detail", []), 1):
        for k in CAMPOS_FILA:
            if k in f:
                out[f"fila{i}.{k}"] = f[k].get("value")
        for p in f.get("programacion", []):
            out[f"fila{i}.d{p['dia']}"] = p["avisos"]
    out["_filas"] = len(d.get("detail", []))
    out["_status"] = r.get("status")
    out["_template"] = (r.get("template") or {}).get("templateId")
    return out


def comparar(ref: dict, obt: dict) -> tuple[int, int, list[str]]:
    aciertos, total, fallos = 0, 0, []
    for k, v in ref.items():
        if k.startswith("_"):
            continue
        total += 1
        o = obt.get(k)
        igual = (abs(v - o) < 0.005 if isinstance(v, (int, float)) and isinstance(o, (int, float)) else v == o)
        if igual:
            aciertos += 1
        else:
            fallos.append(f"{k}: esperado {v!r}, leído {o!r}")
    # días leídos de más también cuentan como error
    for k in obt:
        if k not in ref and not k.startswith("_") and ".d" in k:
            total += 1
            fallos.append(f"{k}: leído {obt[k]!r} donde no había nada")
    return aciertos, total, fallos


def main(args: list[str]):
    detalle = "--detalle" in args
    filtros = [a for a in args if not a.startswith("--")]
    carpeta = RAIZ / "tests" / "escaneos"
    pdfs = {p.stem[:40].replace(" ", "_"): p for p in (RAIZ / "muestras").iterdir() if p.suffix.lower() == ".pdf"}
    refs = {}
    resumen = {}
    t0 = time.perf_counter()
    for png in sorted(carpeta.glob("*__*.png")):
        base, variante = png.stem.rsplit("__", 1)
        if filtros and not any(f.lower() in png.name.lower() for f in filtros):
            continue
        if base not in refs:
            refs[base] = valores(procesar(pdfs[base]))
        ref = refs[base]
        obt = valores(procesar(png))
        ok, tot, fallos = comparar(ref, obt)
        resumen.setdefault(variante, [0, 0, 0, 0])
        resumen[variante][0] += ok
        resumen[variante][1] += tot
        resumen[variante][2] += 1
        resumen[variante][3] += obt["_status"] == ref["_status"]
        marca = "OK " if not fallos else f"{len(fallos):2d}x"
        print(f"[{marca}] {variante:6} {base[:36]:36} {ok}/{tot}  plantilla={'ok' if obt['_template']==ref['_template'] else obt['_template']}  estado={obt['_status']}")
        if detalle:
            for f in fallos:
                print("        ", f)
    print()
    for v, (ok, tot, n, est) in resumen.items():
        print(f"{v:6}: {ok}/{tot} campos = {ok / max(tot, 1):.1%}   estado igual al original {est}/{n}")
    print(f"{time.perf_counter() - t0:.0f} s")


if __name__ == "__main__":
    main(sys.argv[1:])
