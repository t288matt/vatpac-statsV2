# ATC stats loader image (issue 387). Build context is stats_loader/ (see docker-compose.yml); tests are not copied.
# Python 3.14, the same as the collector and watchdog (ADR-033): the tested Python is the shipped Python.

# ---- build stage: resolve dependencies into a venv ----
FROM python:3.14-slim AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

# ---- runtime stage ----
FROM python:3.14-slim AS runtime
ARG VERSION=dev
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    VATPAC_VERSION=${VERSION}
LABEL org.opencontainers.image.title="vatpac-stats-loader" \
      org.opencontainers.image.version="${VERSION}"

RUN groupadd --system --gid 10001 vatpac \
 && useradd --system --uid 10001 --gid vatpac --no-create-home --shell /usr/sbin/nologin vatpac

COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY src /app/src

USER 10001:10001

# No port: the healthcheck reads the run log from the database (the compose file sets the interval and the start period).
HEALTHCHECK --interval=5m --timeout=30s --start-period=30m --retries=3 CMD ["python", "-m", "vatsim_stats.app", "health"]

ENTRYPOINT ["python", "-m", "vatsim_stats.app"]
