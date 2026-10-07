# One-shot migration job: dbmate (schema) + psql (roles and grants).
# Tag must be confirmed by pulling: docker pull amacneil/dbmate:2.21.0
FROM amacneil/dbmate:2.21.0

RUN apk add --no-cache postgresql-client

COPY migrate.sh /usr/local/bin/migrate.sh

# db/ is mounted read-only at /db by compose
ENTRYPOINT ["/bin/sh", "/usr/local/bin/migrate.sh"]
