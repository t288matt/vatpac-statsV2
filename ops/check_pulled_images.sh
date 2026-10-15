#!/bin/sh
# Stops `make deploy` before `up` when the pulled images were not built from the checkout (issue 173).
#   sh ops/check_pulled_images.sh        (run after `docker compose pull`, from the repo root)
# Reads the org.opencontainers.image.revision label of every pulled registry image (collector, watchdog, backup, migrate) and
# compares it with `git rev-parse HEAD`. Exit 0 = all match; exit 1 = a mismatch or an unreadable label. Reads only; never builds.
set -u

head="$(git rev-parse HEAD 2>/dev/null)" || head=""
if [ -z "$head" ]; then echo "check_pulled_images: cannot read the checkout commit" >&2; exit 1; fi

bad=0
for name in collector watchdog backup migrate; do
  img="$(docker compose config --images 2>/dev/null | grep "vatpac-stats-$name:" | head -n 1)"
  rev="$(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$img" 2>/dev/null | tr -d '\r')"
  case "$rev" in
    ''|*[!0-9a-f]*) bad=1 ;;
    *) case "$head" in "$rev"*) ;; *) bad=1 ;; esac
       [ "${#rev}" -ge 7 ] || bad=1 ;;
  esac
done

if [ "$bad" -ne 0 ]; then
  echo "the images for this commit are not published yet: run the release workflow for $head, then run make deploy again" >&2
  echo "(nothing was started or changed; the running stack is untouched)" >&2
  exit 1
fi
echo "check_pulled_images: OK (pulled images were built from $(printf '%s' "$head" | cut -c1-10))"
