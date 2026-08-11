FROM python:3.11-slim

WORKDIR /app

# System deps kept minimal; fastembed pulls onnxruntime (no torch).
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Where fastembed caches ONNX weights. Set explicitly so the build and the
# runtime agree — otherwise the baked models land in a path the running
# container never looks at, and it silently re-downloads them.
ENV FASTEMBED_CACHE_PATH=/opt/fastembed_cache
# huggingface_hub's threaded downloader drives tqdm from worker threads, which
# blows up here as `'tqdm' object has no attribute '_lock'` and takes the whole
# call with it. Nothing should be downloading at runtime after this stage
# anyway; disabling the bars removes the failure mode entirely.
ENV HF_HUB_DISABLE_PROGRESS_BARS=1

# Bake BOTH models into the image rather than fetching them on first request.
#
#  - bge-small-en-v1.5: the app's own query embedder. Downloading it lazily cost
#    ~55s on the first request, which on Render's free tier lands on the first
#    click after a cold start.
#  - Qdrant/bm25: pulled in automatically by mem0's Qdrant store for hybrid
#    search (`_get_bm25_encoder`, lazy, with no config flag to turn it off).
#    Fetching it at runtime overran app/memory/store.py's 8s write cap, so the
#    memory write timed out and returned nothing on a cold instance.
RUN python -c "\
from fastembed import TextEmbedding, SparseTextEmbedding; \
TextEmbedding('BAAI/bge-small-en-v1.5'); \
SparseTextEmbedding('Qdrant/bm25'); \
print('embedding models baked')"

COPY app ./app

EXPOSE 8000

# Single worker: keeps memory low for Render free (512MB) and avoids duplicate
# embedding-model loads. Scale with more workers on a larger plan.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
