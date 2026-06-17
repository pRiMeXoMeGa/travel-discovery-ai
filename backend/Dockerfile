FROM python:3.11-slim

WORKDIR /app

# System deps kept minimal; fastembed pulls onnxruntime (no torch).
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

# Single worker: keeps memory low for Render free (512MB) and avoids duplicate
# embedding-model loads. Scale with more workers on a larger plan.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
