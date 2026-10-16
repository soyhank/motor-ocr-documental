"""Prueba el mapeo automático con formatos que el motor nunca vio (tests/formatos_nuevos/).

Para cada orden inventada (PDF y escaneo) corre el pipeline completo, sin plantilla para
ese formato, y compara con los valores esperados. Con --aprender, después de la primera
lectura se guarda la plantilla aprendida y se vuelve a procesar para ver que la reconoce.

Uso: python _scripts/evaluar_formatos_nuevos.py [--detalle]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from motor.generica import leer_generico  # noqa: E402
from motor.ingesta import ingerir  # noqa: E402
from motor.validar import cargar_config  # noqa: E402

CAB = ("agencia", "agencia_ruc", "anunciante", "anunciante_ruc", "numero_orden", "fecha_emision", "medio",
       "periodo_inicio", "periodo_fin")
TOT = ("neto", "igv", "total")


def comparar(esp: dict, datos: dict) -> tuple[int, int, list[str]]:
    ok = n = 0
    fallos = []
    h, t = datos["header"], datos["totals"]

    def chk(nombre, a, b):
        nonlocal ok, n
        n += 1
        igual = abs(a - b) < 0.01 if isinstance(a, (int, float)) and isinstance(b, (int, float)) else a == b
        if igual:
            ok += 1
        else:
            fallos.append(f"{nombre}: esperado {a!r}, leído {b!r}")

    for k in CAB:
        if k in esp:
            chk(k, esp[k], (h.get(k) or {}).get("value"))
    for k in TOT:
        chk(k, esp[k], (t.get(k) or {}).get("value"))
    filas = datos["detail"]
    chk("filas", len(esp["filas"]), len(filas))
    for i, (e, f) in enumerate(zip(esp["filas"], filas), 1):
        for k in ("total_avisos", "costo_unitario", "costo_total", "duracion", "horario"):
            chk(f"fila{i}.{k}", e[k], (f.get(k) or {}).get("value"))
        if "dias" in e:
            leidos = {d["dia"]: d["avisos"] for d in f.get("programacion", [])}
            chk(f"fila{i}.dias", {int(k): v for k, v in e["dias"].items()}, leidos)
    return ok, n, fallos


def main(args):
    detalle = "--detalle" in args
    carpeta = RAIZ / "tests" / "formatos_nuevos"
    config = cargar_config()
    tot_ok = tot = 0
    for esp_p in sorted(carpeta.glob("*.esperado.json")):
        base = esp_p.name.replace(".esperado.json", "")
        esp = json.loads(esp_p.read_text(encoding="utf-8"))
        for archivo in (carpeta / f"{base}.pdf", carpeta / f"{base}__escaneo.png"):
            doc = ingerir(archivo)
            _, datos, rep = leer_generico(doc, config)
            ok, n, fallos = comparar(esp, datos)
            tot_ok += ok
            tot += n
            print(f"  {archivo.name:32} {ok:3}/{n:3}  {'cuadra' if rep['isValid'] else str(rep['errors']) + ' errores'}")
            if detalle:
                for f in fallos:
                    print("      ", f)
    print(f"  total: {tot_ok}/{tot} = {tot_ok / max(tot, 1):.1%}")


if __name__ == "__main__":
    main(sys.argv[1:])
