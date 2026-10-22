#!/bin/sh
# Prints the image mode the Makefile and compose would use (issue 173): local | registry
#   sh ops/image_mode.sh [ENV_FILE]      (default: .env)
# registry = VATPAC_IMAGE_PREFIX is non-empty in the environment or (last assignment wins, as in compose) in ENV_FILE; local =
# it is not. VATPAC_IMAGE_TAG is an optional override: unset, the tag is `latest`. In registry mode, a tag that is a sha-<7> pin
# prints a loud WARNING on stderr (the pin stops `make deploy` following new releases). Only the words above are printed, never a value.
# Sourced with IMAGE_MODE_SOURCE_ONLY=1 (by ops/check_env.sh and ops/deploy.sh) it only defines sha_pin_warning, the one wording of
# that warning (issue 213); ops/check_pulled_images.sh reports a different situation (a pin that differs from the checkout) in its own words.

sha_pin_warning() {
  echo "WARNING: VATPAC_IMAGE_TAG is a sha- pin, so the images do not follow new releases and make deploy will NOT move to the new images. Delete that line unless this is a deliberate rollback."
}

val() {  # val <NAME>: the value from the environment, else from ENV_FILE
  eval "v=\${$1:-}"
  if [ -z "$v" ] && [ -f "$ENV_FILE" ]; then
    v="$(env_key "$ENV_FILE" "$1")"
  fi
  printf '%s' "$v"
}

image_mode_main() {
  ENV_FILE="${1:-.env}"
  . "$(dirname "$0")/env_key.sh"   # env_key FILE NAME: the one reader of .env keys (issue 212)
  if [ -z "$(val VATPAC_IMAGE_PREFIX)" ]; then echo local; return 0; fi
  case "$(val VATPAC_IMAGE_TAG)" in
    sha-*) sha_pin_warning >&2 ;;
  esac
  echo registry
}

if [ -z "${IMAGE_MODE_SOURCE_ONLY:-}" ]; then set -u; image_mode_main "$@"; fi
