"""Genera escaneos simulados a partir de los PDF digitales de muestras/ para medir el OCR.

Por cada PDF se crean tres variantes en tests/escaneos/:
  *_limpio.png   300 DPI, sin distorsión (escáner de oficina bien calibrado)
  *_normal.png   200 DPI, 1° de inclinación, sombra lateral y algo de ruido
  *_malo.png     150 DPI, 2.5° de inclinación, sombra fuerte, ruido y desenfoque

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
}


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
            img = distorsionar(render(pdf, v["dpi"]), v["angulo"], v["sombra"], v["ruido"], v["blur"], semilla=i)
            (DESTINO / f"{base}__{nombre}.png").write_bytes(cv2.imencode(".png", img)[1].tobytes())
        print("ok", pdf.name)


if __name__ == "__main__":
    main(sys.argv[1:])
