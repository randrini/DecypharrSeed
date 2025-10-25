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

# Add an entrypoint that handles PUID/PGID and ownership at container start
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Create a non-root user with default uid/gid 10001 (can be changed at runtime)
RUN set -eux; \
    groupadd -g 10001 app 2>/dev/null || true; \
    useradd -u 10001 -g 10001 -m -s /usr/sbin/nologin app 2>/dev/null || true; \
    mkdir -p /data

# Default runtime env (override at runtime if needed)
ENV MCC_PORT=8069 \
    MCC_HOST=0.0.0.0 \
    MCC_DB=/data/magnet_cc.sqlite \
    MCC_WAITRESS=1 \
    PUID=10001 \
    PGID=10001

EXPOSE 8069

# Keep image starting as root so the entrypoint can change uid/gid and chown mounts.
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python","/app/app.py"]
