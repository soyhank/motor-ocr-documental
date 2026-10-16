"""Genera escaneos simulados a partir de los PDF digitales de muestras/ para medir el OCR.

Por cada PDF se crean tres variantes en tests/escaneos/:
  *_limpio.png   300 DPI, sin distorsión (escáner de oficina bien calibrado)
  *_normal.png   200 DPI, 1° de inclinación, sombra lateral y algo de ruido
  *_malo.png     150 DPI, 2.5° de inclinación, sombra fuerte, ruido y desenfoque
  *_celular.jpg  foto de celular: perspectiva, luz desigual, tono amarillento, JPEG calidad 70
  *_oficina.jpg  escáner de oficina a 200 DPI guardado en JPEG calidad 55, 0.6° y algo de desenfoque

Uso: python _scripts/generar_escaneos.py [nombre_parcial ...]
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import fitz
import numpy as np

RAIZ = Path(__file__).resolve().parent.parent
DESTINO = RAIZ / "tests" / "escaneos"

VARIANTES = {
    "limpio": dict(dpi=300, angulo=0.0, sombra=0, ruido=0, blur=0),
    "normal": dict(dpi=200, angulo=1.0, sombra=35, ruido=10, blur=0),
    "malo": dict(dpi=150, angulo=2.5, sombra=60, ruido=18, blur=1),
    "celular": dict(dpi=220, angulo=0.0, sombra=0, ruido=6, blur=0.7, perspectiva=0.035, luz=70, jpeg=70),
    "oficina": dict(dpi=200, angulo=0.6, sombra=15, ruido=5, blur=0.5, jpeg=55),
}


def foto(img: np.ndarray, perspectiva: float, luz: int, semilla: int) -> np.ndarray:
    """Foto de celular: la hoja vista algo en trapecio, foco de luz y tono amarillento."""
    rng = np.random.default_rng(semilla)
    h, w = img.shape[:2]
    d = perspectiva * min(h, w)
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    dst = np.float32([[d * rng.uniform(0.5, 1.5), d * rng.uniform(0.2, 1)], [w - d * rng.uniform(0, 0.6), d * rng.uniform(0, 0.5)],
                      [w - d * rng.uniform(0.3, 1.2), h - d * rng.uniform(0, 0.4)], [d * rng.uniform(0, 0.5), h - d * rng.uniform(0.5, 1.2)]])
    out = cv2.warpPerspective(img, cv2.getPerspectiveTransform(src, dst), (w, h), borderValue=(150, 150, 150)).astype(np.float32)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    cx, cy = w * rng.uniform(0.3, 0.7), h * rng.uniform(0.2, 0.5)
    r = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2) / np.hypot(w, h)
    out -= (luz * r * 1.6)[..., None]
    out *= np.float32([0.86, 0.95, 1.0])  # BGR: tono cálido
    return np.clip(out, 0, 255).astype(np.uint8)


def render(pdf: Path, dpi: int) -> np.ndarray:
    pix = fitz.open(pdf)[0].get_pixmap(dpi=dpi)
    img = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w, pix.n)[:, :, :3]
    return cv2.cvtColor(img.copy(), cv2.COLOR_RGB2BGR)


def distorsionar(img: np.ndarray, angulo: float, sombra: int, ruido: int, blur: int, semilla: int) -> np.ndarray:
    h, w = img.shape[:2]
    out = img.astype(np.float32)
    if angulo:
        M = cv2.getRotationMatrix2D((w / 2, h / 2), angulo, 1.0)
        out = cv2.warpAffine(out, M, (w, h), borderValue=(255, 255, 255))
    if sombra:
        grad = np.linspace(0, sombra, w, dtype=np.float32)
        out -= np.tile(grad, (h, 1))[..., None]
    if ruido:
        out += np.random.default_rng(semilla).normal(0, ruido, out.shape).astype(np.float32)
    out = np.clip(out, 0, 255).astype(np.uint8)
    if blur:
        out = cv2.GaussianBlur(out, (0, 0), blur)
    return out


def main(filtros: list[str]):
    DESTINO.mkdir(parents=True, exist_ok=True)
    pdfs = sorted(p for p in (RAIZ / "muestras").iterdir() if p.suffix.lower() == ".pdf")
    if filtros:
        pdfs = [p for p in pdfs if any(f.lower() in p.name.lower() for f in filtros)]
    for i, pdf in enumerate(pdfs):
        base = pdf.stem[:40].replace(" ", "_")
        for nombre, v in VARIANTES.items():
            ext = ".jpg" if v.get("jpeg") else ".png"
            if (DESTINO / f"{base}__{nombre}{ext}").exists():
                continue  # no se rehace: el banco tiene que ser estable entre mediciones
            img = render(pdf, v["dpi"])
            if v.get("perspectiva"):
                img = foto(img, v["perspectiva"], v.get("luz", 0), semilla=i)
            img = distorsionar(img, v["angulo"], v["sombra"], v["ruido"], v["blur"], semilla=i)
            if v.get("jpeg"):
                datos = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, v["jpeg"]])[1].tobytes()
                (DESTINO / f"{base}__{nombre}.jpg").write_bytes(datos)
            else:
                (DESTINO / f"{base}__{nombre}.png").write_bytes(cv2.imencode(".png", img)[1].tobytes())
        print("ok", pdf.name)


if __name__ == "__main__":
    main(sys.argv[1:])
