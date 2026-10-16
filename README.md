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

Las páginas sin texto se rasterizan a ~300 DPI, se enderezan (Hough), se corrige la iluminación, se usa Sauvola para detectar y borrar las líneas de la tabla, y se leen con RapidOCR (ONNX, local) por franjas, llamando por separado al detector y al reconocedor. Las palabras se separan por los huecos reales de tinta (también en cursiva), las etiquetas pegadas a su valor se parten (`RUC:20566514622`), las confusiones típicas se corrigen (`PERI0DO`, `6O`), las columnas de la grilla de días se toman de las líneas verticales de la tabla y cada celda numérica se relee ampliada. Si aun así un número sale mal, las validaciones aritméticas marcan la orden como «No cuadra» para revisión.

Medición con `_scripts/generar_escaneos.py` + `_scripts/benchmark_escaneos.py` (18 escaneos simulados de 6 órdenes, 428 campos por variante):

| Variante | Condiciones | Campos exactos |
|---|---|---|
| limpio | 300 DPI, sin inclinación | ≈ 98 % |
| normal | 200 DPI, 1° de inclinación, sombra y ruido | ≈ 95 % |
| malo | 150 DPI, 2,5°, sombra fuerte, ruido y desenfoque | ≈ 50 % (no apto) |

Lo que falla en limpio/normal son casi siempre espacios dentro de nombres («CHEILPERU»); los números, fechas y la grilla de días salen bien. Para escaneos reales conviene 200 DPI o más.

## Despliegue

En producción: **https://motor-ocr-documental.vercel.app** (proyecto `motor-ocr-documental` de Vercel, Python 3.12, entrada `main.py`, `maxDuration` 300 s, funciones grandes activadas porque el paquete con OCR pasa de 500 MB).

La interfaz web no guarda estado en el servidor: el navegador conserva los resultados y las imágenes, y las revisiones entre documentos (duplicados, audios) y el Excel se piden con los resultados ya extraídos. Por eso funciona igual en local y en Vercel. Límites de Vercel: 4,5 MB por petición (el navegador sube los archivos por tandas y convierte a JPEG las imágenes escaneadas de más de 3 MB) y unos 30-40 s de OCR por página escaneada; un PDF digital tarda uno o dos segundos.

Para desplegar una versión nueva: `vercel deploy --prod --yes --scope soyhanks-projects` desde una copia sin `.git` (el plan Hobby bloquea los despliegues desde Git cuando el autor del commit no es el dueño del proyecto).
