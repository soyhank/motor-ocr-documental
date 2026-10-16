"""Regresión: los 13 PDF de muestra deben leerse con los valores verificados a mano."""
import json
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from motor.pipeline import procesar_lote  # noqa: E402

ESPERADO = json.loads((RAIZ / "tests" / "esperado.json").read_text(encoding="utf-8"))
_RES = None


def resultados():
    global _RES
    if _RES is None:
        _RES = {r["file"]: r for r in procesar_lote(sorted((RAIZ / "muestras").iterdir()))}
    return _RES


@pytest.mark.parametrize("archivo", sorted(ESPERADO))
def test_muestra(archivo):
    esp = ESPERADO[archivo]
    r = resultados()[archivo]
    assert r["template"]["templateId"] == esp["template"]
    fallas = {x["rule"] for x in r["validationReport"]["executedRules"] if x["passed"] is False}
    assert r["status"] == esp["status"], fallas
    s = r["summary"]
    for k, v in esp.items():
        if k in ("template", "status", "dias", "falla"):
            continue
        if isinstance(v, float):
            assert s.get(k) == pytest.approx(v, abs=0.005), k
        else:
            assert s.get(k) == v, k
    filas = r["extractedData"]["detail"]
    for fila, dias in esp.get("dias", {}).items():
        prog = {p["dia"]: p["avisos"] for p in filas[int(fila) - 1]["programacion"]}
        for d, n in dias.items():
            assert prog.get(int(d)) == n, f"fila {fila} día {d}"
    assert fallas == set(esp.get("falla", []))


def test_material_audio_cruzado():
    r = resultados()["Orden_Nº_030817-2025_CLARO_LAZONA_NN_JUNTOSMASCHEVERERV2_MAR.PDF"]
    mats = r.get("materials", [])
    assert mats and all(m["match"] for m in mats)


def test_trazabilidad_bbox():
    r = resultados()["461770_OXIGENO-NN.pdf"]
    total = r["extractedData"]["totals"]["total"]
    assert total["source"]["rawText"] == "38,831.37"
    assert 0 <= total["source"]["bbox"]["x0"] < total["source"]["bbox"]["x1"] <= 1000
