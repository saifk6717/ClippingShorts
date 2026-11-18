# ClippingShorts backend image: the API and all three workers run from this one image with different commands,
# so the Python code, FFmpeg and fonts are identical everywhere (and the layers are stored once).
FROM python:3.12-slim

ARG INSTALL_PARAKEET=0
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 \
    CLIPPINGSHORTS_DATA_DIR=/data/clippingshorts

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates tini fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*
# yt-dlp needs a JavaScript runtime for YouTube
COPY --from=denoland/deno:bin-2.4.5 /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt requirements-parakeet.txt ./
RUN pip install -r requirements.txt \
 && if [ "$INSTALL_PARAKEET" = "1" ]; then pip install -r requirements-parakeet.txt; fi

RUN useradd --create-home --uid 10001 clippingshorts \
 && mkdir -p /data/clippingshorts && chown -R clippingshorts /data
COPY clippingshorts clippingshorts
USER clippingshorts

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "clippingshorts.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
