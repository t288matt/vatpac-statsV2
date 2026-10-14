#!/bin/sh
# Prints the image mode the Makefile and compose would use (issue 173): local | registry | partial
#   sh ops/image_mode.sh [ENV_FILE]      (default: .env)
# VATPAC_IMAGE_PREFIX and VATPAC_IMAGE_TAG count as set when non-empty in the environment or (last assignment wins, as in
# compose) in ENV_FILE. local = neither, registry = both, partial = exactly one. Only the words above are printed, never a value.
set -u

ENV_FILE="${1:-.env}"

is_set() {  # is_set <NAME>
  eval "v=\${$1:-}"
  if [ -z "$v" ] && [ -f "$ENV_FILE" ]; then
    v="$(sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1 | tr -d '\r')"
  fi
  [ -n "$v" ]
}

p=0; t=0
is_set VATPAC_IMAGE_PREFIX && p=1
is_set VATPAC_IMAGE_TAG && t=1
if [ "$p$t" = 11 ]; then echo registry
elif [ "$p$t" = 00 ]; then echo local
else echo partial; fi
