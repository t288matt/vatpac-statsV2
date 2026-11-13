# One-shot migration job: dbmate (schema) + psql (roles and grants).
# Floating major tag (ADR-032): small updates arrive with `docker compose build --pull`; a new major is a deliberate edit.
FROM amacneil/dbmate:2

RUN apk add --no-cache postgresql-client

# Build context is the repository root (issue 173, P1): db/ is baked into the image so the migrations always match the
# code the image was built from. Dockerfile.dockerignore (next to this file) limits the context to db/ and migrate.sh.
COPY ops/migrate/migrate.sh /usr/local/bin/migrate.sh
COPY db /db

# Nothing is mounted over /db: the image is the only carrier of the migrations and roles.sql, so the files that run are the ones this
# image was built with, in local-build and registry mode alike. After editing a migration, rebuild: `docker compose build migrate`.

ENTRYPOINT ["/bin/sh", "/usr/local/bin/migrate.sh"]
