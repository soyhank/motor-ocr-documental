"""Línea de comandos.

  python -m motor procesar <archivos o carpetas> [--salida salida] [--detalle]
  python -m motor calibrar <archivo>              -> volcado + borrador + prompt en cola_aprendizaje/
  python -m motor probar <plantilla.json> <archivos...> [--promover]
  python -m motor plantillas
  python -m motor web [--puerto 8765]
"""
from __future__ import annotations

import argparse
import os
import json
import sys
from pathlib import Path


def _entradas(rutas: list[str]) -> list[Path]:
    out = []
    for r in rutas:
        p = Path(r)
        if p.is_dir():
            out += sorted(x for x in p.iterdir() if x.is_file())
        elif p.exists():
            out.append(p)
        else:
            print(f"No existe: {r}", file=sys.stderr)
    return out


def _icono(estado: str) -> str:
    return {"COMPLETED": "OK  ", "COMPLETED_WITH_WARNINGS": "AVISO", "VALIDATION_FAILED": "ERROR",
            "TEMPLATE_NOT_IDENTIFIED": "NUEVO", "ERROR": "FALLA"}.get(estado, estado)


def cmd_procesar(a):
    from .exportar import guardar_resultados
    from .pipeline import procesar_lote

    resultados = procesar_lote(_entradas(a.rutas))
    for r in resultados:
        s = r.get("summary", {})
        t = r.get("template", {})
        print(f"[{_icono(r['status'])}] {r['file'][:58]:58} {t.get('templateId') or '-':24} "
              f"orden {s.get('numero_orden', '-')!s:>10}  total {s.get('total', '-')!s:>10}  "
              f"filas {s.get('filas', 0)}")
        rep = r.get("validationReport")
        if rep:
            for x in rep["executedRules"]:
                if x["passed"] is False:
                    fila = f" fila {x['row']}" if x.get("row") else ""
                    print(f"        {x['severity']:7} {x['rule']}{fila}: {x.get('detail') or x.get('difference') or ''}")
                elif a.detalle and x["passed"]:
                    print(f"        ok      {x['rule']}")
        if a.detalle and r.get("extractedData"):
            print(json.dumps(r["summary"], ensure_ascii=False, indent=1))
    if a.salida:
        rutas = guardar_resultados(resultados, Path(a.salida))
        print(f"\nResultados en {rutas['carpeta']} (JSON por documento + {Path(rutas['excel']).name})")


def cmd_calibrar(a):
    from .calibrar import borrador_plantilla, prompt_llm, volcado
    from .ingesta import ingerir
    from .pipeline import DIR_COLA, repo

    doc = ingerir(a.archivo)
    DIR_COLA.mkdir(exist_ok=True)
    base = DIR_COLA / Path(a.archivo).stem
    borr = borrador_plantilla(doc, a.id)
    ejemplo = next(iter(repo().todas()), None)
    if ejemplo:
        ejemplo = {k: v for k, v in ejemplo.items() if not k.startswith("_")}
    base.with_suffix(".tokens.json").write_text(json.dumps(volcado(doc), ensure_ascii=False, indent=1), encoding="utf-8")
    base.with_suffix(".borrador.json").write_text(json.dumps(borr, ensure_ascii=False, indent=2), encoding="utf-8")
    base.with_suffix(".prompt.md").write_text(prompt_llm(doc, borr, ejemplo), encoding="utf-8")
    print(f"Volcado, borrador y prompt en {base}.*")


def cmd_probar(a):
    from .calibrar import probar

    plantilla = json.loads(Path(a.plantilla).read_text(encoding="utf-8"))
    r = probar(plantilla, _entradas(a.rutas), promover=a.promover)
    print(json.dumps(r, ensure_ascii=False, indent=1))


def cmd_plantillas(a):
    from .pipeline import repo

    for p in repo().todas():
        print(f"{p['templateId']:28} v{p.get('version', 1)}  {p.get('description', '')}")


def cmd_web(a):
    import uvicorn

    print(f"Abre http://127.0.0.1:{a.puerto}")
    uvicorn.run("motor.web:app", host=a.host, port=a.puerto)


def main():
    ap = argparse.ArgumentParser(prog="motor")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("procesar")
    p.add_argument("rutas", nargs="+")
    p.add_argument("--salida", default=None)
    p.add_argument("--detalle", action="store_true")
    p.set_defaults(f=cmd_procesar)
    p = sub.add_parser("calibrar")
    p.add_argument("archivo")
    p.add_argument("--id", default=None)
    p.set_defaults(f=cmd_calibrar)
    p = sub.add_parser("probar")
    p.add_argument("plantilla")
    p.add_argument("rutas", nargs="+")
    p.add_argument("--promover", action="store_true")
    p.set_defaults(f=cmd_probar)
    p = sub.add_parser("plantillas")
    p.set_defaults(f=cmd_plantillas)
    p = sub.add_parser("web")
    p.add_argument("--puerto", type=int, default=int(os.environ.get("PORT", 8765)))
    p.add_argument("--host", default="127.0.0.1", help="0.0.0.0 para abrirlo a la red o en un servidor")
    p.set_defaults(f=cmd_web)
    a = ap.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
