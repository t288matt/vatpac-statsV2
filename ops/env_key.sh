# One reader of a key from an env file, for the scripts that run with plain `sh` (ops/smoke.sh, check_env.sh, image_mode.sh) and,
# as text, for the remote reads of ops/deploy.sh (issue 212). Sourced, never run. The python scripts use ops/env_file.py, the same rules.
#
# env_key <FILE> <NAME>: prints the value of NAME in FILE, or nothing (also when FILE is missing). The rules, identical everywhere:
#   - the line starts with NAME= in column 0, optionally after `export ` (comments and indented lines never match);
#   - the LAST assignment wins, as in docker compose;
#   - a trailing CR (file edited on Windows) and the whitespace around the value are dropped;
#   - ONE pair of matching surrounding quotes, single or double, is dropped (the form chosen: compose strips them too).
# It prints the value, so callers use it for secrets only where the value is used and never shown.
env_key() {
  [ -f "$1" ] || return 0
  sed -n -e 's/^export[[:space:]][[:space:]]*//' -e "s/^$2=//p" "$1" | tail -n 1 | tr -d '\r' \
    | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/"
}
