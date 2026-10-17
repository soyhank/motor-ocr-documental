"""Calibra el puntaje de certeza: para cada escaneo simulado compara lo leído con el PDF
digital y guarda el puntaje que dio el motor junto a la exactitud real.

Uso: python _scripts/calibrar_certeza.py [--variantes=malo,celular] [--salida=archivo.json]
     python _scripts/calibrar_certeza.py --resumir a.json b.json ...

Pregunta que responde: cuando el motor dice certeza ALTA, ¿cuántas veces la orden salió
perfecta? Si es casi siempre, el puntaje sirve para aprobar sin revisar.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "_scripts"))

from benchmark_escaneos import AFINADO, comparar, valores  # noqa: E402
from motor.certeza import certeza  # noqa: E402
from motor.pipeline import procesar  # noqa: E402


def medir(variantes: list[str] | None) -> list[dict]:
    pdfs = {p.stem[:40].replace(" ", "_"): p for p in (RAIZ / "muestras").iterdir() if p.suffix.lower() == ".pdf"}
    refs, filas = {}, []
    carpeta = RAIZ / "tests" / "escaneos"
    for img in sorted([*carpeta.glob("*__*.png"), *carpeta.glob("*__*.jpg")]):
        base, variante = img.stem.rsplit("__", 1)
        if variantes and variante not in variantes:
            continue
        if base not in refs:
            refs[base] = valores(procesar(pdfs[base]))
        r = procesar(img)
        r.pop("_doc", None)
        ok, tot, fallos = comparar(refs[base], valores(r), base)
        c = certeza(r)
        filas.append({"orden": base, "variante": variante, "grupo": "afinado" if base in AFINADO else "a ciegas",
                      "aciertos": ok, "datos": tot, "perfecta": not fallos, "estado": r.get("status"),
                      "certeza": c["puntaje"], "nivel": c["nivel"], "lectura": c["lectura"],
                      "dudosos": [d["campo"] for d in c["dudosos"]], "fallos": [f.split(":")[0] for f in fallos]})
        print(f"{variante:8} {base[:34]:34} certeza={c['puntaje']:5.1f} {c['nivel']:5}  {ok}/{tot}"
              f"{'' if not fallos else '  fallos=' + ','.join(filas[-1]['fallos'][:4])}", flush=True)
    return filas


def resumir(filas: list[dict]):
    print(f"\n{'nivel':6} {'órdenes':>8} {'perfectas':>10} {'exactitud':>10}")
    for nivel in ("ALTA", "MEDIA", "BAJA"):
        g = [f for f in filas if f["nivel"] == nivel]
        if not g:
            continue
        ok, tot = sum(f["aciertos"] for f in g), sum(f["datos"] for f in g)
        print(f"{nivel:6} {len(g):8} {sum(f['perfecta'] for f in g):10} {ok / max(tot, 1):10.2%}")
    # de los datos mal leídos, cuántos el motor marcó como dudosos o hizo fallar una validación
    mal = [(f, k) for f in filas for k in f["fallos"]]
    vistos = sum(1 for f, k in mal if k in f["dudosos"] or f["estado"] != "COMPLETED" or f["nivel"] != "ALTA")
    print(f"\nDatos mal leídos: {len(mal)}; detectados (dato dudoso, validación fallida o certeza no ALTA): {vistos}")
    alta_mal = [f for f in filas if f["nivel"] == "ALTA" and not f["perfecta"]]
    print(f"Órdenes con certeza ALTA que tenían algún error: {len(alta_mal)} de {sum(f['nivel'] == 'ALTA' for f in filas)}")
    for f in alta_mal:
        print("   ", f["variante"], f["orden"], f["fallos"])


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--resumir" in args:
        filas = [x for a in args if a.endswith(".json") for x in json.loads(Path(a).read_text(encoding="utf-8"))]
        resumir(filas)
        sys.exit()
    variantes = next((a.split("=", 1)[1].split(",") for a in args if a.startswith("--variantes=")), None)
    salida = next((a.split("=", 1)[1] for a in args if a.startswith("--salida=")),
                  str(RAIZ / "salida" / "calibracion_certeza.json"))
    filas = medir(variantes)
    Path(salida).write_text(json.dumps(filas, ensure_ascii=False, indent=1), encoding="utf-8")
    resumir(filas)
