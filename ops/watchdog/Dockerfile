# Base image pinned by tag; digest pinning (FROM python:3.12-slim@sha256:...) is the stricter policy once
# digests can be resolved (docker buildx imagetools inspect). Dependabot's docker ecosystem tracks this.
FROM python:3.12-slim
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
ARG VERSION=dev
LABEL org.opencontainers.image.title="vatpac-stats-watchdog" \
      org.opencontainers.image.version="${VERSION}"

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

RUN groupadd --system --gid 10001 watchdog \
 && useradd --system --uid 10001 --gid watchdog --no-create-home --shell /usr/sbin/nologin watchdog \
 && mkdir -p /var/watchdog && chown watchdog:watchdog /var/watchdog

COPY watchdog.py .
USER 10001:10001
ENTRYPOINT ["python", "/app/watchdog.py"]
