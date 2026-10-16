"""Datos maestros (catalogo.json): corrige nombres conocidos leídos por OCR.

Solo se reemplaza cuando la lectura coincide con una forma conocida salvo espacios,
tildes, mayúsculas o puntuación, o cuando casi coincide (un carácter de diferencia en
nombres largos) y no hay otra forma igual de cercana. Si no, el valor queda como se leyó.
"""
from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

from rapidfuzz import fuzz
from rapidfuzz.distance import Levenshtein

RAIZ = Path(__file__).resolve().parent.parent


def _clave(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^A-Z0-9]", "", s.upper())


@lru_cache(maxsize=1)
def _cargar(mtime: float) -> dict:
    p = RAIZ / "catalogo.json"
    try:
        datos = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    listas = {k: [(v, _clave(v)) for v in vs] for k, vs in datos.get("listas", {}).items()}
    return {campo: listas.get(lista, []) for campo, lista in datos.get("campos", {}).items()}


def _catalogo() -> dict:
    try:
        mtime = (RAIZ / "catalogo.json").stat().st_mtime
    except OSError:
        return {}
    return _cargar(mtime)


def canonizar(campo: str, valor) -> str | None:
    """Forma conocida para `valor` en el campo, o None si no hay una segura."""
    if not isinstance(valor, str) or not valor.strip():
        return None
    opciones = _catalogo().get(campo)
    if not opciones:
        return None
    k = _clave(valor)
    if len(k) < 3:
        return None
    # mayúsculas/minúsculas distintas son otra forma de escribirlo: no se toca
    mayus = valor.upper() == valor
    opciones = [(v, kv) for v, kv in opciones if (v.upper() == v) == mayus]
    if not opciones:
        return None
    iguales = [v for v, kv in opciones if kv == k]
    if iguales:
        # varias formas con la misma clave ('GRUPO RPP SAC' / 'GRUPORPP SAC'): la más parecida
        return max(iguales, key=lambda v: fuzz.ratio(v, valor))
    if len(k) < 8:
        return None
    # casi iguales: como mucho 1 edición cada 12 caracteres, y sin empate
    tope = max(1, len(k) // 12)
    dist = sorted((Levenshtein.distance(k, kv), v) for v, kv in opciones)
    if dist[0][0] <= tope and (len(dist) == 1 or dist[1][0] > dist[0][0]):
        return dist[0][1]
    return None
