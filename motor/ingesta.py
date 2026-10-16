"""Etapas 1 a 4 del pipeline: ingesta, preproceso, OCR posicional y normalización geoespacial.

Cada página termina como una lista de tokens con coordenadas normalizadas a [0..1000]
(x respecto al ancho, y respecto al alto), sin importar si venía de un PDF nativo
o de una imagen escaneada.
"""
from __future__ import annotations

import hashlib
import io
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

EXT_IMAGEN = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}
MIN_CHARS_NATIVO = 25  # menos caracteres que esto en una página = página rasterizada


@dataclass
class Token:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    page: int
    bold: bool = False
    size: float = 0.0
    conf: float = 1.0
    # coordenadas originales en puntos PDF (o píxeles) para la trazabilidad
    raw: tuple = ()

    @property
    def clean(self) -> str:
        return limpiar(self.text)

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def h(self) -> float:
        return self.y1 - self.y0

    def bbox(self) -> dict:
        return {"x0": round(self.x0, 1), "y0": round(self.y0, 1), "x1": round(self.x1, 1), "y1": round(self.y1, 1)}

    def to_dict(self) -> dict:
        return {"text": self.text, "cleanText": self.clean, "page": self.page, "bbox": self.bbox()}


@dataclass
class Pagina:
    numero: int
    ancho: float
    alto: float
    origen: str  # "nativo" | "ocr"
    tokens: list[Token] = field(default_factory=list)
    # bordes verticales detectados (x normalizado, y0, y1) para delimitar columnas
    verticales: list[tuple] = field(default_factory=list)
    imagen_png: bytes | None = None
    # imagen limpia usada por el OCR (solo páginas rasterizadas) para releer celdas
    imagen_ocr: object = None


@dataclass
class Documento:
    ruta: str
    nombre: str
    sha256: str
    paginas: list[Pagina]

    @property
    def tokens(self) -> list[Token]:
        return [t for p in self.paginas for t in p.tokens]

    def texto(self) -> str:
        return "\n".join(" ".join(l.texto for l in agrupar_lineas(p.tokens)) for p in self.paginas)


def limpiar(s: str) -> str:
    """Texto comparable: sin tildes, mayúsculas, sin puntuación de etiquetas."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.upper().replace("º", "O").replace("°", "O")
    return re.sub(r"[^\w/%$\-+.,:]", "", s).replace(".", "").strip(":")


def sha256(datos: bytes) -> str:
    return hashlib.sha256(datos).hexdigest()


# ---------------------------------------------------------------- PDF nativo

def _paginas_pdf(datos: bytes, con_imagen: bool) -> list[Pagina]:
    import fitz
    import pdfplumber

    paginas: list[Pagina] = []
    doc_fitz = fitz.open(stream=datos, filetype="pdf")
    with pdfplumber.open(io.BytesIO(datos)) as pdf:
        for i, p in enumerate(pdf.pages):
            W, H = float(p.width), float(p.height)
            # se quitan duplicados y los espacios "fantasma" que caen encima de una letra
            p = p.dedupe_chars(tolerance=1)
            fantasmas = _espacios_superpuestos(p.chars)
            if fantasmas:
                p = p.filter(lambda o: not (o.get("object_type") == "char" and id(o) in fantasmas))
            words = p.extract_words(x_tolerance=1.5, y_tolerance=2, keep_blank_chars=False,
                                    extra_attrs=["fontname", "size"])
            texto_util = sum(len(w["text"].strip()) for w in words)
            png = None
            if con_imagen or texto_util < MIN_CHARS_NATIVO:
                png = doc_fitz[i].get_pixmap(dpi=300 if texto_util < MIN_CHARS_NATIVO else 110).tobytes("png")
            if texto_util < MIN_CHARS_NATIVO:
                pag = _pagina_ocr(png, i + 1)
                paginas.append(pag)
                continue
            pag = Pagina(numero=i + 1, ancho=W, alto=H, origen="nativo", imagen_png=png)
            vistos = set()
            for w in words:
                txt = w["text"].strip()
                if not txt:
                    continue
                # algunos generadores dibujan el mismo texto dos veces en el mismo lugar
                clave = (txt, round(w["x0"]), round(w["top"]))
                if clave in vistos:
                    continue
                vistos.add(clave)
                pag.tokens.append(Token(
                    text=txt,
                    x0=w["x0"] / W * 1000, y0=w["top"] / H * 1000,
                    x1=w["x1"] / W * 1000, y1=w["bottom"] / H * 1000,
                    page=i + 1, bold="bold" in w.get("fontname", "").lower(),
                    size=float(w.get("size", 0)),
                    raw=(round(w["x0"], 1), round(w["top"], 1), round(w["x1"], 1), round(w["bottom"], 1)),
                ))
            for e in p.edges:
                if e.get("orientation") == "v" and abs(e["bottom"] - e["top"]) > 3:
                    pag.verticales.append((e["x0"] / W * 1000, e["top"] / H * 1000, e["bottom"] / H * 1000))
            paginas.append(pag)
    return paginas


def _espacios_superpuestos(chars: list[dict]) -> set[int]:
    """Algunos generadores dibujan una capa de espacios desfasada sobre el texto, lo que
    parte palabras como '14:30-16:00' en letras sueltas. Se marcan los espacios que se
    solapan con un carácter visible del mismo renglón."""
    por_renglon: dict[int, list[dict]] = {}
    for c in chars:
        if c["text"].strip():
            por_renglon.setdefault(round(c["top"]), []).append(c)
    # solo cuentan los espacios que vienen en rachas (la capa fantasma se dibuja seguida);
    # un espacio suelto entre dos palabras es real aunque la cursiva lo haga solaparse
    en_racha = set()
    i = 0
    while i < len(chars):
        j = i
        while j < len(chars) and not chars[j]["text"].strip():
            j += 1
        if j - i >= 3:
            en_racha.update(id(c) for c in chars[i:j])
        i = j + 1 if j == i else j
    out = set()
    for c in chars:
        if id(c) not in en_racha:
            continue
        ancho = max(c["x1"] - c["x0"], 0.1)
        for k in (round(c["top"]) - 1, round(c["top"]), round(c["top"]) + 1):
            for v in por_renglon.get(k, ()):
                solape = min(c["x1"], v["x1"]) - max(c["x0"], v["x0"])
                if solape > 0.3 * ancho:
                    out.add(id(c))
                    break
            if id(c) in out:
                break
    return out


# ---------------------------------------------------------------- imágenes / escaneados

LADO_OBJETIVO = 3300  # ~300 DPI en una hoja carta/A4 apaisada


def preprocesar(img: np.ndarray) -> dict:
    """Prepara una página rasterizada.

    - Escala de grises y reescalado a ~300 DPI si la imagen viene más chica.
    - Corrección de inclinación (Hough + minAreaRect) hasta ±45°.
    - Corrección de iluminación (quita sombras) y binarización Sauvola para la estructura.
    - Imagen para el OCR: grises sin sombras y sin las líneas de la tabla, que de otro
      modo se leen como "|" y pegan los números.
    """
    import cv2

    gris = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if max(gris.shape) < LADO_OBJETIVO * 0.8:
        f = LADO_OBJETIVO / max(gris.shape)
        gris = cv2.resize(gris, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)
    gris = _enderezar(gris)
    fondo = cv2.GaussianBlur(gris, (0, 0), sigmaX=25)
    plano = np.clip(gris.astype(np.float32) / np.maximum(fondo, 1) * 235, 0, 255).astype(np.uint8)
    bw = _sauvola(plano)
    tinta = cv2.bitwise_not(bw)
    H, W = gris.shape
    horiz = cv2.morphologyEx(tinta, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(W // 25, 20), 1)))
    vert = cv2.morphologyEx(tinta, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(H // 60, 15))))
    reglas = cv2.dilate(cv2.bitwise_or(horiz, vert), np.ones((3, 3), np.uint8))
    para_ocr = plano.copy()
    para_ocr[reglas > 0] = 255
    verticales = []
    n, _, stats, _ = cv2.connectedComponentsWithStats(vert)
    for i in range(1, n):
        x, y, w, h, _ = stats[i]
        verticales.append(((x + w / 2) / W * 1000, y / H * 1000, (y + h) / H * 1000))
    return {"gris": gris, "binaria": bw, "ocr": para_ocr, "verticales": verticales}


def _enderezar(gris: np.ndarray) -> np.ndarray:
    import cv2

    inv = cv2.bitwise_not(gris)
    _, bw = cv2.threshold(inv, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    lineas = cv2.HoughLinesP(bw, 1, np.pi / 720, threshold=200,
                             minLineLength=gris.shape[1] // 6, maxLineGap=10)
    angulos = []
    if lineas is not None:
        for x1, y1, x2, y2 in lineas.reshape(-1, 4):
            a = np.degrees(np.arctan2(y2 - y1, x2 - x1))
            if abs(a) <= 45:
                angulos.append(a)
    if not angulos:
        coords = np.column_stack(np.where(bw > 0))
        if len(coords) < 50:
            return gris
        a = cv2.minAreaRect(coords[:, ::-1].astype(np.float32))[-1]
        angulos = [a - 90 if a > 45 else a]
    ang = float(np.median(angulos))
    if abs(ang) < 0.15:
        return gris
    h, w = gris.shape
    M = cv2.getRotationMatrix2D((w / 2, h / 2), ang, 1.0)
    return cv2.warpAffine(gris, M, (w, h), flags=cv2.INTER_CUBIC, borderValue=255)


def _sauvola(gris: np.ndarray, ventana: int = 31, k: float = 0.2, R: float = 128.0) -> np.ndarray:
    import cv2

    g = gris.astype(np.float64)
    media = cv2.boxFilter(g, -1, (ventana, ventana), borderType=cv2.BORDER_REFLECT)
    media2 = cv2.boxFilter(g * g, -1, (ventana, ventana), borderType=cv2.BORDER_REFLECT)
    desv = np.sqrt(np.maximum(media2 - media * media, 0))
    umbral = media * (1 + k * (desv / R - 1))
    return np.where(g > umbral, 255, 0).astype(np.uint8)


_OCR = None


def _motor_ocr():
    global _OCR
    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR
        # umbral de detección más bajo: con el valor por defecto se pierden renglones
        # largos donde dos columnas de texto se tocan
        # text_score bajo: con cajas por carácter la confianza de los renglones largos
        # baja y la librería los descartaría
        _OCR = RapidOCR(det_box_thresh=0.3, text_score=0.3)
    return _OCR


def _cortes_por_huecos(tinta: np.ndarray, x0: int, y0: int, x1: int, y1: int) -> list[tuple[float, float]]:
    """Huecos sin tinta dentro del renglón: (x central en píxeles de página, ancho en
    fracción del alto del renglón). Se proyecta la tinta con el texto derecho y también
    "des-inclinado" (cursivas), y se juntan los huecos de ambas proyecciones."""
    import cv2

    alto = max(y1 - y0, 1)
    recorte = tinta[max(y0, 0):max(y1, 0), max(x0, 0):max(x1, 0)]
    if recorte.size == 0:
        return []
    minimo = 2
    cortes: list[tuple[float, float]] = []
    for k in (0.0, 0.22, 0.32):
        if k:
            M = np.float32([[1, k, -k * alto], [0, 1, 0]])
            proy = cv2.warpAffine(recorte, M, (recorte.shape[1], recorte.shape[0]), borderValue=0)
        else:
            proy = recorte
        hay = (proy > 0).sum(axis=0) > 0
        inicio = None
        for i, v in enumerate(hay):
            if not v and inicio is None:
                inicio = i
            elif v and inicio is not None:
                if i - inicio >= minimo and inicio > 0:
                    cortes.append((x0 + (inicio + i) / 2 + k * alto / 2, (i - inicio) / alto))
                inicio = None
    return sorted(cortes)


_CHARS_NUMERO = set("0123456789.,:/-%")
_ANCHOS = {"estrecho": 0.45, "ancho": 1.45, "mayuscula": 1.05, "espacio": 0.55}


def _ancho_estimado(c: str) -> float:
    """Ancho relativo aproximado de un carácter (para repartir la caja del renglón)."""
    if c in "il1.,:;'|!jtfrI()[]":
        return _ANCHOS["estrecho"]
    if c in "mwMW@":
        return _ANCHOS["ancho"]
    if c.isspace():
        return _ANCHOS["espacio"]
    if c.isupper() or c in "ÑÁÉÍÓÚ":
        return _ANCHOS["mayuscula"]
    return 1.0


def _palabras(texto: str, caja, cajas_car, tinta: np.ndarray | None = None) -> list[tuple[str, float, float, float, float]]:
    """Parte una frase del OCR en palabras: en los espacios que reconoció el modelo y en
    los huecos reales de tinta que caen entre dos caracteres consecutivos. Dentro de un
    número (dígitos, punto, coma, dos puntos) solo se corta si el hueco es muy grande:
    en fuentes monoespaciadas el "1" deja mucho aire a los lados."""
    xs = [c[0] for c in caja]
    ys = [c[1] for c in caja]
    X0, X1, Y0, Y1 = min(xs), max(xs), min(ys), max(ys)
    chars = list(texto)
    visibles = [c for c in chars if not c.isspace()]
    if cajas_car and len(cajas_car) in (len(chars), len(visibles)):
        cajas = [(min(p[0] for p in b), max(p[0] for p in b)) for b in cajas_car]
        if len(cajas) == len(chars):
            pares = list(zip(chars, cajas))
        else:
            it = iter(cajas)
            pares = [(c, next(it) if not c.isspace() else None) for c in chars]
    else:  # sin cajas por carácter: reparto proporcional al ancho estimado de cada letra
        anchos = [_ancho_estimado(c) for c in chars]
        total = sum(anchos) or 1
        pares, acum = [], 0.0
        for c, w in zip(chars, anchos):
            pares.append((c, (X0 + (X1 - X0) * acum / total, X0 + (X1 - X0) * (acum + w) / total)))
            acum += w
    cortes = _cortes_por_huecos(tinta, int(X0), int(Y0), int(X1), int(Y1)) if tinta is not None else []
    # hueco típico entre letras del renglón: un espacio entre palabras lo supera varias veces
    anchos_h = sorted(w for _, w in cortes)
    tipico = anchos_h[len(anchos_h) // 2] if anchos_h else 0.1
    out, actual = [], []
    for c, b in pares:
        if c.isspace() or b is None:
            if actual:
                out.append(actual)
            actual = []
            continue
        if actual and c not in ".,:;":  # la puntuación se pega siempre al carácter anterior
            prev_c, prev = actual[-1]
            cx_prev, cx_act = (prev[0] + prev[1]) / 2, (b[0] + b[1]) / 2
            huecos = [w for k, w in cortes if cx_prev < k < cx_act]
            if huecos:
                numerico = prev_c in _CHARS_NUMERO and c in _CHARS_NUMERO
                # entre letras hace falta un hueco claro (los espacios que sí reconoce el
                # modelo ya se cortaron arriba); dentro de un número, uno muy grande
                h = max(huecos)
                if numerico:
                    corta = h >= 0.7
                else:
                    corta = h >= 0.38 or (h >= 0.2 and h >= 3 * tipico)
                if corta:
                    out.append(actual)
                    actual = []
        actual.append((c, b))
    if actual:
        out.append(actual)
    return [("".join(c for c, _ in g), min(b[0] for _, b in g), Y0, max(b[1] for _, b in g), Y1) for g in out]


def _rect(caja):
    xs = [p[0] for p in caja]
    ys = [p[1] for p in caja]
    return min(xs), min(ys), max(xs), max(ys)


def _ocr_franja(motor, franja: np.ndarray, desplaz_y: int, min_score: float = 0.3) -> list:
    """Detector, recorte y reconocedor llamados por separado. El modo return_word_box de la
    librería descarta renglones largos y deja el motor en mal estado, así que las cajas
    por carácter se calculan aquí a partir de las columnas de decodificación."""
    boxes, _ = motor.text_det(franja)
    if boxes is None or len(boxes) == 0:
        return []
    crops = motor.get_crop_img_list(franja, boxes)
    rec, _ = motor.text_rec(crops, True)
    items = []
    for box, r in zip(boxes, rec):
        texto, score = r[0], float(r[1])
        if score < min_score or not texto.strip():
            continue
        caja = [[float(p[0]), float(p[1]) + desplaz_y] for p in box]
        cajas_car = None
        try:
            col_num, word_list, word_col_list = r[2][0], r[2][1], r[2][2]
            chars = [c for w in word_list for c in w]
            cols = [c for w in word_col_list for c in w]
            if "".join(chars) == texto and len(cols) == len(chars) and col_num:
                x0, x1 = caja[0][0], caja[1][0]
                y0, y1 = min(p[1] for p in caja), max(p[1] for p in caja)
                celda = (x1 - x0) / float(col_num)
                cajas_car = [[[x0 + c * celda, y0], [x0 + (c + 1) * celda, y0],
                              [x0 + (c + 1) * celda, y1], [x0 + c * celda, y1]] for c in cols]
        except (IndexError, TypeError):
            cajas_car = None
        items.append((caja, texto, score, cajas_car))
    return items


def _ocr_por_franjas(img: np.ndarray, alto_franja: int = 760, solape: int = 120) -> list:
    """Corre el OCR por franjas horizontales. Con la página completa el detector se salta
    los números pequeños de las grillas; por franjas los encuentra."""
    H = img.shape[0]
    motor = _motor_ocr()
    items = []
    y = 0
    while True:
        y1 = min(y + alto_franja, H)
        items.extend(_ocr_franja(motor, img[y:y1], y))
        if y1 >= H:
            break
        y = y1 - solape
    # en las zonas de solape el mismo texto sale dos veces: se queda el más completo
    items.sort(key=lambda it: -((_rect(it[0])[2] - _rect(it[0])[0]) * (_rect(it[0])[3] - _rect(it[0])[1])))
    elegidos = []
    for it in items:
        a = _rect(it[0])
        area_a = (a[2] - a[0]) * (a[3] - a[1])
        repetido = False
        for e in elegidos:
            b = _rect(e[0])
            inter = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
            if area_a and inter / area_a > 0.5:
                repetido = True
                break
        if not repetido:
            elegidos.append(it)
    return elegidos


_RX_ETIQUETA_PUNTO = re.compile(r"^([^\d]*[A-Za-z°º][.°º])(\d\S*)$")


def _partir_etiqueta(texto: str) -> list[str]:
    """'RUC:20566514622' -> ['RUC:', '20566514622']; 'Rv.0' -> ['Rv.', '0']; 'N°040360' -> ['N°', '040360'].
    Se corta en el último ':' cuando lo que va antes tiene letras y no dígitos, o entre una
    etiqueta terminada en punto/grado y un número."""
    i = texto.rfind(":")
    if 0 < i < len(texto) - 1:
        antes = texto[:i]
        if re.search(r"[A-Za-z]", antes) and not re.search(r"\d", antes):
            return [texto[:i + 1], texto[i + 1:]]
    if i == 0 and len(texto) > 1:
        return [":", texto[1:]]
    m = _RX_ETIQUETA_PUNTO.match(texto)
    if m:
        return [m.group(1), m.group(2)]
    m = re.match(r"^([A-Za-z]{3,})(\d[\d.,]*%)$", texto)  # IGV18.00%
    if m:
        return [m.group(1), m.group(2)]
    return [texto]


def _pulir_token_ocr(texto: str) -> list[str]:
    """Limpieza de un token leído por OCR:
    - puntuación de ancho completo (，：（) a ASCII;
    - 0 entre letras es una O (PERI0DO, GRUP0RPP);
    - "RUC:20566514622", "Rv.0" o "Reset/Estado:" se parten en etiqueta y valor;
    - en tokens mayormente numéricos, O->0 e I/l->1 (confusiones típicas)."""
    texto = unicodedata.normalize("NFKC", texto)
    texto = re.sub(r"(?<=[A-Za-z])0(?=[A-Za-z])", "O", texto)
    partes = []
    for trozo in re.split(r"(?<=[A-Za-z])(/)(?=[A-Za-z])", texto):
        partes.extend(_partir_etiqueta(trozo))
    out = []
    for p in partes:
        digitos = sum(c.isdigit() for c in p)
        if digitos and digitos * 2 >= len(p.replace(".", "").replace(",", "")):
            for _ in range(2):
                p = re.sub(r"(?<=[\d.,])[Oo]|[Oo](?=[\d.,])", "0", p)
                p = re.sub(r"(?<=[\d.,])[Il|]|[Il|](?=[\d.,])", "1", p)
        if p:
            out.append(p)
    return out


def _pagina_ocr(png: bytes, numero: int) -> Pagina:
    import cv2

    img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    prep = preprocesar(img)
    H, W = prep["gris"].shape
    vista = cv2.imencode(".png", prep["gris"])[1].tobytes()
    pag = Pagina(numero=numero, ancho=W, alto=H, origen="ocr", imagen_png=vista, verticales=prep["verticales"],
                 imagen_ocr=prep["ocr"])
    tinta = cv2.bitwise_not(_sauvola(prep["ocr"]))
    for caja, texto, conf, cajas_car in _ocr_por_franjas(cv2.cvtColor(prep["ocr"], cv2.COLOR_GRAY2BGR)):
        for pal, x0, y0, x1, y1 in _palabras(texto, caja, cajas_car, tinta):
            partes = _pulir_token_ocr(pal)
            total = sum(len(p) for p in partes) or 1
            pos = 0
            for p in partes:
                px0 = x0 + (x1 - x0) * pos / total
                pos += len(p)
                px1 = x0 + (x1 - x0) * pos / total
                pag.tokens.append(Token(
                    text=p, x0=px0 / W * 1000, y0=y0 / H * 1000, x1=px1 / W * 1000, y1=y1 / H * 1000,
                    page=numero, raw=(round(px0), round(y0), round(px1), round(y1)), conf=conf,
                ))
    return pag


# ---------------------------------------------------------------- entrada principal

def ingerir(ruta: str | Path, datos: bytes | None = None, con_imagen: bool = False) -> Documento:
    ruta = Path(ruta)
    if datos is None:
        datos = ruta.read_bytes()
    ext = ruta.suffix.lower()
    if ext == ".pdf":
        paginas = _paginas_pdf(datos, con_imagen)
    elif ext in EXT_IMAGEN:
        if ext in {".tif", ".tiff"}:
            from PIL import Image
            im = Image.open(io.BytesIO(datos))
            paginas = []
            for i in range(getattr(im, "n_frames", 1)):
                im.seek(i)
                b = io.BytesIO()
                im.convert("RGB").save(b, "PNG")
                paginas.append(_pagina_ocr(b.getvalue(), i + 1))
        else:
            paginas = [_pagina_ocr(datos, 1)]
    else:
        raise ValueError(f"Formato no soportado: {ext}")
    for p in paginas:
        p.tokens.sort(key=lambda t: (round(t.cy / 4), t.x0))
    return Documento(ruta=str(ruta), nombre=ruta.name, sha256=sha256(datos), paginas=paginas)


# ---------------------------------------------------------------- líneas

@dataclass
class Linea:
    tokens: list[Token]

    @property
    def y0(self):
        return min(t.y0 for t in self.tokens)

    @property
    def y1(self):
        return max(t.y1 for t in self.tokens)

    @property
    def cy(self):
        return sum(t.cy for t in self.tokens) / len(self.tokens)

    @property
    def texto(self):
        return " ".join(t.text for t in self.tokens)

    @property
    def page(self):
        return self.tokens[0].page

    @property
    def page_ocr(self) -> bool:
        return self.tokens[0].conf < 1.0  # los tokens de PDF nativo llevan confianza 1.0


def agrupar_lineas(tokens: list[Token]) -> list[Linea]:
    """Agrupa tokens en renglones por solapamiento vertical de su centro."""
    lineas: list[Linea] = []
    for t in sorted(tokens, key=lambda t: (t.page, t.cy)):
        destino = None
        for l in reversed(lineas[-4:]):
            if l.page != t.page:
                continue
            alto = max(min(t.h, max(x.h for x in l.tokens)), 1)
            if abs(l.cy - t.cy) <= alto * 0.45:
                destino = l
                break
        if destino:
            destino.tokens.append(t)
        else:
            lineas.append(Linea([t]))
    for l in lineas:
        l.tokens.sort(key=lambda t: t.x0)
    lineas.sort(key=lambda l: (l.page, l.cy))
    return lineas


def leer_celda(pag: Pagina, x0: float, y0: float, x1: float, y1: float) -> tuple[str, float] | None:
    """Relee una celda pequeña (un número de la grilla o un importe) solo con el
    reconocedor, sin detector, ampliando el recorte. Devuelve (texto, confianza);
    ("", 1.0) si la celda está vacía y None si no hay imagen."""
    import cv2

    img = pag.imagen_ocr
    if img is None:
        return None
    H, W = img.shape[:2]
    a, b = int(x0 / 1000 * W) + 2, int(x1 / 1000 * W) - 2
    c, d = int(y0 / 1000 * H) - 3, int(y1 / 1000 * H) + 3
    if b - a < 4 or d - c < 4:
        return None
    celda = img[max(c, 0):d, max(a, 0):b].copy()
    # restos de líneas verticales de la tabla: columnas casi llenas de tinta
    llenas = (celda < 128).mean(axis=0) > 0.85
    celda[:, llenas] = 255
    cols_tinta = (celda < 128).sum(axis=0)
    if (celda < 128).sum() < 12:
        return "", 1.0
    # recorta al contenido real y amplía a ~64 px de alto
    xs = np.where(cols_tinta > 0)[0]
    celda = celda[:, max(xs[0] - 4, 0):xs[-1] + 5]
    f = 64 / max(celda.shape[0], 1)
    if 0.5 < f < 4:
        celda = cv2.resize(celda, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)
    celda = cv2.copyMakeBorder(celda, 10, 10, 20, 20, cv2.BORDER_CONSTANT, value=255)
    res, _ = _motor_ocr()(cv2.cvtColor(celda, cv2.COLOR_GRAY2BGR), use_det=False, use_cls=False, use_rec=True)
    if not res:
        return None
    return str(res[0][0]).strip(), float(res[0][1])
