# Multi-stage, multi-platform Dockerfile
# Build stage: build wheels for the target platform
ARG TARGETPLATFORM
ARG TARGETARCH
FROM --platform=${TARGETPLATFORM:-linux/amd64} python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=off

WORKDIR /wheels

# Use a pinned requirements.txt in repo root
COPY requirements.txt .

# Install build deps so wheels can be built for target arch, then build wheels
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      build-essential \
      gcc \
      libffi-dev \
      libssl-dev \
      ca-certificates \
      python3-dev \
    && python -m pip install --upgrade pip setuptools wheel \
    && pip wheel --wheel-dir=/wheels -r requirements.txt \
    && rm -rf /var/lib/apt/lists/*

# Runtime stage: lightweight image with only runtime deps and app code
FROM --platform=${TARGETPLATFORM:-linux/amd64} python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY app.py /app/app.py
COPY entrypoint.sh /app/entrypoint.sh

# Pin libs for reproducibility
RUN pip install --no-cache-dir \
      flask==3.0.3 \
      qbittorrent-api==2025.7.0 \
      pyyaml==6.0.2 \
      python-dotenv==1.1.1 \
      waitress==3.0.0 \
 && chmod +x /app/entrypoint.sh \
 && mkdir -p /data

# Default PUID/PGID (can be overridden at runtime)
ENV PUID=10001 \
    PGID=10001

# Default runtime env (override at runtime if needed)
ENV MCC_PORT=8069 \
    MCC_HOST=0.0.0.0 \
    MCC_DB=/data/magnet_cc.sqlite \
    MCC_WAITRESS=1 \
    PUID=10001 \
    PGID=10001

EXPOSE 8069

# Healthcheck: simple GET /login
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD \
  python -c "import urllib.request,os,sys; port=os.environ.get('MCC_PORT','8069'); url=f'http://127.0.0.1:{port}/login'; import urllib.error; sys.exit(0 if urllib.request.urlopen(url, timeout=3).status==200 else 1)"

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python","/app/app.py"]
