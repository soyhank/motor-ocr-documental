"""Repositorio de plantillas JSON (recarga en caliente) y etapa 5: identificación por puntuación."""
from __future__ import annotations

import json
import re
from pathlib import Path

from rapidfuzz import fuzz

from .ingesta import Documento, agrupar_lineas
from .normalizar import sin_tildes

RAIZ = Path(__file__).resolve().parent.parent
DIR_PLANTILLAS = RAIZ / "plantillas"


class Repositorio:
    """Lee todas las plantillas de la carpeta. Si un archivo cambia se recarga sin reiniciar.
    Cuando hay varias versiones de un mismo templateId gana la mayor que esté activa."""

    def __init__(self, carpeta: Path = DIR_PLANTILLAS):
        self.carpeta = Path(carpeta)
        self._firma = None
        self._plantillas: dict[str, dict] = {}

    def _firma_actual(self):
        return tuple(sorted((p.name, p.stat().st_mtime) for p in self.carpeta.glob("*.json")))

    def todas(self) -> list[dict]:
        firma = self._firma_actual()
        if firma != self._firma:
            cargadas: dict[str, dict] = {}
            for p in sorted(self.carpeta.glob("*.json")):
                try:
                    t = json.loads(p.read_text(encoding="utf-8"))
                except json.JSONDecodeError as e:
                    print(f"[plantillas] {p.name} inválida: {e}")
                    continue
                if t.get("status", "active") != "active":
                    continue
                t["_archivo"] = p.name
                previa = cargadas.get(t["templateId"])
                if not previa or t.get("version", 1) > previa.get("version", 1):
                    cargadas[t["templateId"]] = t
            self._plantillas = cargadas
            self._firma = firma
        return list(self._plantillas.values())

    def recargar(self):
        self._firma = None

    def obtener(self, template_id: str) -> dict | None:
        self.todas()
        return self._plantillas.get(template_id)


def _texto_region(doc: Documento, region: dict | None) -> str:
    if not region:
        return doc.texto()
    toks = [t for t in doc.tokens
            if region.get("page", t.page) == t.page
            and region.get("x0", -1) <= t.cx <= region.get("x1", 1001)
            and region.get("y0", -1) <= t.cy <= region.get("y1", 1001)]
    return "\n".join(l.texto for l in agrupar_lineas(toks))


def puntuar(doc: Documento, plantilla: dict) -> tuple[float, list[dict]]:
    reglas = plantilla.get("identification", {}).get("rules", [])
    total = 0.0
    detalle = []
    cache: dict = {}
    for r in reglas:
        clave = json.dumps(r.get("region"), sort_keys=True)
        if clave not in cache:
            cache[clave] = sin_tildes(_texto_region(doc, r.get("region")))
        texto = cache[clave]
        tipo = r.get("type", "exact_text")
        valor = sin_tildes(r["value"])
        # el OCR a veces pega o parte palabras: se compara también sin espacios
        compacto = re.sub(r"\s+", "", texto).upper()
        valor_c = re.sub(r"\s+", "", valor).upper()
        ok = False
        if tipo == "exact_text":
            ok = valor.upper() in texto.upper() or valor_c in compacto
        elif tipo == "regex":
            ok = (re.search(valor, texto, re.IGNORECASE) is not None
                  or re.search(re.sub(r"\\s[*+?]?|\s+", r"\\s*", valor), texto, re.IGNORECASE) is not None)
        elif tipo == "fuzzy_text":
            ok = max(fuzz.partial_ratio(valor.upper(), texto.upper()),
                     fuzz.partial_ratio(valor_c, compacto)) >= r.get("minRatio", 85)
        elif tipo == "not_text":  # penaliza si aparece
            ok = valor.upper() not in texto.upper() and valor_c not in compacto
        peso = r.get("weight", 10)
        if ok:
            total += peso
        detalle.append({"type": tipo, "value": r["value"], "weight": peso, "matched": ok})
    return total, detalle


def identificar(doc: Documento, repo: Repositorio) -> dict:
    candidatos = []
    for p in repo.todas():
        score, det = puntuar(doc, p)
        candidatos.append({"templateId": p["templateId"], "version": p.get("version", 1), "score": score,
                           "threshold": p.get("identification", {}).get("minScoreThreshold", 80), "rules": det})
    candidatos.sort(key=lambda c: c["score"], reverse=True)
    ganador = next((c for c in candidatos if c["score"] >= c["threshold"]), None)
    return {"winner": ganador, "candidates": candidatos}
