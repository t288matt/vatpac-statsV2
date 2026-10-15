# One-shot migration job: dbmate (schema) + psql (roles and grants).
# Floating major tag (ADR-032): small updates arrive with `docker compose build --pull`; a new major is a deliberate edit.
FROM amacneil/dbmate:2

RUN apk add --no-cache postgresql-client

# Build context is the repository root (issue 173, P1): db/ is baked into the image so the migrations always match the
# code the image was built from. Dockerfile.dockerignore (next to this file) limits the context to db/ and migrate.sh.
COPY ops/migrate/migrate.sh /usr/local/bin/migrate.sh
COPY db /db

# docker-compose.yml still mounts ./db read-only over /db, so the checkout's migrations win over the baked copy in both
# local-build and registry mode (the mount is removed in a later phase of issue 173). The baked copy matters for
# release images pulled from the registry, which must carry the migrations of the commit they were built from.

ENTRYPOINT ["/bin/sh", "/usr/local/bin/migrate.sh"]
