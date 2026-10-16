"""Normalizadores de valores: números, fechas, periodos, RUC, moneda, horarios."""
from __future__ import annotations

import calendar
import re
import unicodedata
from datetime import date

MESES = {
    "ENERO": 1, "FEBRERO": 2, "MARZO": 3, "ABRIL": 4, "MAYO": 5, "JUNIO": 6, "JULIO": 7,
    "AGOSTO": 8, "SETIEMBRE": 9, "SEPTIEMBRE": 9, "OCTUBRE": 10, "NOVIEMBRE": 11, "DICIEMBRE": 12,
    "ENE": 1, "FEB": 2, "MAR": 3, "ABR": 4, "MAY": 5, "JUN": 6, "JUL": 7, "AGO": 8,
    "SET": 9, "SEP": 9, "OCT": 10, "NOV": 11, "DIC": 12,
}
_MES_RE = "|".join(sorted(MESES, key=len, reverse=True))

# letras de día de semana usadas por las agencias (lunes = 0). Algunas usan "M" para
# martes y también para miércoles, por eso cada letra admite un conjunto de días.
DIAS_SEMANA = {
    "L": {0}, "LU": {0}, "M": {1, 2}, "MA": {1}, "W": {2}, "MI": {2}, "X": {2}, "J": {3}, "JU": {3},
    "V": {4}, "VI": {4}, "S": {5}, "SA": {5}, "D": {6}, "DO": {6},
}


def sin_tildes(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def _texto_fecha(s: str) -> str:
    """Mayúsculas sin tildes y con las confusiones del OCR dentro de palabras corregidas
    (MARZ0 -> MARZO) para reconocer nombres de mes."""
    t = sin_tildes(str(s)).upper()
    return re.sub(r"(?<=[A-Z])0(?=[A-Z0-9\s,./]|$)", "O", t)


def numero(s, decimal: str = "auto"):
    """'1.615,55' -> 1615.55 ; '12,470.27' -> 12470.27 ; '20,0' -> 20.0 ; '-' -> 0.0."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    txt = re.sub(r"US\$|S/\.?|\$|PEN|USD", "", str(s)).strip()
    if txt != str(s).strip() and not re.search(r"\d", txt):
        return 0.0  # solo el símbolo de moneda ("S/ -", "$ ·"): importe cero
    if re.fullmatch(r"[-–—·]+", txt):
        return 0.0
    # confusiones típicas del OCR dentro de un número: O->0, I/l->1
    txt = re.sub(r"(?<=[\d.,])[Oo]|[Oo](?=[\d.,])", "0", txt)
    txt = re.sub(r"(?<=[\d.,])[Il|]|[Il|](?=[\d.,]\d)", "1", txt)
    m = re.search(r"-?\d[\d.,]*", txt)
    if not m:
        return None
    n = m.group(0).rstrip(".,")
    neg = n.startswith("-")
    n = n.lstrip("-")
    if decimal == ",":
        n = n.replace(".", "").replace(",", ".")
    elif decimal == ".":
        n = n.replace(",", "")
    else:
        seps = [i for i, c in enumerate(n) if c in ".,"]
        if seps:
            ultimo = seps[-1]
            cola = len(n) - ultimo - 1
            if len(seps) == 1 and cola == 3:
                decimal_en = None          # 1,215 -> miles
            elif cola == 3 and len({n[i] for i in seps}) == 1:
                decimal_en = None          # 1.234.567
            else:
                decimal_en = ultimo        # 1.615,55 / 12,470.27 / 11.346.40 (OCR) / 138.5585
            n = "".join(c for i, c in enumerate(n) if c not in ".," or i == decimal_en).replace(",", ".")
    try:
        v = float(n)
    except ValueError:
        return None
    return -v if neg else v


def entero(s, decimal="auto"):
    v = numero(s, decimal)
    return None if v is None else int(round(v))


def porcentaje(s, decimal="auto"):
    v = numero(s, decimal)
    if v is None:
        return None
    return v / 100 if v > 1 else v


def _anio(a: str) -> int:
    a = int(a)
    return a + 2000 if a < 100 else a


def fecha(s) -> str | None:
    """Devuelve ISO 'YYYY-MM-DD' desde los formatos vistos en las órdenes."""
    if not s:
        return None
    t = _texto_fecha(s)
    m = re.search(r"(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})", t)
    if m:
        d, mth, a = int(m.group(1)), int(m.group(2)), _anio(m.group(3))
        return _iso(a, mth, d)
    m = re.search(rf"\b({_MES_RE})\.?\s*(\d{{1,2}})\s*,?\s*(\d{{4}})", t)  # Abril 03, 2025
    if m:
        return _iso(int(m.group(3)), MESES[m.group(1)], int(m.group(2)))
    m = re.search(rf"(\d{{1,2}})\s*(?:DE\s*)?({_MES_RE})\.?\s*(?:DE\s*|DEL\s*)?(\d{{4}})", t)  # 2 de Abril de 2025
    if m:
        return _iso(int(m.group(3)), MESES[m.group(2)], int(m.group(1)))
    return None


def _iso(a, m, d):
    try:
        return date(a, m, d).isoformat()
    except ValueError:
        return None


def mes(s) -> str | None:
    """'ABRIL/2025', 'abril 2025' -> '2025-04'."""
    if not s:
        return None
    t = _texto_fecha(s)
    m = re.search(rf"\b({_MES_RE})\.?\s*(?:/|-|DE|DEL)?\s*(\d{{4}})", t)
    if m:
        return f"{int(m.group(2)):04d}-{MESES[m.group(1)]:02d}"
    m = re.search(r"\b(\d{1,2})[/\-](\d{4})\b", t)
    if m:
        return f"{int(m.group(2)):04d}-{int(m.group(1)):02d}"
    return None


def periodo(s) -> dict | None:
    """'07 al 23 Abril 2025' | '14/04/25 al 29/04/25' | 'del 01/01/2025 al 30/04/2025' | 'abril 2025'."""
    if not s:
        return None
    t = _texto_fecha(s)
    fechas = re.findall(r"\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}", t)
    if len(fechas) >= 2:
        return {"inicio": fecha(fechas[0]), "fin": fecha(fechas[1])}
    m = re.search(rf"(\d{{1,2}})\s*AL\s*(\d{{1,2}})\s*(?:DE\s+)?({_MES_RE})\.?\s*(?:DE\s+|DEL\s+)?(\d{{4}})", t)
    if m:
        a, mm = int(m.group(4)), MESES[m.group(3)]
        return {"inicio": _iso(a, mm, int(m.group(1))), "fin": _iso(a, mm, int(m.group(2)))}
    ms = mes(t)
    if ms:
        a, mm = map(int, ms.split("-"))
        return {"inicio": _iso(a, mm, 1), "fin": _iso(a, mm, calendar.monthrange(a, mm)[1])}
    return None


def ruc(s) -> str | None:
    if not s:
        return None
    m = re.search(r"(?<!\d)(10|15|16|17|20)\d{9}(?!\d)", re.sub(r"(?<=\d)[ .](?=\d)", "", str(s)))
    return m.group(0) if m else None


def ruc_valido(r: str | None) -> bool:
    """Dígito verificador SUNAT (módulo 11)."""
    if not r or not re.fullmatch(r"\d{11}", r):
        return False
    pesos = [5, 4, 3, 2, 7, 6, 5, 4, 3, 2]
    s = sum(int(d) * p for d, p in zip(r[:10], pesos))
    dv = 11 - s % 11
    dv = {10: 0, 11: 1}.get(dv, dv)
    return dv == int(r[10])


def moneda(s) -> str | None:
    if not s:
        return None
    t = sin_tildes(str(s)).upper()
    if "US$" in t or "USD" in t or "DOLAR" in t or re.search(r"(^|\s)\$", t):
        return "USD"
    if "S/" in t or "SOL" in t or "PEN" in t:
        return "PEN"
    return None


def horario(s) -> str | None:
    """'06:00-23:59', '16:00 19:00', 'ROTATIVO (06:00 - 09:00)' -> '06:00-23:59'."""
    if not s:
        return None
    horas = re.findall(r"(\d{1,2}):(\d{2})", str(s))
    if not horas:
        return None
    fmt = [f"{int(h):02d}:{m}" for h, m in horas[:2]]
    return "-".join(fmt)


def duracion(s, decimal="auto"):
    """'40 sec', '20,0', '15"', '15 Seg' -> segundos (float)."""
    if s is None or not re.search(r"\d", str(s)):
        return None
    return numero(s, decimal)


def texto(s) -> str | None:
    if s is None:
        return None
    t = re.sub(r"[ \t]+", " ", str(s)).strip(" :")
    return t or None


NORMALIZADORES = {
    "text": lambda v, o: texto(v),
    "number": lambda v, o: numero(v, o.get("decimalSeparator", "auto")),
    "money": lambda v, o: numero(v, o.get("decimalSeparator", "auto")),
    "int": lambda v, o: entero(v, o.get("decimalSeparator", "auto")),
    "percent": lambda v, o: porcentaje(v, o.get("decimalSeparator", "auto")),
    "date": lambda v, o: fecha(v),
    "month": lambda v, o: mes(v),
    "period": lambda v, o: periodo(v),
    "ruc": lambda v, o: ruc(v),
    "currency": lambda v, o: moneda(v),
    "schedule": lambda v, o: horario(v),
    "duration": lambda v, o: duracion(v, o.get("decimalSeparator", "auto")),
}


def normalizar(valor, tipo: str, opciones: dict | None = None):
    f = NORMALIZADORES.get(tipo or "text", NORMALIZADORES["text"])
    return f(valor, opciones or {})
