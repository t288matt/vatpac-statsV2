#!/bin/sh
# Stops `make deploy` before `up` when the pulled images do not match the checkout (issue 173).
#   sh ops/check_pulled_images.sh        (run after `docker compose pull`, from the repo root)
# For each pulled image (collector, watchdog, backup, migrate) the revision label must be HEAD, or an ancestor of HEAD with no
# change since to anything baked into the images (so a docs-only commit after the release is fine). Reads only; never builds.
# Exit 0 = all match; exit 1 = otherwise, with a plain message saying which case.
set -u

# paths copied into the four images (collector/Dockerfile, ops/watchdog, ops/backup, ops/migrate and db). The images carry the code AND the
# migrations (nothing is mounted over them), so a change to any of these since the release label means the pulled image is not this commit.
BAKED="collector ops/watchdog ops/backup ops/migrate ops/audit/independent.py ops/audit/invariants.sql ops/audit/identity_flap.sql db"

head="$(git rev-parse HEAD 2>/dev/null)" || head=""
if [ -z "$head" ]; then echo "check_pulled_images: cannot read the checkout commit" >&2; exit 1; fi

unreadable=""; stale=""; pinned=""
for name in collector watchdog backup migrate; do
  img="$(docker compose config --images 2>/dev/null | grep "vatpac-stats-$name:" | head -n 1)"
  rev="$(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$img" 2>/dev/null | tr -d '\r')"
  case "$rev" in
    ''|*[!0-9a-f]*) unreadable="$unreadable $name"; continue ;;
  esac
  [ "${#rev}" -ge 7 ] || { unreadable="$unreadable $name"; continue; }
  case "$head" in "$rev"*) continue ;; esac
  # older release: fine only if it is an ancestor and nothing baked into the images changed since
  # shellcheck disable=SC2086
  if git merge-base --is-ancestor "$rev" "$head" 2>/dev/null && git diff --quiet "$rev" "$head" -- $BAKED 2>/dev/null; then continue; fi
  case "${img##*:}" in sha-*) pinned="$pinned $name" ;; *) stale="$stale $name" ;; esac
done

if [ -n "$unreadable" ]; then
  echo "could not read a usable revision label for:$unreadable (docker not answering, image not pulled, or image without the label); nothing was started" >&2
  exit 1
fi
if [ -n "$pinned" ]; then
  echo "VATPAC_IMAGE_TAG pins a sha- tag that differs from this checkout for:$pinned. Delete that line from .env (or check out the pinned commit); nothing was started" >&2
  exit 1
fi
if [ -n "$stale" ]; then
  echo "the images for this commit are not published yet: run the release workflow for $head, then run make deploy again" >&2
  echo "(nothing was started or changed; the running stack is untouched)" >&2
  exit 1
fi
echo "check_pulled_images: OK (pulled images match the checkout $(printf '%s' "$head" | cut -c1-10))"
