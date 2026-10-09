# One-shot migration job: dbmate (schema) + psql (roles and grants).
# Tag must be confirmed by pulling: docker pull amacneil/dbmate:2.21.0
# Pinned by tag; digest pinning (FROM image:tag@sha256:...) is the stricter policy once digests can be
# resolved (docker buildx imagetools inspect). Dependabot's docker ecosystem tracks this file.
FROM amacneil/dbmate:2.36.0

RUN apk add --no-cache postgresql-client

COPY migrate.sh /usr/local/bin/migrate.sh

# db/ is mounted read-only at /db by compose
ENTRYPOINT ["/bin/sh", "/usr/local/bin/migrate.sh"]
