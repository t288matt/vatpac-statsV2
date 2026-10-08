# Collector image. Build context is the REPO ROOT (see docker-compose.yml).
# .dockerignore excludes tests/docs/grafana/ops; config/ is NOT copied: it is mounted
# read-only at /app/config at run time.

# ---- build stage: resolve dependencies into a venv ----
FROM python:3.12-slim AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY collector/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

# ---- runtime stage ----
FROM python:3.12-slim AS runtime
ARG VERSION=dev
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HEALTH_PORT=8080 \
    VATPAC_VERSION=${VERSION}
LABEL org.opencontainers.image.title="vatpac-stats-collector" \
      org.opencontainers.image.version="${VERSION}"

RUN groupadd --system --gid 10001 vatpac \
 && useradd --system --uid 10001 --gid vatpac --no-create-home --shell /usr/sbin/nologin vatpac \
 && mkdir /archive && chown vatpac:vatpac /archive

COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY collector/src /app/src

USER 10001:10001

# curl is absent in slim images; use urllib. /health is 503 until the first successful poll.
HEALTHCHECK --interval=30s --timeout=6s --start-period=40s --retries=3 \
  CMD ["python", "-c", "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('HEALTH_PORT','8080'), timeout=4)"]

ENTRYPOINT ["python", "-m", "vatpac.main"]
