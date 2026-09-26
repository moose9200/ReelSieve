# ReelSieve by Braivex — FastAPI + render worker (ffmpeg, Playwright for reviews, Depth-Anything for parallax)
FROM mcr.microsoft.com/playwright/python:v1.50.0-jammy
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 HF_HOME=/opt/hf-cache RENDER_TMP_DIR=/tmp/reelsieve PORT=8787
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY requirements.txt .
RUN pip install --extra-index-url https://download.pytorch.org/whl/cpu -r requirements.txt && python -m playwright install chromium
# The depth model is a software artifact: bake it in so rendering never depends on a volume or a download.
RUN python -c "from huggingface_hub import snapshot_download; snapshot_download('depth-anything/Depth-Anything-V2-Small-hf')"
COPY app ./app
EXPOSE 8787
CMD ["python", "-m", "app.start"]
