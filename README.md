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
python -m pytest tests -q          # 27 pruebas
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

## Formato nuevo: se aprende solo

Si ninguna plantilla llega al umbral, la orden no se descarta (`motor/generica.py`):

1. Se lee con una **plantilla genérica de sinónimos** (`ORDEN N°`, `N° de Orden`, `Fecha de emisión`, `Señores`, `Atención`, `Tarifa`, `Valor venta`, `I.G.V.`, `Importe total`…), también cuando las etiquetas van encima del valor (recuadros). Para la tabla se prueban varias formas de leer las filas y para los totales todas las combinaciones de candidatos: gana la que hace cuadrar la aritmética.
2. Los roles se resuelven con datos conocidos: el RUC y los nombres de Grupo RPP marcan al proveedor, `catalogo.json` y el membrete (el texto más grande de arriba) a la agencia, y cada RUC se asigna al nombre más cercano.
3. **Si las validaciones cuadran**, se guarda una plantilla propia de esa agencia (`plantillas/AUTO_<AGENCIA>.v1.json`, con `"origin": "auto"`) con las etiquetas exactas que usa y su RUC para reconocerla. La siguiente orden de esa agencia ya se identifica sola.
4. **Si no cuadran**, la orden queda como `NEW_FORMAT_REVIEW` («Formato nuevo: revisar») con lo que se pudo leer. El asesor corrige en pantalla, vuelve a validar y pulsa «Guardar formato»: el motor ubica cada valor corregido en el documento, toma su etiqueta y guarda la plantilla (`POST /api/aprender`).

El aprendizaje automático se apaga con `"aprendizajeAutomatico": false` en `config.json`.

Medición, con formatos que el motor nunca vio (`_scripts/generar_formatos_nuevos.py` crea tres órdenes de agencias inventadas, con otro diseño y otras palabras, en PDF y escaneadas):

| Prueba | Resultado |
|---|---|
| 3 agencias inventadas, PDF y escaneo (`_scripts/evaluar_formatos_nuevos.py`) | 164/164 datos (100 %) |
| Las 14 órdenes reales leídas como si su formato no existiera (`_scripts/evaluar_generica.py`) | 671/671 datos (100 %) |
| Segunda orden de la misma agencia con la plantilla aprendida de la primera | 287/287 datos, reconocida con 98 a 100 puntos |

Las plantillas genéricas se armaron conociendo los 5 formatos reales, así que la prueba exigente es la de las agencias inventadas. El borrador manual sigue disponible:

```bash
python -m motor calibrar nueva_orden.pdf --id ORDEN_AGENCIA_X
python -m motor probar cola_aprendizaje/nueva_orden.borrador.json nueva_orden.pdf --promover
```

## Revisión y corrección en pantalla

En la ficha de cada orden, «Qué revisar» lista los errores con su fila. «Corregir datos» vuelve editables la cabecera, los totales, las filas y cada día de la pauta; «Validar de nuevo» recalcula todas las reglas (`POST /api/revalidar`). Cada corrección queda en `correcciones` con el valor leído y el final, y la lista muestra la orden como «corregida».

## Salida para otros sistemas

El JSON limpio (`motor/salida.py`) trae solo lo necesario:

```json
{
  "documento": {"id": "DOC-20261016-153316BB", "archivo": "Orden_Nº_040176-2025-RADIO_OXIGENO-NIVEL_NACIONAL.PDF",
                "estado": "COMPLETED", "estadoTexto": "Correcto", "plantilla": "ORDEN_FORMATO_DIVISION",
                "formatoNuevo": false, "origen": "digital"},
  "cabecera": {"numero_orden": "040176", "fecha_emision": "2025-04-03", "agencia": "HAVAS+ S.A.C.", "...": "..."},
  "detalle": [{"item": 2, "horario": "06:00-23:59", "duracion": 20.0, "total_avisos": 104, "costo_unitario": 109.1,
               "costo_total": 11346.4, "programacion": [{"fecha": "2025-04-07", "dia": 7, "avisos": 8}, "..."]}],
  "totales": {"costo": 11346.4, "recargo": 0.0, "descuento": 0.0, "neto": 11346.4, "igv_pct": 0.18, "igv": 2042.35,
              "total": 13388.75, "moneda": "PEN", "total_avisos": 114},
  "validacion": {"ok": true, "errores": [], "observaciones": []}
}
```

Si alguien corrigió algo, se agrega `"correcciones": [{"campo": "totals.total", "antes": …, "despues": …}]`.

En la web: «Descargar JSON limpio» (una orden) y «JSON limpio (todas)». El JSON completo, con página, texto crudo y recuadro de cada dato, sigue disponible como «JSON completo (trazabilidad)».

## API

Documentación interactiva en `/docs`. Si `MOTOR_API_KEY` (o `config.json -> api.claves`) está definida, cada llamada a `/api/v1` lleva la cabecera `X-API-Key`.

| Método | Ruta | Qué hace |
|---|---|---|
| POST | `/api/v1/ordenes` | Sube un archivo (`archivo`) y devuelve el JSON limpio; `?formato=completo` para la trazabilidad, `?notificar=false` para no avisar |
| POST | `/api/v1/ordenes/validar` | Recibe el JSON limpio corregido y lo devuelve con la validación y el estado recalculados |
| GET | `/api/v1/plantillas` | Formatos que el motor reconoce (incluidos los aprendidos) |
| GET | `/api/v1/salud` | Estado del servicio |

```bash
curl -H "X-API-Key: $MOTOR_API_KEY" -F "archivo=@orden.pdf" http://servidor:8765/api/v1/ordenes
```

## Errores y alertas

Un archivo que no se puede leer vuelve con `estado: ERROR` y un código claro: `VACIO`, `MUY_GRANDE`, `FORMATO_NO_SOPORTADO`, `PDF_DANADO`, `PDF_PROTEGIDO`, `SIN_PAGINAS`, `DEMASIADAS_PAGINAS`, `IMAGEN_DANADA`, `SIN_TEXTO`.

Las órdenes con error, que no cuadran o de formato nuevo por revisar disparan una alerta (`motor/notificar.py`) por webhook (JSON con un campo `text` que entienden Teams y Slack) y por correo SMTP. Se configura en `config.json -> notificaciones` o con variables de entorno (`MOTOR_WEBHOOK_URL`, `MOTOR_SMTP_SERVIDOR`, `MOTOR_SMTP_PUERTO`, `MOTOR_SMTP_USUARIO`, `MOTOR_SMTP_CLAVE`, `MOTOR_SMTP_DE`, `MOTOR_SMTP_PARA`). La clave del correo va solo en una variable de entorno. Cada envío queda en `salida/notificaciones.log`, y `POST /api/notificaciones/prueba` manda una alerta de prueba.

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

**Para que alguien lo pruebe desde afuera, con esta PC como servidor**: doble clic en `PUBLICAR.bat`. Arranca el motor y abre un túnel temporal de Cloudflare (gratis, sin cuenta; descarga una sola vez el `cloudflared.exe` oficial en `herramientas/`). En la ventana aparece una dirección `https://…trycloudflare.com` que se puede mandar a cualquiera; por defecto el acceso es libre. `PUBLICAR.bat MiClave` pide esa contraseña (usuario cualquiera) y la API la usa como `X-API-Key`. La dirección cambia cada vez que se abre y deja de funcionar al cerrar la ventana o apagar la PC. Cloudflare corta las respuestas que tardan más de 100 s: un escaneo de una página tarda unos 45 s, así que conviene subir los escaneos de a uno.

**En un servidor propio** (VPS, Render, Railway, Fly.io, Cloud Run…) con Docker:

```bash
docker build -t motor-ocr .
docker run -p 8765:8765 motor-ocr
```

**Vercel** (https://motor-ocr-documental.vercel.app): funciona, pero con 1 vCPU un escaneado tarda 2 a 3 minutos y hay un límite de 300 s y 4,5 MB por envío; sirve para PDF digitales y escaneos de una página. Se despliega con `vercel deploy --prod --yes --scope soyhanks-projects` desde una copia sin `.git`.

La interfaz web no guarda estado en el servidor: el navegador conserva los resultados y las imágenes, y las revisiones entre documentos y el Excel se piden con los resultados ya extraídos. Cada escaneado se envía solo; los PDF digitales van de a cuatro.
