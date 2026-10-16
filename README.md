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

## Escaneados y fotos

Las páginas sin texto se rasterizan a ~300 DPI y pasan por:

1. **Rectificación de fotos**: si se ve el borde de la hoja sobre un fondo (foto de celular), se ubican sus cuatro bordes y se corrige la perspectiva. En un escaneo no se toca nada.
2. Enderezado (Hough), corrección de iluminación, Sauvola y borrado de las líneas de la tabla.
3. **OCR**: detector PP-OCRv4 y reconocedor **PP-OCRv6 medium multilingüe** (`modelos/`, lee tildes y Ñ), por franjas. Las cajas que unen dos renglones (la cabecera de la grilla de días) se parten por los huecos de tinta.
4. Limpieza: palabras separadas por los huecos reales, etiquetas pegadas partidas (`RUC:20566514622`), confusiones típicas (`PERI0DO`, `OOOOOO85`), caracteres no latinos y los «:» mal leídos se descartan.
5. Columnas de la grilla tomadas de las líneas verticales y relectura ampliada de cada celda numérica.
6. **Datos maestros** (`catalogo.json`): agencias, divisiones, nombres de Grupo RPP, medios, ciudades, anunciantes y condiciones de pago. Si un nombre leído coincide con uno conocido salvo espacios, tildes o puntuación (o difiere en un carácter en nombres largos), se usa la forma exacta; el valor leído queda en `ocrValue`. Cada agencia o anunciante nuevo se agrega a ese archivo.

Si aun así un número sale mal, las validaciones aritméticas marcan la orden como «No cuadra» para revisión.

### Medición

`_scripts/generar_escaneos.py` crea 5 variantes de cada una de las 14 órdenes (70 imágenes) y `_scripts/benchmark_escaneos.py` compara campo por campo con el PDF digital. Las 6 órdenes que se usaron para afinar el motor se informan aparte de las otras 8, que se midieron «a ciegas».

| Variante | Condiciones | Todas | A ciegas |
|---|---|---|---|
| limpio | 300 DPI | 100 % | 100 % |
| normal | 200 DPI, 1°, sombra y ruido | 100 % | 100 % |
| oficina | 200 DPI, JPEG calidad 55, 0,6°, algo borroso | 100 % | 100 % |
| celular | foto: perspectiva, luz desigual, tono cálido, JPEG 70 | 100 % | 100 % |
| malo | 150 DPI, 2,5°, sombra fuerte, ruido y desenfoque | 88,8 % | 100 % |

Cada variante tiene 846 datos. En «malo» fallan dos órdenes (Samsung y 040360) y las dos quedan marcadas «No cuadra». Antes de este cambio (reconocedor PP-OCRv4 móvil) las cifras eran 98,6 % limpio, 96,7 % normal y ≈ 50 % malo. Son escaneos simulados: con escaneos reales de las agencias conviene volver a medir. Para escanear, 200 DPI o más.

```bash
python _scripts/generar_escaneos.py          # crea las imágenes que falten en tests/escaneos/
python _scripts/benchmark_escaneos.py --variantes=normal,celular --detalle
```

## Despliegue

El motor rinde mejor en un equipo propio: el reconocedor grande tarda unos 45 s por página escaneada en una PC de escritorio de 12 núcleos y no hay límite de tamaño ni de tiempo por envío.

**En esta PC (Windows)**: doble clic en `INICIAR.bat` (la primera vez instala las dependencias) y se abre http://127.0.0.1:8765. Con `INICIAR.bat red` también lo pueden usar otros equipos de la misma red entrando a `http://IP-de-esta-PC:8765`.

**En un servidor propio** (VPS, Render, Railway, Fly.io, Cloud Run…) con Docker:

```bash
docker build -t motor-ocr .
docker run -p 8765:8765 motor-ocr
```

**Vercel** (https://motor-ocr-documental.vercel.app): funciona, pero con 1 vCPU un escaneado tarda 2 a 3 minutos y hay un límite de 300 s y 4,5 MB por envío; sirve para PDF digitales y escaneos de una página. Se despliega con `vercel deploy --prod --yes --scope soyhanks-projects` desde una copia sin `.git`.

La interfaz web no guarda estado en el servidor: el navegador conserva los resultados y las imágenes, y las revisiones entre documentos y el Excel se piden con los resultados ya extraídos. Cada escaneado se envía solo; los PDF digitales van de a cuatro.
