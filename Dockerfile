FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY app.py /app/app.py

# Pin libs for reproducibility
RUN pip install --no-cache-dir \
      flask==3.0.3 \
      qbittorrent-api==2025.7.0 \
      pyyaml==6.0.2 \
      python-dotenv==1.1.1 \
      waitress==3.0.0 \
 && useradd -u 10001 -m app \
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
  python -c "import urllib.request,os,sys; port=os.environ.get('MCC_PORT','8069'); url=f'http://127.0.0.1:{port}/login'; import urllib.error; sys.exit(0 if urllib.request.urlopen(url, timeout=3).status==200 else 1)"

CMD ["python","/app/app.py"]
