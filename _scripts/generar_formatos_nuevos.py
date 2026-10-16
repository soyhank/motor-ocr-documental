"""Crea órdenes de agencias inventadas, con diseños y etiquetas que el motor nunca vio, para
probar el mapeo automático de formatos nuevos. Escribe tests/formatos_nuevos/*.pdf y el
valor esperado de cada una en *.esperado.json.

Uso: python _scripts/generar_formatos_nuevos.py
"""
from __future__ import annotations

import calendar
import json
from datetime import date
from pathlib import Path

import fitz

RAIZ = Path(__file__).resolve().parent.parent
DESTINO = RAIZ / "tests" / "formatos_nuevos"
DIAS = "LMMJVSD"  # lunes..domingo
NOMBRES_DIA = ["L", "M", "M", "J", "V", "S", "D"]


def ruc(base10: str) -> str:
    """Completa un RUC de 10 dígitos con su dígito verificador (módulo 11 SUNAT)."""
    f = [5, 4, 3, 2, 7, 6, 5, 4, 3, 2]
    s = sum(int(d) * p for d, p in zip(base10, f))
    r = 11 - s % 11
    return base10 + str({10: 0, 11: 1}.get(r, r))


def texto(pg, x, y, s, tam=8, negrita=False):
    pg.insert_text((x, y), s, fontsize=tam, fontname="hebo" if negrita else "helv")


def caja(pg, x0, y0, x1, y1, ancho=0.6):
    pg.draw_rect(fitz.Rect(x0, y0, x1, y1), width=ancho)


def money(v):
    return f"{v:,.2f}"


def formato_andina():
    """Membrete arriba a la izquierda, etiquetas 'Etiqueta:' y grilla de días."""
    ag, ruc_ag = "ANDINA MEDIA S.A.C.", ruc("2061234567")
    anun, ruc_an = "DISTRIBUIDORA LIMA NORTE S.A.C.", ruc("2055566677")
    anio, mes = 2025, 6
    filas = [
        ("NOTICIERO MATINAL", "06:00-09:00", 20, {2: 2, 3: 2, 4: 2, 5: 2, 6: 2, 9: 2, 10: 2, 11: 2, 12: 2, 13: 2}, 210.00),
        ("ROTATIVO TARDE", "14:00-18:00", 20, {16: 3, 17: 3, 18: 3, 19: 3, 20: 3}, 150.00),
        ("ROTATIVO NOCHE BONIFICADO", "19:00-22:00", 20, {21: 2, 22: 2}, 0.00),
    ]
    doc = fitz.open()
    pg = doc.new_page(width=842, height=595)  # A4 apaisada
    texto(pg, 30, 40, ag, 13, True)
    texto(pg, 30, 54, f"RUC {ruc_ag}  -  Av. Javier Prado Este 1234, San Isidro", 8)
    texto(pg, 560, 40, "ORDEN DE PUBLICIDAD", 12, True)
    texto(pg, 560, 56, "N° de Orden: AM-2025-0457", 9, True)
    y = 90
    izq = [("Fecha de Orden:", "14/05/2025"), ("Anunciante:", anun), ("RUC Anunciante:", ruc_an),
           ("Producto:", "AGUA DE MESA CRISTALINA"), ("Campaña:", "VERANO REFRESCANTE")]
    der = [("Atención:", "GRUPO RPP S.A.C."), ("Emisora:", "RPP NOTICIAS"), ("Vigencia:", "01/06/2025 al 30/06/2025"),
           ("Forma de pago:", "45 días"), ("Plaza:", "LIMA")]
    for i, (a, b) in enumerate(izq):
        texto(pg, 30, y + 14 * i, a, 8, True)
        texto(pg, 130, y + 14 * i, b, 8)
    for i, (a, b) in enumerate(der):
        texto(pg, 460, y + 14 * i, a, 8, True)
        texto(pg, 540, y + 14 * i, b, 8)
    # tabla
    y0 = 175
    cols = [("Programa", 30, 175), ("Hora", 175, 235), ("Seg.", 235, 260)]
    ndias = calendar.monthrange(anio, mes)[1]
    xd = 260
    ancho_dia = 12.4
    fin_dias = xd + ndias * ancho_dia
    cols_der = [("Spots", fin_dias, fin_dias + 34), ("Tarifa", fin_dias + 34, fin_dias + 84), ("Importe", fin_dias + 84, fin_dias + 144)]
    caja(pg, 30, y0, fin_dias + 144, y0 + 26)
    for nombre, a, b in cols + cols_der:
        texto(pg, a + 3, y0 + 16, nombre, 7, True)
        pg.draw_line((b, y0), (b, y0 + 26 + 18 * len(filas)), width=0.4)
    for d in range(1, ndias + 1):
        x = xd + (d - 1) * ancho_dia
        texto(pg, x + 3, y0 + 10, NOMBRES_DIA[date(anio, mes, d).weekday()], 6)
        texto(pg, x + (3 if d < 10 else 1), y0 + 22, str(d), 6)
        pg.draw_line((x, y0), (x, y0 + 26 + 18 * len(filas)), width=0.3)
    yy = y0 + 26
    total_neto = 0
    esperado_filas = []
    for desc, hora, seg, dias, tarifa in filas:
        spots = sum(dias.values())
        imp = round(spots * tarifa, 2)
        total_neto += imp
        texto(pg, 33, yy + 12, desc, 7)
        texto(pg, 178, yy + 12, hora, 7)
        texto(pg, 240, yy + 12, str(seg), 7)
        for d, n in dias.items():
            texto(pg, xd + (d - 1) * ancho_dia + 3, yy + 12, str(n), 7)
        texto(pg, fin_dias + 10, yy + 12, str(spots), 7)
        texto(pg, fin_dias + 40, yy + 12, money(tarifa), 7)
        texto(pg, fin_dias + 92, yy + 12, money(imp), 7)
        pg.draw_line((30, yy + 18), (fin_dias + 144, yy + 18), width=0.3)
        esperado_filas.append({"total_avisos": spots, "costo_unitario": tarifa, "costo_total": imp, "duracion": float(seg),
                               "horario": hora, "dias": dias})
        yy += 18
    caja(pg, 30, y0 + 26, fin_dias + 144, yy)
    igv = round(total_neto * 0.18, 2)
    total = round(total_neto + igv, 2)
    yt = yy + 30
    for i, (a, b) in enumerate([("Subtotal", total_neto), ("IGV 18%", igv), ("Total a pagar", total)]):
        texto(pg, 600, yt + 14 * i, a, 9, True)
        texto(pg, 720, yt + 14 * i, "S/ " + money(b), 9)
    texto(pg, 30, yt, "Elaborado por: CARLA MENDOZA", 8)
    texto(pg, 30, 560, "Las órdenes deben facturarse a nombre de la agencia indicando el número de orden.", 7)
    esperado = {"agencia": ag, "agencia_ruc": ruc_ag, "anunciante": anun, "numero_orden": "AM-2025-0457",
                "fecha_emision": "2025-05-14", "medio": "RPP NOTICIAS", "periodo_inicio": "2025-06-01",
                "periodo_fin": "2025-06-30", "neto": total_neto, "igv": igv, "total": total, "filas": esperado_filas}
    return doc, esperado


def formato_nexo():
    """Sin grilla de días; membrete centrado; 'Señores:'; totales con otras palabras."""
    ag, ruc_ag = "NEXO PUBLICIDAD E.I.R.L.", ruc("2060987654")
    anun = "CLINICA SANTA ROSA S.A."
    filas = [("Mención en vivo - Programa Ampliación de Noticias", "07:00-08:00", 30, 12, 380.00),
             ("Spot rotativo", "06:00-22:00", 20, 40, 95.50),
             ("Spot rotativo bonificado", "06:00-22:00", 20, 8, 0.00)]
    doc = fitz.open()
    pg = doc.new_page(width=595, height=842)  # A4 vertical
    texto(pg, 200, 50, ag, 14, True)
    texto(pg, 215, 64, f"R.U.C. {ruc_ag}", 9)
    texto(pg, 40, 110, "ORDEN DE SERVICIO Nro 1288", 12, True)
    texto(pg, 380, 110, "Lima, 2 de junio de 2025", 9)
    datos = [("Señores:", "GRUPO RPP SAC"), ("Cliente:", anun), ("Medio:", "RADIO CAPITAL"),
             ("Periodo de transmisión:", "05/06/2025 al 20/06/2025"), ("Condición de pago:", "30 días fecha factura")]
    for i, (a, b) in enumerate(datos):
        texto(pg, 40, 140 + 15 * i, a, 9, True)
        texto(pg, 170, 140 + 15 * i, b, 9)
    y0 = 240
    cols = [("Detalle", 40, 250), ("Franja", 250, 315), ("Dur.", 315, 345), ("Cantidad", 345, 395),
            ("Precio Unit.", 395, 465), ("Valor Total", 465, 555)]
    caja(pg, 40, y0, 555, y0 + 18)
    for n, a, b in cols:
        texto(pg, a + 3, y0 + 12, n, 8, True)
    yy = y0 + 18
    neto = 0
    esp = []
    for desc, franja, dur, cant, pu in filas:
        v = round(cant * pu, 2)
        neto += v
        texto(pg, 43, yy + 13, desc[:42], 8)
        texto(pg, 253, yy + 13, franja, 8)
        texto(pg, 320, yy + 13, f'{dur}"', 8)
        texto(pg, 360, yy + 13, str(cant), 8)
        texto(pg, 405, yy + 13, money(pu), 8)
        texto(pg, 485, yy + 13, money(v), 8)
        yy += 20
        esp.append({"total_avisos": cant, "costo_unitario": pu, "costo_total": v, "duracion": float(dur), "horario": franja})
    caja(pg, 40, y0 + 18, 555, yy)
    igv = round(neto * 0.18, 2)
    tot = round(neto + igv, 2)
    for i, (a, b) in enumerate([("Valor venta", neto), ("I.G.V. (18%)", igv), ("Importe total", tot)]):
        texto(pg, 380, yy + 30 + 15 * i, a, 9, True)
        texto(pg, 480, yy + 30 + 15 * i, money(b), 9)
    texto(pg, 40, yy + 90, "Moneda: Soles", 9)
    esperado = {"agencia": ag, "agencia_ruc": ruc_ag, "anunciante": anun, "numero_orden": "1288", "fecha_emision": "2025-06-02",
                "medio": "RADIO CAPITAL", "periodo_inicio": "2025-06-05", "periodo_fin": "2025-06-20",
                "neto": round(neto, 2), "igv": igv, "total": tot, "filas": esp}
    return doc, esperado


def formato_punto():
    """Datos en recuadros con la etiqueta arriba del valor; tabla con grilla de días."""
    ag, ruc_ag = "PUNTO MEDIOS PERU S.A.C.", ruc("2054433221")
    anun, ruc_an = "FERRETERIA EL CONSTRUCTOR S.A.C.", ruc("2049988776")
    anio, mes = 2025, 7
    filas = [("Rotativo mañana", "06:00-12:00", 15, {1: 4, 2: 4, 3: 4, 4: 4, 7: 4, 8: 4}, 120.00),
             ("Rotativo tarde", "12:00-18:00", 15, {9: 3, 10: 3, 11: 3, 14: 3}, 98.00)]
    doc = fitz.open()
    pg = doc.new_page(width=842, height=595)
    texto(pg, 30, 36, ag, 12, True)
    texto(pg, 30, 50, f"RUC: {ruc_ag}", 8)
    texto(pg, 620, 36, "ORDEN N° 7731", 13, True)
    recuadros = [("Fecha", "30/06/2025"), ("Anunciante", anun), ("RUC", ruc_an), ("Medio", "RADIO OXIGENO"),
                 ("Periodo", "01/07/2025 al 14/07/2025"), ("Proveedor", "GRUPORPP S.A.C.")]
    x = 30
    anchos = [80, 190, 80, 110, 140, 130]
    for (a, b), w in zip(recuadros, anchos):
        caja(pg, x, 70, x + w, 104)
        texto(pg, x + 4, 80, a, 7, True)
        texto(pg, x + 4, 97, b, 8)
        x += w
    y0 = 130
    ndias = calendar.monthrange(anio, mes)[1]
    xd = 260
    ad = 12.4
    fin = xd + ndias * ad
    cols = [("Descripción", 30, 175), ("Horario", 175, 235), ("Seg", 235, 260)]
    der = [("Total", fin, fin + 34), ("C. Unitario", fin + 34, fin + 90), ("C. Total", fin + 90, fin + 150)]
    caja(pg, 30, y0, fin + 150, y0 + 26)
    for n, a, b in cols + der:
        texto(pg, a + 3, y0 + 16, n, 7, True)
        pg.draw_line((b, y0), (b, y0 + 26 + 18 * len(filas)), width=0.4)
    for d in range(1, ndias + 1):
        xx = xd + (d - 1) * ad
        texto(pg, xx + 3, y0 + 10, NOMBRES_DIA[date(anio, mes, d).weekday()], 6)
        texto(pg, xx + (3 if d < 10 else 1), y0 + 22, str(d), 6)
        pg.draw_line((xx, y0), (xx, y0 + 26 + 18 * len(filas)), width=0.3)
    yy = y0 + 26
    neto = 0
    esp = []
    for desc, hora, seg, dias, cu in filas:
        n = sum(dias.values())
        v = round(n * cu, 2)
        neto += v
        texto(pg, 33, yy + 12, desc, 7)
        texto(pg, 178, yy + 12, hora, 7)
        texto(pg, 240, yy + 12, str(seg), 7)
        for d, k in dias.items():
            texto(pg, xd + (d - 1) * ad + 3, yy + 12, str(k), 7)
        texto(pg, fin + 10, yy + 12, str(n), 7)
        texto(pg, fin + 40, yy + 12, money(cu), 7)
        texto(pg, fin + 100, yy + 12, money(v), 7)
        pg.draw_line((30, yy + 18), (fin + 150, yy + 18), width=0.3)
        esp.append({"total_avisos": n, "costo_unitario": cu, "costo_total": v, "duracion": float(seg), "horario": hora, "dias": dias})
        yy += 18
    caja(pg, 30, y0 + 26, fin + 150, yy)
    igv = round(neto * 0.18, 2)
    tot = round(neto + igv, 2)
    for i, (a, b) in enumerate([("Neto", neto), ("IGV", igv), ("Total", tot)]):
        caja(pg, 600, yy + 20 + 18 * i, 760, yy + 38 + 18 * i)
        texto(pg, 604, yy + 33 + 18 * i, a, 8, True)
        texto(pg, 690, yy + 33 + 18 * i, money(b), 8)
    esperado = {"agencia": ag, "agencia_ruc": ruc_ag, "anunciante": anun, "anunciante_ruc": ruc_an, "numero_orden": "7731",
                "fecha_emision": "2025-06-30", "medio": "RADIO OXIGENO", "periodo_inicio": "2025-07-01",
                "periodo_fin": "2025-07-14", "neto": round(neto, 2), "igv": igv, "total": tot, "filas": esp}
    return doc, esperado


def main():
    DESTINO.mkdir(parents=True, exist_ok=True)
    for nombre, f in (("andina_media", formato_andina), ("nexo_publicidad", formato_nexo), ("punto_medios", formato_punto)):
        doc, esp = f()
        doc.save(DESTINO / f"{nombre}.pdf")
        (DESTINO / f"{nombre}.esperado.json").write_text(json.dumps(esp, ensure_ascii=False, indent=1), encoding="utf-8")
        # versión escaneada a 200 DPI
        pix = doc[0].get_pixmap(dpi=200)
        pix.save(str(DESTINO / f"{nombre}__escaneo.png"))
        print("ok", nombre)


if __name__ == "__main__":
    main()
