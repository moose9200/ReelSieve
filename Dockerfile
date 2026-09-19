# ReelSieve by Braivex — FastAPI + ffmpeg + Playwright (reviews) + Depth-Anything (parallax)
FROM mcr.microsoft.com/playwright/python:v1.50.0-jammy
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 HF_HOME=/data/hf-cache TRANSFORMERS_OFFLINE=0
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY requirements.txt .
RUN pip install --extra-index-url https://download.pytorch.org/whl/cpu -r requirements.txt && python -m playwright install chromium
COPY app ./app
COPY tests ./tests
COPY README.md main.py ./
ENV JOBS_DIR=/data/jobs GDRIVE_TOKEN_PATH=/data/google-token.json AUTH_PATH=/data/auth.json PORT=8787
EXPOSE 8787
CMD ["sh","-c","uvicorn app.server:app --host 0.0.0.0 --port ${PORT:-8787}"]
