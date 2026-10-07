# Backup sidecar: pg_dump client (same major/minor as the server) plus a small scheduler loop.
# Tag must match the db service image in docker-compose.yml.
# Pinned by tag; digest pinning (FROM image:tag@sha256:...) is the stricter policy once digests can be
# resolved (docker buildx imagetools inspect). Dependabot's docker ecosystem tracks this file.
FROM postgres:18.4-bookworm

# Optional extra tools for the BACKUP_REMOTE hook, e.g. EXTRA_APT_PACKAGES="rclone" (empty = none).
ARG EXTRA_APT_PACKAGES=""
RUN if [ -n "$EXTRA_APT_PACKAGES" ]; then \
      apt-get update && apt-get install -y --no-install-recommends $EXTRA_APT_PACKAGES \
      && rm -rf /var/lib/apt/lists/*; \
    fi

COPY backup.sh restore.sh restore-test.sh restore-lib.sh /usr/local/bin/
RUN chmod 0755 /usr/local/bin/backup.sh /usr/local/bin/restore.sh /usr/local/bin/restore-test.sh \
 && chmod 0644 /usr/local/bin/restore-lib.sh \
 && mkdir -p /backups && chown postgres:postgres /backups

# The base image's entrypoint starts a server; replace it.
USER postgres
ENTRYPOINT []
CMD ["/usr/local/bin/backup.sh", "loop"]
