#!/bin/sh
# Prints the image mode the Makefile and compose would use (issue 173): local | registry
#   sh ops/image_mode.sh [ENV_FILE]      (default: .env)
# registry = VATPAC_IMAGE_PREFIX is non-empty in the environment or (last assignment wins, as in compose) in ENV_FILE; local =
# it is not. VATPAC_IMAGE_TAG is an optional override: unset, the tag is `latest`. In registry mode, a tag that is a sha-<7> pin
# prints a loud WARNING on stderr (the pin stops `make deploy` following new releases). Only the words above are printed, never a value.
set -u

ENV_FILE="${1:-.env}"
. "$(dirname "$0")/env_key.sh"   # env_key FILE NAME: the one reader of .env keys (issue 212)

val() {  # val <NAME>: the value from the environment, else from ENV_FILE
  eval "v=\${$1:-}"
  if [ -z "$v" ] && [ -f "$ENV_FILE" ]; then
    v="$(env_key "$ENV_FILE" "$1")"
  fi
  printf '%s' "$v"
}

if [ -z "$(val VATPAC_IMAGE_PREFIX)" ]; then echo local; exit 0; fi
case "$(val VATPAC_IMAGE_TAG)" in
  sha-*) echo "WARNING: VATPAC_IMAGE_TAG is a sha- pin, so the images do not follow new releases. Delete that line unless this is a deliberate rollback." >&2 ;;
esac
echo registry
