"""Pruebas de las funciones de integración: formatos nuevos, errores de archivo, JSON limpio,
revalidación tras una corrección y API."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "_scripts"))

from motor.generica import leer_generico, plantilla_aprendida  # noqa: E402
from motor.ingesta import ingerir  # noqa: E402
from motor.pipeline import procesar  # noqa: E402
from motor.plantillas import puntuar  # noqa: E402
from motor.salida import revalidar_simple, salida_simple  # noqa: E402
from motor.validar import cargar_config  # noqa: E402

NUEVOS = RAIZ / "tests" / "formatos_nuevos"


@pytest.mark.parametrize("nombre", ["andina_media", "nexo_publicidad", "punto_medios"])
def test_formato_nunca_visto(nombre):
    from evaluar_formatos_nuevos import comparar

    esp = json.loads((NUEVOS / f"{nombre}.esperado.json").read_text(encoding="utf-8"))
    doc = ingerir(NUEVOS / f"{nombre}.pdf")
    plantilla, datos, rep = leer_generico(doc, cargar_config())
    ok, n, fallos = comparar(esp, datos)
    assert not fallos, fallos
    assert rep["isValid"]
    # la plantilla aprendida reconoce el mismo formato
    nueva = plantilla_aprendida(doc, plantilla, datos)
    assert puntuar(doc, nueva)[0] >= nueva["identification"]["minScoreThreshold"]


@pytest.mark.parametrize("contenido,nombre,codigo", [
    (b"", "vacio.pdf", "VACIO"),
    (b"esto no es un pdf", "falso.pdf", "PDF_DANADO"),
    (b"%PDF-1.4 basura", "roto.pdf", "PDF_DANADO"),
    (b"hola", "orden.docx", "FORMATO_NO_SOPORTADO"),
    (b"\x89PNG basura", "foto.png", "IMAGEN_DANADA"),
])
def test_errores_de_archivo(tmp_path, contenido, nombre, codigo):
    p = tmp_path / nombre
    p.write_bytes(contenido)
    r = procesar(p)
    assert r["status"] == "ERROR"
    assert r["error"]["code"] == codigo
    assert salida_simple(r)["documento"]["error"]["code"] == codigo


def test_pdf_protegido(tmp_path):
    import fitz

    d = fitz.open()
    d.new_page().insert_text((72, 72), "ORDEN N° 1")
    p = tmp_path / "clave.pdf"
    d.save(p, encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="1234", owner_pw="1234")
    assert procesar(p)["error"]["code"] == "PDF_PROTEGIDO"


def test_json_limpio_y_revalidacion():
    r = procesar(RAIZ / "muestras" / "Orden_Nº_040360-2025.PDF")
    r.pop("_doc", None)
    s = salida_simple(r)
    assert list(s) == ["documento", "cabecera", "detalle", "totales", "validacion"]
    assert s["validacion"]["ok"] and s["cabecera"]["numero_orden"]
    assert all("programacion" in f for f in s["detalle"])
    # un total corregido mal hace fallar la validación; el original vuelve a cuadrar
    malo = json.loads(json.dumps(s))
    malo["totales"]["total"] = 1.0
    assert revalidar_simple(malo)["documento"]["estado"] == "VALIDATION_FAILED"
    assert revalidar_simple(s)["documento"]["estado"] == r["status"]


def test_api_v1(monkeypatch):
    from fastapi.testclient import TestClient
    from motor.web import app

    monkeypatch.setenv("MOTOR_API_KEY", "k")
    c = TestClient(app)
    pdf = RAIZ / "muestras" / "461770_OXIGENO-NN.pdf"
    assert c.post("/api/v1/ordenes", files={"archivo": (pdf.name, pdf.read_bytes())}).status_code == 401
    r = c.post("/api/v1/ordenes?notificar=false", files={"archivo": (pdf.name, pdf.read_bytes())}, headers={"X-API-Key": "k"})
    assert r.status_code == 200 and r.json()["cabecera"]["numero_orden"] == "461770"
    v = c.post("/api/v1/ordenes/validar", json=r.json(), headers={"X-API-Key": "k"})
    assert v.json()["validacion"]["ok"]


def test_login(monkeypatch):
    from fastapi.testclient import TestClient
    from motor import web

    monkeypatch.delenv("MOTOR_LOGIN", raising=False)
    web._intentos.clear()
    c = TestClient(web.app)
    assert c.get("/", follow_redirects=False).status_code == 303
    assert c.get("/api/plantillas").status_code == 401
    assert c.get("/login").status_code == 200 and c.get("/api/salud").status_code == 200
    assert c.post("/api/login", json={"usuario": "admin", "contrasena": "mal"}).status_code == 401
    r = c.post("/api/login", json={"usuario": "admin", "contrasena": "123456789"})
    assert r.status_code == 200 and web.COOKIE in r.cookies
    assert c.get("/api/plantillas").status_code == 200
    assert c.get("/api/sesion").json()["usuario"] == "admin"
    c.post("/api/logout")
    c.cookies.clear()
    assert c.get("/api/plantillas").status_code == 401
    # una cookie alterada no sirve
    c.cookies.set(web.COOKIE, web._firmar("admin", 9999999999)[:-4] + "AAAA")
    assert c.get("/api/plantillas").status_code == 401


def test_login_bloquea_tras_intentos(monkeypatch):
    from fastapi.testclient import TestClient
    from motor import web

    web._intentos.clear()
    c = TestClient(web.app)
    for _ in range(8):
        c.post("/api/login", json={"usuario": "admin", "contrasena": "x"})
    assert c.post("/api/login", json={"usuario": "admin", "contrasena": "123456789"}).status_code == 429
    web._intentos.clear()


def test_certeza():
    from motor.certeza import certeza

    r = procesar(RAIZ / "muestras" / "461770_OXIGENO-NN.pdf")
    r.pop("_doc", None)
    c = certeza(r)
    assert c["puntaje"] >= 95 and c["nivel"] == "ALTA" and not c["dudosos"]
    assert salida_simple(r)["documento"]["certeza"]["puntaje"] == c["puntaje"]
    # si no cuadra, nunca queda en ALTA
    malo = json.loads(json.dumps(salida_simple(r)))
    malo["totales"]["total"] = 1.0
    assert revalidar_simple(malo)["documento"]["certeza"]["puntaje"] <= 70
    assert certeza({"status": "ERROR"})["puntaje"] == 0
