# Backup sidecar: pg_dump client (same major/minor as the server) plus a small scheduler loop.
# Tag must match the db service image in docker-compose.yml.
FROM postgres:16.4-bookworm

COPY backup.sh restore.sh restore-test.sh /usr/local/bin/
RUN chmod 0755 /usr/local/bin/backup.sh /usr/local/bin/restore.sh /usr/local/bin/restore-test.sh \
 && mkdir -p /backups && chown postgres:postgres /backups

# The base image's entrypoint starts a server; replace it.
USER postgres
ENTRYPOINT []
CMD ["/usr/local/bin/backup.sh", "loop"]
