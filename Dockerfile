"""Minimal production image for the Forex Platform control plane.

The MT5 gateway is intentionally a separate Windows service because MetaTrader5
runs on Windows. The Linux control plane only needs Python and the signed HTTP
client.
"""
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
RUN addgroup --system forex && adduser --system --ingroup forex forex

COPY pyproject.toml README.md ./
COPY forex_platform ./forex_platform
RUN pip install --no-cache-dir .

RUN mkdir -p /var/lib/forex-platform /etc/forex-platform && \
    chown -R forex:forex /var/lib/forex-platform /etc/forex-platform
USER forex

EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=3)"

ENTRYPOINT ["forex-platform", "serve-production"]
