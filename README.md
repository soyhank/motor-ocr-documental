# Motor OCR Documental Flexible

Implementación de `Motor_OCR_Documental_Flexible_Especificacion.docx` para **órdenes de publicidad de agencias** dirigidas a Grupo RPP.

Sin LLM en producción: el pipeline es determinista (ingesta → preproceso → OCR posicional → normalización → identificación por puntaje → extracción por anclas y tablas → validación) y cada formato de agencia es una plantilla JSON.

## Uso

```bash
pip install -r requirements.txt

# procesar archivos o carpetas (PDF, JPG/PNG/TIFF y, opcional, el audio del spot)
python -m motor procesar muestras/ --salida salida

# interfaz web local: subir órdenes, ver cada dato resaltado sobre el documento, descargar Excel
python -m motor web          # http://127.0.0.1:8765

# pruebas de regresión con los 14 PDF de muestra (valores verificados a mano)
python -m pytest tests -q
```

La salida trae un JSON por documento (formato de la sección 7 de la especificación: valor, tag, página, texto crudo y bbox de cada campo) y un Excel con las hojas Órdenes, Detalle, Programación (un renglón por día) y Validaciones.

## Plantillas incluidas (`plantillas/`)

| Plantilla | Formato | Muestras |
|---|---|---|
| `ORDEN_FORMATO_DIVISION` | Havas+, Especialidades Digitales (División, Proveedor real, Condición de pago) | 6 |
| `ORDEN_FORMATO_SRES_PLAN` | Funell Media / Quiroga, Media Dialogue (Sres., Plan – Presupuesto) | 3 |
| `ORDEN_INTERPUBLIC_INITIATIVE` | Interpublic / Initiative (Programa, Tema, Seg, N° Spots) | 3 |
| `ORDEN_RESET_SEMCO` | Reset Comunicaciones, sistema semco | 1 |
| `ORDEN_CHEIL_EXCEL` | Cheil Perú, exportado desde Excel, con paquetes de auspicio | 1 |

Las plantillas se recargan al guardarlas, sin reiniciar. Las reglas aritméticas comunes están en `plantillas/_reglas_base/ORDEN_PUBLICIDAD.json`.

## Qué se valida

- Aritmética: avisos por día = total de la fila; avisos × costo unitario = costo de la fila; suma de filas = costo/neto; neto × IGV; neto + IGV = total; totales diarios del pie.
- Negocio: RUC con dígito verificador SUNAT; orden emitida a Grupo RPP (`config.json`); los días de la semana del cuadro corresponden al mes; avisos dentro del periodo; pauta dentro de las fechas de campaña; orden emitida antes del primer aviso; horario en cada fila; bonificación declarada («Bonificados del 20 %»).
- Lote: archivos u órdenes duplicadas; duración del audio del spot frente a la duración pautada (por nombre del archivo y motivo).

## Formato nuevo (auto-calibración offline)

Si ninguna plantilla llega al umbral, el documento queda como `TEMPLATE_NOT_IDENTIFIED` y su volcado espacial se guarda en `cola_aprendizaje/`.

```bash
python -m motor calibrar nueva_orden.pdf --id ORDEN_AGENCIA_X   # volcado + borrador heurístico + prompt
# (opcional, fuera de producción) afinar el borrador con un asistente usando el prompt
python -m motor probar cola_aprendizaje/nueva_orden.borrador.json nueva_orden.pdf --promover
```

`probar` corre la plantilla en sandbox y solo la promueve a `plantillas/` si todas las validaciones de nivel ERROR se cumplen.

## Escaneados

Las páginas sin texto se rasterizan a ~200-300 DPI, se enderezan (Hough), se corrige la iluminación, se usa Sauvola para detectar y borrar las líneas de la tabla, y se leen con RapidOCR (ONNX, local). Las palabras se separan por los huecos reales de tinta y la grilla de días se relee celda por celda. Si el OCR se equivoca en un número, las validaciones aritméticas lo marcan como «No cuadra» para revisión.
