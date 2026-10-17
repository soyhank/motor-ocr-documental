"""Etapa 6: extracción por anclas relativas y proyección de tablas.

Todo trabaja sobre tokens con coordenadas normalizadas [0..1000].
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .catalogo import canonizar
from .ingesta import Documento, Linea, Pagina, Token, agrupar_lineas
from .normalizar import DIAS_SEMANA, normalizar, sin_tildes


# ------------------------------------------------------------------ anclas

@dataclass
class Ancla:
    tokens: list[Token]
    linea: Linea
    idx_fin: int  # índice del último token del ancla dentro de la línea
    exacta: bool = True  # False si se aceptó por parecido (OCR)

    @property
    def x0(self):
        return min(t.x0 for t in self.tokens)

    @property
    def x1(self):
        return max(t.x1 for t in self.tokens)

    @property
    def y0(self):
        return min(t.y0 for t in self.tokens)

    @property
    def y1(self):
        return max(t.y1 for t in self.tokens)

    @property
    def page(self):
        return self.tokens[0].page


def _variantes_ocr(s: str) -> list[str]:
    """Variantes de un texto con las confusiones típicas del OCR deshechas
    (PERI0D0 -> PERIODO, Unitar1o -> Unitario/Unitarlo, 5pots -> Spots)."""
    base = re.sub(r"(?<=[A-Za-z])0|0(?=[A-Za-z])", "O", s)
    base = re.sub(r"(?<=[A-Za-z])5(?=[A-Za-z])|(?<![\d.,])5(?=[a-z]{2})", "S", base)
    base = re.sub(r"(?<=[A-Za-z])8(?=[A-Za-z])", "B", base)
    out = [base]
    if re.search(r"(?<=[A-Za-z])1(?=[A-Za-z.:])", base):
        for letra in ("i", "l", "I"):
            out.append(re.sub(r"(?<=[A-Za-z])1(?=[A-Za-z.:])", letra, base))
    return out


def _compilar(patron: str, sensible: bool):
    """Los espacios del patrón se vuelven opcionales: el OCR a veces pega palabras
    ("Tipodecompra:") y el PDF a veces las parte."""
    p = re.sub(r"(?<!\\) +", r"\\s*", sin_tildes(patron))
    return re.compile(p, 0 if sensible else re.IGNORECASE)


def _en_region(t_or_a, region: dict | None) -> bool:
    if not region:
        return True
    if "page" in region and region["page"] != t_or_a.page:
        return False
    cx = (t_or_a.x0 + t_or_a.x1) / 2
    cy = (t_or_a.y0 + t_or_a.y1) / 2
    return (region.get("x0", -1) <= cx <= region.get("x1", 1001)
            and region.get("y0", -1) <= cy <= region.get("y1", 1001))


_RX_META = re.compile(r"[*+(){}|]")


def _literal_de(patron: str) -> str | None:
    """Texto plano equivalente a un patrón casi literal ("PERIODO:", "Divisi[oó]n:");
    None si el patrón usa construcciones de regex de verdad."""
    p = sin_tildes(patron)
    p = re.sub(r"\\s[*+]", " ", p)
    p = re.sub(r"\[([^\]]+)\]\??", lambda m: m.group(1)[0], p)
    p = p.replace("\\.", ".").replace("\\(", "(").replace("\\)", ")").replace("\\b", "")
    p = re.sub(r"\?$", "", p).lstrip("^").rstrip("$")
    if _RX_META.search(p) or "\\" in p:
        return None
    p = re.sub(r"\s+", " ", p).strip()
    return p if len(p.replace(" ", "")) >= 6 else None


def _parecido(texto: str, literal: str) -> bool:
    """Compara sin espacios ni tildes y admite 1 o 2 caracteres mal leídos (PERlODOO: ~ PERIODO:)."""
    from rapidfuzz import fuzz

    a = re.sub(r"\s+", "", sin_tildes(texto)).upper()
    b = re.sub(r"\s+", "", literal).upper()
    if abs(len(a) - len(b)) > 2 or not a or not b:
        return False
    # mismo inicio y mismo final: así la etiqueta no se "come" el valor ("PRODUCTO:M")
    # ni se confunde con otra parecida ("MEDIO:" frente a "MEDIOS")
    return fuzz.ratio(a, b) >= 80 and a[0] == b[0] and a[-1] == b[-1]


def buscar_anclas(lineas: list[Linea], patron: str, sensible: bool = False, region: dict | None = None,
                  usados: set | None = None, max_tokens: int = 6) -> list[Ancla]:
    """Busca secuencias de tokens consecutivos cuyo texto unido calce completo con el patrón.
    Por cada posición inicial se queda con la secuencia más larga que calce. Si el patrón es
    casi literal, también se acepta una lectura parecida (errores típicos de OCR)."""
    rx = _compilar(patron, sensible)
    literal = _literal_de(patron)
    # en escaneados los dos puntos finales de la etiqueta a veces no se leen
    sin_dp = re.sub(r"(\\s\*|\s)*:\??$", "", patron)
    rx_sin_dp = _compilar(sin_dp, sensible) if sin_dp != patron and sin_dp else None
    res = []
    for l in lineas:
        toks = l.tokens
        i = 0
        while i < len(toks):
            mejor = None
            if usados and id(toks[i]) in usados:
                i += 1
                continue
            exacta = True
            for j in range(i, min(len(toks), i + max_tokens)):
                if usados and id(toks[j]) in usados:
                    break
                unido = sin_tildes(" ".join(t.text for t in toks[i:j + 1]))
                if rx.fullmatch(unido) or any(rx.fullmatch(v) for v in _variantes_ocr(unido))                         or (l.page_ocr and j > i and rx.fullmatch(unido.replace(" ", ""))):
                    mejor, exacta = j, True
                elif literal is not None and not sensible and l.page_ocr and _parecido(unido, literal):
                    if mejor is None:
                        mejor, exacta = j, False
                elif (j == i and l.page_ocr and mejor is None and literal and literal.endswith(":")
                      and " " not in literal and len(unido) > len(literal)
                      and unido.upper().endswith(literal.upper())):
                    # etiqueta pegada a la palabra anterior ('PREPACIUDAD:'): texto encimado o
                    # renglones muy juntos en el escaneo
                    mejor, exacta = j, False
                elif (rx_sin_dp is not None and l.page_ocr and mejor is None and rx_sin_dp.fullmatch(unido)
                      and (j + 1 >= len(toks) or not toks[j + 1].text.startswith(":"))
                      and (i == 0 or toks[i].x0 - toks[i - 1].x1 > 1.5 * max(toks[i].h, 1))):
                    mejor, exacta = j, False
            if mejor is not None:
                a = Ancla(toks[i:mejor + 1], l, mejor, exacta)
                if _en_region(a, region):
                    res.append(a)
                i = mejor + 1
            else:
                i += 1
    return res


def _texto_tokens(toks: list[Token]) -> str:
    return " ".join(t.text for t in toks)


def _valor_derecha(a: Ancla, cfg: dict) -> list[Token]:
    max_dist = cfg.get("maxDistance", 400)
    max_gap = cfg.get("maxGap", 80)
    # por defecto el valor termina al encontrar otra etiqueta ("MEDIO:", "RUC:")
    patron_stop = cfg.get("stopPattern", r".+:")
    stop = _compilar(patron_stop, False) if patron_stop else None
    out: list[Token] = []
    for t in a.linea.tokens[a.idx_fin + 1:]:
        if t.x0 < a.x1 - 2:
            continue
        if t.x0 > a.x1 + max_dist:
            break
        if out and t.x0 - out[-1].x1 > max_gap:
            break
        if out and stop and stop.fullmatch(sin_tildes(t.text)):
            break
        out.append(t)
    return out


def _valor_abajo(a: Ancla, lineas: list[Linea], cfg: dict) -> list[Token]:
    max_dist = cfg.get("maxDistance", 60)
    holgura = cfg.get("xSlack", 25)
    x0, x1 = a.x0 - holgura, a.x1 + cfg.get("xExtra", holgura)
    for l in lineas:
        if l.page != a.page or l.cy <= a.linea.cy + 1:
            continue
        if l.cy - a.y1 > max_dist:
            break
        toks = [t for t in l.tokens if t.x1 > x0 and t.x0 < x1]
        if toks:
            return toks
    return []


def _valor_caja(a: Ancla, lineas: list[Linea], caja: dict) -> list[Token]:
    X0, X1 = a.x0 + caja.get("dx0", 0), a.x0 + caja.get("dx1", 300)
    Y0, Y1 = a.y0 + caja.get("dy0", 0), a.y0 + caja.get("dy1", 30)
    out = []
    for l in lineas:
        if l.page != a.page:
            continue
        for t in l.tokens:
            if t in a.tokens:
                continue
            if X0 <= t.cx <= X1 and Y0 <= t.cy <= Y1:
                out.append(t)
    return out


def _continuacion(a: Ancla, primeras: list[Token], lineas: list[Linea], cfg: dict) -> list[Token]:
    """Renglones extra debajo del valor (direcciones partidas en dos líneas)."""
    n = cfg.get("continuationLines", 0)
    if not n or not primeras:
        return primeras
    x0 = a.x0 - 5
    x1 = a.x1 + cfg.get("maxDistance", 400)
    etiqueta = re.compile(r".*:\s*$")
    out = list(primeras)
    ultimo_y = max(t.y1 for t in primeras)
    for l in lineas:
        if n <= 0:
            break
        if l.page != a.page or l.cy <= a.linea.cy + 1:
            continue
        if l.y0 - ultimo_y > 25:
            break
        toks = [t for t in l.tokens if t.x0 >= x0 and t.x1 <= x1 + 40]
        if not toks or etiqueta.match(toks[0].text):
            break
        out.extend(toks)
        ultimo_y = max(t.y1 for t in toks)
        n -= 1
    return out


def _campo_region(campo: dict, lineas: list[Linea], opciones: dict) -> dict | None:
    """Campo sin etiqueta: texto dentro de una caja fija (p. ej. razón social del membrete)."""
    reg = campo["region"]

    def _seleccion(r):
        out = []
        for l in lineas:
            if l.page != r.get("page", 1):
                continue
            toks = [t for t in l.tokens if _en_region(t, r)]
            if toks:
                out.append(toks)
        return out

    sel = _seleccion(reg)
    if not sel and lineas and lineas[0].page_ocr and not campo.get("allLines"):
        # escaneo o foto algo corridos respecto del original: caja ampliada y el renglón
        # más cercano al centro de la caja original
        m = 15
        amplia = dict(reg, x0=reg.get("x0", 0) - m, x1=reg.get("x1", 1000) + m,
                      y0=reg.get("y0", 0) - m, y1=reg.get("y1", 1000) + m)
        centro = (reg.get("y0", 0) + reg.get("y1", 1000)) / 2
        sel = sorted(_seleccion(amplia), key=lambda ts: abs(sum(t.cy for t in ts) / len(ts) - centro))[:1]
        if sel and campo.get("lineIndex", 0):
            sel = []
    if not sel:
        return None
    if campo.get("allLines"):
        toks = [t for r in sel for t in r]
        crudo = "\n".join(_texto_tokens(r) for r in sel)
    else:
        i = campo.get("lineIndex", 0)
        if i >= len(sel):
            return None
        toks = sel[i]
        crudo = _texto_tokens(toks)
    if campo.get("valuePattern"):
        m = re.search(campo["valuePattern"], crudo, re.IGNORECASE | re.DOTALL)
        if not m:
            return None
        crudo = m.group(1) if m.groups() else m.group(0)
    valor = normalizar(crudo.strip(), campo.get("type", "text"), opciones)
    if valor is None:
        return None
    return {"value": valor, "tag": campo.get("tag", campo["name"].upper()), "source": _fuente(toks, crudo)}


def _campo_respaldo(campo: dict, lineas: list[Linea], opciones: dict) -> dict | None:
    """Último recurso cuando la etiqueta no se reconoció (OCR): se busca el valor por su
    propia forma en cada renglón, p. ej. "07 al 23 Abril 2025"."""
    rx = re.compile(campo["fallbackPattern"], re.IGNORECASE)
    for l in lineas:
        m = rx.search(sin_tildes(l.texto))
        if not m:
            continue
        crudo = (m.group(1) if m.groups() else m.group(0)).strip()
        valor = normalizar(crudo, campo.get("type", "text"), opciones)
        if valor is None:
            continue
        # tokens que cubren el trozo encontrado (para la trazabilidad)
        pos, toks = 0, []
        texto = sin_tildes(l.texto)
        for t in l.tokens:
            fin = pos + len(t.text)
            if fin > m.start() and pos < m.end():
                toks.append(t)
            pos = fin + 1
        return {"value": valor, "tag": campo.get("tag", campo["name"].upper()), "source": _fuente(toks or l.tokens, crudo),
                "fallback": True}
    return None


def extraer_campo(campo: dict, lineas: list[Linea], opciones: dict, limite_y: tuple | None = None) -> dict | None:
    res = _extraer_campo_ancla(campo, lineas, opciones, limite_y)
    if res is None and campo.get("fallbackPattern") and lineas and lineas[0].page_ocr:
        res = _campo_respaldo(campo, lineas, opciones)
    return res


def _sin_dos_puntos_falsos(toks: list[Token], ocr: bool) -> list[Token]:
    """En escaneados, los dos puntos que siguen a la etiqueta a veces se leen como otro
    signo o una letra suelta ('à', '•.', 'E' de baja confianza). Si el valor empieza con
    un token así, se descarta."""
    if not ocr or len(toks) < 2:
        return toks
    t = toks[0]
    if len(t.text) <= 2 and (not re.fullmatch(r"[A-Za-z0-9]+", t.text) or (len(t.text) == 1 and t.conf < 0.8))             and not re.fullmatch(r"[$#(%+-]|S/|N[°º]", t.text):
        return toks[1:]
    return toks


def _extraer_campo_ancla(campo: dict, lineas: list[Linea], opciones: dict, limite_y: tuple | None = None) -> dict | None:
    if "region" in campo and "anchor" not in campo:
        return _campo_region(campo, lineas, opciones)
    anc = campo.get("anchor", {})
    region = dict(anc.get("region") or {})
    if limite_y:
        region.setdefault("y0", limite_y[0])
        region.setdefault("y1", limite_y[1])
        region.setdefault("page", limite_y[2])
    candidatos = buscar_anclas(lineas, anc["pattern"], anc.get("caseSensitive", False), region or None)
    if not candidatos:
        return None
    # las coincidencias exactas van primero; las aproximadas (OCR) solo si no hay otra
    candidatos.sort(key=lambda a: not a.exacta)
    nth = anc.get("nth", 0)
    candidatos = candidatos[nth:] if nth >= 0 else candidatos[nth:]
    direccion = anc.get("searchDirection", "RIGHT").upper()
    rx_valor = re.compile(campo["valuePattern"], re.IGNORECASE | re.DOTALL) if campo.get("valuePattern") else None
    for a in candidatos:
        toks: list[Token] = []
        if direccion in ("RIGHT", "RIGHT_OR_DOWN"):
            toks = _sin_dos_puntos_falsos(_valor_derecha(a, anc), a.linea.page_ocr)
            toks = _continuacion(a, toks, lineas, anc)
        if not toks and direccion in ("DOWN", "RIGHT_OR_DOWN"):
            toks = _valor_abajo(a, lineas, anc)
        if direccion == "RELATIVE_BOX":
            toks = _valor_caja(a, lineas, anc.get("box", {}))
        if direccion == "SELF":
            toks = a.tokens
        if direccion == "LINE":  # desde el ancla hasta el final del renglón
            toks = [t for t in a.linea.tokens if t.x0 >= a.x0 - 1]
        crudo = _texto_tokens(toks)
        if rx_valor:
            m = rx_valor.search(crudo)
            if not m:
                continue
            crudo = m.group(1) if m.groups() else m.group(0)
        crudo = crudo.strip(" :")
        if not crudo and campo.get("type") not in ("number", "money"):
            continue
        valor = normalizar(crudo, campo.get("type", "text"), opciones)
        if valor is None and campo.get("type") not in (None, "text"):
            continue
        fuente = toks or a.tokens
        return {
            "value": valor,
            "tag": campo.get("tag", campo["name"].upper()),
            "source": _fuente(fuente, crudo),
            "_ancla": _texto_tokens(a.tokens),  # etiqueta exacta (para aprender plantillas)
        }
    return None


def _fuente(toks: list[Token], crudo: str) -> dict:
    return {
        "page": toks[0].page,
        "rawText": crudo,
        "bbox": {
            "x0": round(min(t.x0 for t in toks), 1), "y0": round(min(t.y0 for t in toks), 1),
            "x1": round(max(t.x1 for t in toks), 1), "y1": round(max(t.y1 for t in toks), 1),
        },
        # confianza del reconocedor: la del token más dudoso (1.0 en PDF digital)
        "confidence": round(min(t.conf for t in toks), 3),
    }


def extraer_campos(campos: list[dict], lineas: list[Linea], opciones: dict, limite_y=None) -> dict:
    out = {}
    for c in campos:
        v = extraer_campo(c, lineas, opciones, limite_y)
        if v is None and c.get("default") is not None:
            v = {"value": c["default"], "tag": c.get("tag", c["name"].upper()), "source": None, "default": True}
        if v is not None and v.get("source") is not None and lineas and lineas[0].page_ocr                 and (c.get("type") or "text") == "text":
            canon = canonizar(c["name"], v.get("value"), c.get("knownValues"))
            if canon is not None and canon != v["value"]:
                v["ocrValue"] = v["value"]
                v["value"] = canon
        if v is not None:
            out[c["name"]] = v
    return out


# ------------------------------------------------------------------ tablas

@dataclass
class Columna:
    nombre: str
    tipo: str
    x0: float
    x1: float
    cx: float
    dia: int | None = None
    letra: str | None = None


def _unir_cajas(toks):
    return min(t.x0 for t in toks), max(t.x1 for t in toks)


def _bordes(pag: Pagina, y0: float, y1: float) -> list[float]:
    """Reglas verticales que bajan desde la cabecera hacia el cuerpo de la tabla.
    Se descartan los recuadros pequeños que algunos generadores dibujan alrededor del texto."""
    sonda = y1 + 8
    xs = sorted(x for x, a, b in pag.verticales if a <= sonda <= b and b - a > 12)
    out: list[float] = []
    for x in xs:
        if not out or x - out[-1] > 1.5:
            out.append(x)
    return out


def detectar_columnas(pag: Pagina, lineas: list[Linea], cfg: dict) -> tuple[list[Columna], float, float] | None:
    """Ubica la cabecera de la tabla y devuelve columnas con sus límites en x, y el
    rango vertical de la cabecera."""
    defs = [h if isinstance(h, dict) else {"name": h.lower(), "pattern": re.escape(h)} for h in cfg["headerAnchors"]]
    lp = [l for l in lineas if l.page == pag.numero]
    primero = buscar_anclas(lp, defs[0]["pattern"], defs[0].get("caseSensitive", False), defs[0].get("region"))
    if not primero:
        return None
    a0 = primero[0]
    banda = cfg.get("headerBand", 30)
    lb = [l for l in lp if abs(l.cy - a0.linea.cy) <= banda]
    usados: set = set()
    encontrados = []
    for d in defs:
        an = buscar_anclas(lb, d["pattern"], d.get("caseSensitive", False), None, usados)
        if not an:
            if d.get("required", True) and d is defs[0]:
                return None
            continue
        a = an[0]
        for t in a.tokens:
            usados.add(id(t))
        encontrados.append((d, a))

    # columnas de días: números 1..31 de izquierda a derecha. Se admite que falten o se
    # peguen algunos (OCR): se ajusta el paso entre columnas y se completan los que faltan.
    dias: list[Token] = []
    extra_dias: list[tuple[int, float, float, float]] = []  # (dia, cx, y0, y1) inferidos
    if cfg.get("dayColumns", True):
        nums: list[Token] = []
        for l in lb:
            for t in l.tokens:
                if id(t) in usados or not re.fullmatch(r"\d{1,12}", t.text):
                    continue
                if len(t.text) <= 2 and 1 <= int(t.text) <= 31:
                    nums.append(t)
                    continue
                desglose = _desglosar_dias(t.text)  # "1011" -> 10, 11 (OCR pegó celdas vecinas)
                if desglose:
                    ancho = (t.x1 - t.x0) / len(t.text)
                    pos = 0
                    for d in desglose:
                        n = len(str(d))
                        nums.append(Token(str(d), t.x0 + ancho * pos, t.y0, t.x0 + ancho * (pos + n), t.y1, t.page))
                        pos += n
        nums.sort(key=lambda t: t.x0)
        mejor: list[Token] = []
        for i, t in enumerate(nums):
            seq = [t]
            for u in nums[i + 1:]:
                if int(u.text) > int(seq[-1].text) and u.x0 > seq[-1].x0:
                    # el salto en x debe ser proporcional al salto de número
                    if len(seq) >= 2:
                        paso = (seq[-1].cx - seq[0].cx) / (int(seq[-1].text) - int(seq[0].text))
                        esperado = seq[-1].cx + paso * (int(u.text) - int(seq[-1].text))
                        if abs(u.cx - esperado) > paso * 0.45:
                            continue
                    seq.append(u)
            if len(seq) > len(mejor):
                mejor = seq
        if len(mejor) >= 7:
            dias = mejor
            paso = (dias[-1].cx - dias[0].cx) / max(int(dias[-1].text) - int(dias[0].text), 1)
            presentes = {int(t.text) for t in dias}
            ultimo = cfg.get("maxDays", 31)
            y0d, y1d = min(t.y0 for t in dias), max(t.y1 for t in dias)
            for d in range(1, ultimo + 1):
                if d in presentes:
                    continue
                cx = dias[0].cx + paso * (d - int(dias[0].text))
                if 0 <= cx <= 1000:
                    extra_dias.append((d, cx, y0d, y1d))
    y_ini = min([a.y0 for _, a in encontrados] + [t.y0 for t in dias])
    y_fin = max([a.y1 for _, a in encontrados] + [t.y1 for t in dias])

    # letras de día de semana alineadas sobre cada número
    letras = {}
    if dias:
        cand = [t for l in lp for t in l.tokens
                if y_ini - 25 <= t.cy <= y_fin and sin_tildes(t.text).upper() in DIAS_SEMANA and id(t) not in usados
                and t not in dias]
        for d in dias:
            cerca = [t for t in cand if abs(t.cx - d.cx) < 6 and t.cy < d.cy]
            if cerca:
                letras[int(d.text)] = sin_tildes(min(cerca, key=lambda t: abs(t.cx - d.cx)).text).upper()

    cols: list[Columna] = []
    for d, a in encontrados:
        x0, x1 = a.x0, a.x1
        cols.append(Columna(d["name"], d.get("type", "text"), x0, x1, (x0 + x1) / 2))
    if not dias and cfg.get("dayColumns", True) and cfg.get("useRulings", True) and pag.origen == "ocr":
        celdas = _grilla_sin_numeros(pag, y_ini, y_fin, cols, cfg)
        if celdas:
            cand = [t for l in lp for t in l.tokens
                    if y_ini - 25 <= t.cy <= y_fin + 5 and sin_tildes(t.text).upper() in DIAS_SEMANA and id(t) not in usados]
            for d, (x0, x1) in celdas.items():
                cx = (x0 + x1) / 2
                cerca = [t for t in cand if abs(t.cx - cx) < (x1 - x0) * 0.45]
                letra = sin_tildes(min(cerca, key=lambda t: abs(t.cx - cx)).text).upper() if cerca else None
                cols.append(Columna(f"d{d}", "int", x0, x1, cx, dia=d, letra=letra))
    if dias:
        paso = (dias[-1].cx - dias[0].cx) / max(int(dias[-1].text) - int(dias[0].text), 1)
        izq = max([c.x1 for c in cols if c.dia is None and c.cx < dias[0].cx] + [0])
        der = min([c.x0 for c in cols if c.dia is None and c.cx > dias[-1].cx] + [1000])
        celdas = _celdas_por_reglas(pag, dias, izq, der, paso, cfg) if cfg.get("useRulings", True) else None
        if celdas:
            # las reglas verticales de la grilla definen cada columna con exactitud
            for d, (x0, x1) in celdas.items():
                cols.append(Columna(f"d{d}", "int", x0, x1, (x0 + x1) / 2, dia=d, letra=letras.get(d)))
        else:
            for t in dias:
                cols.append(Columna(f"d{int(t.text)}", "int", t.cx - paso / 2, t.cx + paso / 2, t.cx,
                                    dia=int(t.text), letra=letras.get(int(t.text))))
            # columnas inferidas solo si caen dentro de la zona de la grilla
            for d, cx, _, _ in extra_dias:
                if izq < cx - paso / 2 and cx + paso / 2 < der:
                    cols.append(Columna(f"d{d}", "int", cx - paso / 2, cx + paso / 2, cx, dia=d, letra=letras.get(d)))
    cols.sort(key=lambda c: c.cx)

    # límites de columnas con nombre: bordes verticales del PDF si existen, si no puntos medios
    bordes = _bordes(pag, y_ini, y_fin) if cfg.get("useRulings", True) else []
    for i, c in enumerate(cols):
        if c.dia is not None:
            continue
        izq_lim = cols[i - 1].x1 if i > 0 else 0
        der_lim = cols[i + 1].x0 if i + 1 < len(cols) else 1000
        bi = [x for x in bordes if izq_lim - 1 <= x <= c.x0 + 1]
        bd = [x for x in bordes if c.x1 - 1 <= x <= der_lim + 1]
        nuevo_x0 = bi[-1] if bi else (izq_lim + c.x0) / 2 if i > 0 else 0
        nuevo_x1 = bd[0] if bd else (c.x1 + der_lim) / 2 if i + 1 < len(cols) else 1000
        c.x0, c.x1 = nuevo_x0, nuevo_x1
    for d, _ in encontrados:  # límites forzados desde la plantilla
        c = next(c for c in cols if c.nombre == d["name"])
        if "x0" in d:
            c.x0 = d["x0"]
        if "x1" in d:
            c.x1 = d["x1"]
    return cols, y_ini, y_fin


def _desglosar_dias(texto: str) -> list[int] | None:
    """'1011' -> [10, 11]; '9101112' -> [9, 10, 11, 12]; números de día consecutivos pegados."""
    for inicial in (1, 2):
        out, pos = [], 0
        while pos < len(texto):
            n = inicial if not out else (1 if out[-1] < 9 else 2)
            trozo = texto[pos:pos + n]
            if not trozo.isdigit() or (out and int(trozo) != out[-1] + 1) or not (1 <= int(trozo) <= 31):
                out = None
                break
            out.append(int(trozo))
            pos += n
        if out and len(out) >= 2:
            return out
    return None


def _celdas_por_reglas(pag: Pagina, dias: list[Token], izq: float, der: float, paso: float, cfg: dict) -> dict | None:
    """Si la grilla de días tiene líneas verticales, cada celda queda delimitada por ellas.
    Devuelve {dia: (x0, x1)} o None si las líneas no forman una grilla coherente."""
    y0 = min(t.y0 for t in dias)
    y1 = max(t.y1 for t in dias)
    xs = sorted(x for x, a, b in pag.verticales if a <= y1 + 8 and b >= y0 - 2 and b - a > 8 and izq - 2 <= x <= der + 2)
    reglas: list[float] = []
    for x in xs:
        if not reglas or x - reglas[-1] > paso * 0.3:
            reglas.append(x)
    if len(reglas) < 6:
        return None
    # celdas entre reglas consecutivas con un ancho parecido al paso
    celdas = [(a, b) for a, b in zip(reglas, reglas[1:]) if 0.6 * paso <= b - a <= 1.5 * paso]
    if len(celdas) < 6:
        return None
    # cada número leído debe caer en una celda y la numeración debe ser consecutiva por celda
    asignacion = {}
    for t in dias:
        for i, (a, b) in enumerate(celdas):
            if a <= t.cx <= b:
                asignacion[i] = int(t.text)
                break
    if len(asignacion) < 5:
        return None
    desfases = {d - i for i, d in asignacion.items()}
    if len(desfases) != 1:
        return None
    desfase = desfases.pop()
    ultimo = cfg.get("maxDays", 31)
    out = {}
    for i, (a, b) in enumerate(celdas):
        d = i + desfase
        if 1 <= d <= ultimo:
            out[d] = (a, b)
    return out if len(out) >= 7 else None


def _grilla_sin_numeros(pag: Pagina, y0: float, y1: float, cols: list[Columna], cfg: dict) -> dict | None:
    """Cuando el OCR no leyó los números de los días pero la tabla tiene líneas verticales,
    la corrida más larga de celdas de ancho parecido (27 a 31) es la grilla: días 1..N."""
    ultimo = cfg.get("maxDays", 31)
    xs = sorted(x for x, a, b in pag.verticales if a <= y1 + 8 and b >= y0 - 2 and b - a > 8)
    reglas: list[float] = []
    for x in xs:
        if not reglas or x - reglas[-1] > 2:
            reglas.append(x)
    if len(reglas) < 20:
        return None
    anchos = [b - a for a, b in zip(reglas, reglas[1:])]
    mejor: tuple[int, int] | None = None  # (inicio, fin) en índices de celdas
    i = 0
    while i < len(anchos):
        j = i
        ref = anchos[i]
        while j + 1 < len(anchos) and abs(anchos[j + 1] - ref) <= 0.25 * ref:
            j += 1
        if mejor is None or j - i > mejor[1] - mejor[0]:
            mejor = (i, j)
        i = j + 1
    if mejor is None:
        return None
    n = mejor[1] - mejor[0] + 1
    if not 27 <= n <= ultimo:
        return None
    celdas = [(reglas[k], reglas[k + 1]) for k in range(mejor[0], mejor[1] + 1)]
    # la grilla no puede pisar una columna con nombre
    for c in cols:
        if c.dia is None and c.x0 < celdas[-1][1] - 1 and c.x1 > celdas[0][0] + 1:
            return None
    return {d + 1: celdas[d] for d in range(n)}


def _asignar(t: Token, cols: list[Columna]) -> Columna | None:
    dentro = [c for c in cols if c.x0 - 0.5 <= t.cx <= c.x1 + 0.5]
    if dentro:
        return min(dentro, key=lambda c: abs(c.cx - t.cx))
    return None


def _celdas(linea: Linea, cols: list[Columna], pag: Pagina | None = None, tipos: dict | None = None) -> dict[str, list[Token]]:
    out: dict[str, list[Token]] = {}
    for t in linea.tokens:
        c = _asignar(t, cols)
        if c:
            out.setdefault(c.nombre, []).append(t)
    if pag is not None and pag.origen == "ocr" and any(_RX_NUM.match(t.text) for t in linea.tokens):
        _releer_celdas(linea, cols, pag, out, tipos or {})
    return out


_NUMERICOS = {"int", "number", "money", "duration", "percent"}


def _normalizar_ocr_numero(txt: str) -> str:
    """Primer número del texto releído, corrigiendo O->0 e I/l->1 dentro de él."""
    txt = re.sub(r"(?<=[\d.,])[Oo]|[Oo](?=[\d.,])", "0", txt)
    txt = re.sub(r"(?<=[\d.,])[Il|]|[Il|](?=[\d.,])", "1", txt)
    m = re.search(r"-?\d[\d.,]*", txt)
    return m.group(0) if m else ""


def _releer_celdas(linea: Linea, cols: list[Columna], pag: Pagina, out: dict, tipos: dict):
    """En páginas escaneadas se relee cada celda numérica (grilla de días e importes) con
    el reconocedor sobre un recorte ampliado; es más fiable que el detector sobre la
    página entera, que se salta dígitos sueltos o pega columnas vecinas."""
    from .ingesta import leer_celda

    for c in cols:
        numerica = c.dia is not None or tipos.get(c.nombre) in _NUMERICOS
        if not numerica:
            continue
        actual = out.get(c.nombre)
        if actual and (max(t.x1 for t in actual) - min(t.x0 for t in actual)) > 1.4 * (c.x1 - c.x0):
            # el detector pegó varias celdas en un solo token ("101010101012"): se descarta
            out.pop(c.nombre, None)
            actual = None
        res = leer_celda(pag, c.x0, linea.y0, c.x1, linea.y1)
        if res is None:
            continue
        txt, conf = res
        if txt == "":
            if actual and c.dia is not None and all(len(t.text) <= 1 for t in actual):
                # el detector vio algo que la relectura no: celda vacía con ruido
                out.pop(c.nombre, None)
            continue
        limpio = _normalizar_ocr_numero(txt)
        if c.dia is not None:
            if not re.fullmatch(r"\d{1,3}", limpio):
                continue
        elif not re.fullmatch(r"-?\d[\d.,]*", limpio):
            continue
        texto_det = " ".join(t.text for t in actual) if actual else ""
        if actual and texto_det == limpio:
            continue
        ancho = c.x1 - c.x0
        desborda = actual and (min(t.x0 for t in actual) < c.x0 - 0.15 * ancho or max(t.x1 for t in actual) > c.x1 + 0.15 * ancho)
        if c.dia is not None and desborda and len(texto_det) > len(limpio) and limpio in texto_det.replace(" ", ""):
            # el detector pegó la celda vecina ('1010' sobre dos columnas): vale la relectura
            out[c.nombre] = [Token(limpio, c.x0 + 1, linea.y0, c.x1 - 1, linea.y1, pag.numero, conf=conf)]
            continue
        if actual and all(_RX_NUM.match(t.text) for t in actual):
            conf_det = min(t.conf for t in actual)
            # el detector ya leyó un número válido con confianza parecida o mejor: se respeta
            if conf_det >= conf - 0.05:
                continue
            # la relectura solo le añade dígitos (se coló un vecino por la inclinación): se respeta
            if len(limpio) > len(texto_det) and texto_det in limpio:
                continue
        out[c.nombre] = [Token(limpio, c.x0 + 1, linea.y0, c.x1 - 1, linea.y1, pag.numero, conf=conf)]


_RX_NUM = re.compile(r"^-?[\d.,]+%?$|^[-–]$")


def extraer_tabla(doc: Documento, cfg: dict, opciones: dict) -> dict:
    """Devuelve {'rows': [...], 'footer': {...}, 'columns': [...], 'bbox': ...}."""
    filas: list[dict] = []
    pie = None
    meta_cols = None
    limite_tabla = None
    stop = _compilar(cfg.get("stopAnchor", r"TOTAL"), False)
    tipos = {(h["name"] if isinstance(h, dict) else h.lower()): (h.get("type", "text") if isinstance(h, dict) else "text")
             for h in cfg["headerAnchors"]}
    col_desc = cfg.get("descriptionColumn", next(iter(tipos)))
    modo = cfg.get("rowMode", "line")
    col_ancla = cfg.get("anchorColumn")
    seccion = None
    # si al OCR se le perdió la cabecera de la columna ancla, se usa otra numérica
    respaldo_ancla = [col_ancla] + [c for c in ("total_avisos", "costo_total", "costo_unitario") if c != col_ancla]

    for pag in doc.paginas:
        lineas = agrupar_lineas(pag.tokens)
        det = detectar_columnas(pag, lineas, cfg)
        if not det:
            continue
        cols, y_ini, y_fin = det
        nombres_cols = {c.nombre for c in cols}
        if modo == "anchor" and col_ancla not in nombres_cols:
            col_ancla = next((c for c in respaldo_ancla if c in nombres_cols), col_ancla)
        if meta_cols is None:
            meta_cols = [{"name": c.nombre, "x0": round(c.x0, 1), "x1": round(c.x1, 1), "day": c.dia,
                          "weekday": c.letra} for c in cols]
        cuerpo: list[Linea] = []
        linea_stop = None
        for l in lineas:
            if l.cy <= y_fin + 1:
                continue
            if (stop.search if cfg.get("stopAnywhere") else stop.match)(sin_tildes(l.texto)):
                linea_stop = l
                break
            cuerpo.append(l)
        if linea_stop is not None:
            limite_tabla = (linea_stop.cy - 3, pag.numero)
            pie = _fila(_celdas(linea_stop, cols, pag, tipos), cols, tipos, opciones, col_desc)
            pie["_y"] = linea_stop.cy

        buffer: list[Token] = []
        for l in cuerpo:
            celdas = _celdas(l, cols, pag, tipos)
            if not celdas:
                continue
            otras = [k for k in celdas if k != col_desc]
            hay_num = any(_RX_NUM.match(t.text) for k in otras for t in celdas[k])
            if modo == "anchor":
                es_ancla = col_ancla in celdas and any(_RX_NUM.match(t.text) for t in celdas[col_ancla])
                if not es_ancla:
                    buffer.extend(celdas.get(col_desc, []) or [t for k in celdas for t in celdas[k]])
                    continue
                celdas[col_desc] = buffer + celdas.get(col_desc, [])
                buffer = []
                filas.append(_fila(celdas, cols, tipos, opciones, col_desc, seccion))
            else:
                if not hay_num:
                    texto_linea = l.texto.strip()
                    if cfg.get("sectionRows", True) and len(celdas) == 1 and col_desc not in celdas:
                        seccion = texto_linea
                    elif filas and cfg.get("mergeContinuation", True) and not otras:
                        filas[-1]["descripcion_extra"] = (filas[-1].get("descripcion_extra", "") + " " + texto_linea).strip()
                    else:
                        seccion = texto_linea
                    continue
                filas.append(_fila(celdas, cols, tipos, opciones, col_desc, seccion))

    # paquetes: fila con costo y sin avisos agrupa a las siguientes sin costo
    if cfg.get("packages"):
        paquete = None
        for f in filas:
            tiene_avisos = f.get("total_avisos", {}).get("value") not in (None, 0) or f["_dias"]
            tiene_costo = f.get("costo_total", {}).get("value") not in (None,)
            if tiene_costo and not tiene_avisos:
                paquete = f["descripcion"]["value"] if "descripcion" in f else None
                f["es_paquete"] = {"value": True, "tag": "DET_PAQUETE"}
            elif paquete and not tiene_costo:
                f["paquete"] = {"value": paquete, "tag": "DET_PAQUETE_PADRE"}
            elif tiene_costo:
                paquete = None

    # campos derivados de la descripción (p. ej. MOTIVO:)
    for f in filas:
        desc = f.get(col_desc, {}).get("_multilinea") or ""
        for nombre, spec in (cfg.get("descriptionFields") or {}).items():
            patron, tipo = (spec, "text") if isinstance(spec, str) else (spec["pattern"], spec.get("type", "text"))
            if nombre in f and f[nombre].get("value") is not None:
                continue
            m = re.search(patron, desc, re.IGNORECASE | re.DOTALL)
            if m:
                v = re.sub(r"\s+", " ", (m.group(1) if m.groups() else m.group(0))).strip()
                f[nombre] = {"value": normalizar(v, tipo, opciones), "tag": f"DET_{nombre.upper()}",
                             "source": f[col_desc].get("source")}
        if cfg.get("bonusPattern"):
            rx = re.compile(cfg["bonusPattern"], re.IGNORECASE)
            cu = f.get("costo_unitario", {}).get("value")
            bon = bool(rx.search(f.get("_crudo", ""))) or (cu == 0 and f.get("total_avisos", {}).get("value"))
            f["bonificado"] = {"value": bool(bon), "tag": "DET_BONIFICADO"}
    return {"rows": filas, "footer": pie, "columns": meta_cols, "limite": limite_tabla}


def _orden_lectura(toks: list[Token]) -> list[Token]:
    """Renglones visuales (centros a menos de medio alto) y, dentro de cada uno, de
    izquierda a derecha; una leve inclinación no invierte '16:00 19:00'."""
    renglones: list[list[Token]] = []
    for t in sorted(toks, key=lambda t: t.cy):
        if renglones and abs(t.cy - sum(x.cy for x in renglones[-1]) / len(renglones[-1])) < 0.5 * max(t.h, 1):
            renglones[-1].append(t)
        else:
            renglones.append([t])
    return [t for r in renglones for t in sorted(r, key=lambda t: t.x0)]


def _fila(celdas: dict, cols: list[Columna], tipos: dict, opciones: dict, col_desc: str, seccion=None) -> dict:
    fila: dict = {"_dias": {}}
    crudo_total = []
    for c in cols:
        toks = celdas.get(c.nombre)
        if not toks:
            continue
        toks = _orden_lectura(toks)
        crudo = " ".join(t.text for t in toks)
        crudo_total.append(crudo)
        if c.dia is not None:
            v = normalizar(crudo, "int", opciones)
            if v:
                fila["_dias"][c.dia] = {"avisos": v, "letra": c.letra, "source": _fuente(toks, crudo)}
            continue
        tipo = tipos.get(c.nombre, "text")
        if c.nombre == col_desc:
            # descripción multilínea: se respeta el salto entre renglones
            renglones: dict[int, list[Token]] = {}
            for t in toks:
                renglones.setdefault(round(t.cy / 3), []).append(t)
            multilinea = "\n".join(" ".join(x.text for x in sorted(r, key=lambda t: t.x0))
                                   for _, r in sorted(renglones.items()))
            fila[c.nombre] = {"value": re.sub(r"\s+", " ", multilinea).strip(), "tag": f"DET_{c.nombre.upper()}",
                              "source": _fuente(toks, crudo), "_multilinea": multilinea}
            continue
        fila[c.nombre] = {"value": normalizar(crudo, tipo, opciones), "tag": f"DET_{c.nombre.upper()}",
                          "source": _fuente(toks, crudo)}
    if seccion:
        fila["seccion"] = {"value": seccion, "tag": "DET_SECCION"}
    fila["_crudo"] = " | ".join(crudo_total)
    return fila
