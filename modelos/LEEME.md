# Modelos de OCR

- `PP-OCRv6_rec_medium.onnx`: reconocedor de texto PP-OCRv6 «medium» multilingüe (incluye castellano con tildes y Ñ), de PaddleOCR (licencia Apache 2.0), convertido a ONNX por el proyecto RapidOCR. Origen: https://www.modelscope.cn/models/RapidAI/RapidOCR (v3.10.0, `onnx/PP-OCRv6/rec/PP-OCRv6_rec_medium.onnx`, SHA256 eef444829dbbe18d7fea59a3f6eb75647518d2b3a9568d27c92e42940204894b).

El detector de renglones es el PP-OCRv4 que trae `rapidocr-onnxruntime`. Para probar otro modelo sin tocar el código: variables `OCR_DET_MODEL` y `OCR_REC_MODEL` con la ruta del `.onnx`.
