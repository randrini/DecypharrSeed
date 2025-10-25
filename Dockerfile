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

# Copy wheels built for the right architecture and install them offline
COPY --from=builder /wheels /wheels
COPY requirements.txt .

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates \
    && python -m pip install --upgrade pip \
    && pip install --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /var/lib/apt/lists/* /wheels

# Application files
COPY app.py /app/app.py

# Create a non-root user and data dir
RUN useradd -u 10001 -m app \
 && mkdir -p /data \
 && chown -R app:app /data /app

# Default runtime env (override at runtime if needed)
ENV MCC_PORT=8069 \
    MCC_HOST=0.0.0.0 \
    MCC_DB=/data/magnet_cc.sqlite \
    MCC_WAITRESS=1

EXPOSE 8069
USER app

# Healthcheck: simple GET /login
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD \
  python -c "import urllib.request,os,sys; port=os.environ.get('MCC_PORT','8069'); url=f'http://127.0.0.1:{port}/login'; import urllib.error; \
try: r = urllib.request.urlopen(url, timeout=3); sys.exit(0 if getattr(r,'status',None) in (200,301,302) or r.getcode()==200 else 1) \
except Exception: sys.exit(1)"

CMD ["python","/app/app.py"]
