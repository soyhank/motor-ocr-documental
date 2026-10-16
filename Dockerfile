# Servidor propio (cualquier VPS, Render, Railway, Fly.io, Cloud Run...):
#   docker build -t motor-ocr .
#   docker run -p 8765:8765 motor-ocr      ->  http://localhost:8765
# Sin el límite de 4,5 MB por envío ni el de 300 s por petición de Vercel.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1 PORT=8765
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY motor ./motor
COPY web ./web
COPY plantillas ./plantillas
COPY modelos ./modelos
COPY muestras ./muestras
COPY config.json catalogo.json ./
EXPOSE 8765
CMD ["sh", "-c", "python -m motor web --host 0.0.0.0 --puerto ${PORT}"]
