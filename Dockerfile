ARG PYTHON_IMAGE=python:3.12-slim
# Official Playwright image: Chromium plus its system libraries. Keep the tag in
# sync with PLAYWRIGHT_VERSION below.
ARG PLAYWRIGHT_IMAGE=mcr.microsoft.com/playwright/python:v1.64.1-noble

# --- slim: Wildberries and Avito over plain HTTP, no browser -------------------
FROM ${PYTHON_IMAGE} AS slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    SNIFLOT_DB_PATH=/data/sniflot.db SNIFLOT_OZON_ENABLED=false
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install ".[mcp]" && useradd --system --uid 1000 --home /app sniflot \
    && mkdir /data && chown 1000 /data
USER 1000
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
CMD ["sniflot", "serve"]

# --- full (default): + Chromium under Xvfb for Ozon and Avito -----------------
FROM ${PLAYWRIGHT_IMAGE} AS full
ARG PLAYWRIGHT_VERSION=1.64.1
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    SNIFLOT_DB_PATH=/data/sniflot.db
RUN apt-get update && apt-get install -y --no-install-recommends xvfb xauth \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --break-system-packages ".[mcp]" "playwright==${PLAYWRIGHT_VERSION}" \
    && mkdir /data && chown 1000 /data
# A visible browser on a virtual display: Ozon treats headless Chromium more strictly.
ENV SNIFLOT_BROWSER_HEADLESS=false SNIFLOT_AVITO_BACKEND=browser
USER 1000
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
CMD ["xvfb-run", "-a", "--server-args=-screen 0 1366x900x24", "sniflot", "serve"]
