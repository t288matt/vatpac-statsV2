#!/bin/bash
# Read-only deploy checks for the production VM (issue 104, trimmed slice). Run from the owner's PC (Git Bash), repo root:
#   bash ops/deploy.sh --preflight <commit>   before the deploy: is it safe to start? (reads only)
#   bash ops/deploy.sh --verify               after the deploy: is the stack healthy? (reads only; FAILS first if this checkout is not
#       at the commit the VM checkout is at: it counts invariants from the local ops/audit/*.sql, so it must be an up-to-date checkout)
#   bash ops/deploy.sh --status               what is live: release marker, containers, newest dump (reads only)
#   bash ops/deploy.sh --mark <commit> [--allow-silent-issues]  after a VERIFIED deploy: write ~/vatpac-statsV2/.deploy/release (the ONLY write)
#       Refuses unless this checkout's HEAD is exactly <commit> (the silent-issue check reads commits and scripts from here).
#       Refuses first if an issue named in the commits since the previous marker has no comment newer than its newest commit
#       (ops/stale_issues.sh), or if that cannot be checked; --allow-silent-issues marks anyway and prints a warning.
#       Warns if the VM checkout is not at the commit being marked; the .deploy/release.log entry also records thirdparty=<digests of db, grafana and the dbmate base; unknown on any failure> and images=<revision of the
#       running collector image (first 12 characters), or local, or unknown>. The release file itself keeps its three lines.
# Registry mode (VATPAC_IMAGE_PREFIX set in the VM's .env, issue 173): the tag defaults to `latest`, which the release workflow
# moves to the newest published commit (no .env edit per deploy; a VATPAC_IMAGE_TAG=sha-<7> line is only a deliberate rollback pin).
# --preflight checks the five images exist for the commit and WARNs loudly about a sha pin, and that each service the VM runs
# today is healthy (a service that is new in the commit is not missing);
# --verify checks each container's image revision label against the VM
# checkout; --status prints the images. Local mode (neither set) skips these checks. Only the two key names are read from .env.
# It never starts, stops, builds or restarts anything and never prints .env values. The owner's explicit go for each
# deploy is unchanged; this script only replaces remembering the checks. Exit 0 means no FAIL (WARN is allowed).
# Settings (environment): DEPLOY_SSH (default vatpac-v3), DEPLOY_DIR (default ~/vatpac-statsV2), DEPLOY_GH (path to gh),
# DEPLOY_REPO (default t288matt/vatpac-statsV2), DEPLOY_MIN_MEM_MB, DEPLOY_MAX_DISK_PCT, DEPLOY_MAX_DUMP_AGE_H,
# DEPLOY_MIN_DUMP_BYTES, DEPLOY_MAX_LOAD, DEPLOY_LOG_SINCE, DEPLOY_CMD_TIMEOUT (seconds per ssh call, default 60).
# make deploy on the VM (issue 411) runs these on the VM itself, with DEPLOY_LOCAL=1 (the same commands, no ssh to itself):
#   bash ops/deploy.sh --deploy-gate <commit>  first step, before anything changes: the preflight checks; then a fresh dump
#       (backup.sh once in the backup container); then free space (DEPLOY_DUMP_RESERVE_MB, DEPLOY_SAFE_MARGIN_MB) and the new dump
#       checked readable and newer than the start. Any FAIL stops make deploy with nothing changed.
#   bash ops/deploy.sh --wait-healthy           after up: waits up to DEPLOY_WAIT_S (default 300) for every service to be healthy
# Settings for these: DEPLOY_DUMP_RESERVE_MB (default 160), DEPLOY_SAFE_MARGIN_MB (default 1000), DEPLOY_WAIT_S (default 300).

DEPLOY_SSH="${DEPLOY_SSH:-vatpac-v3}"
DEPLOY_DIR="${DEPLOY_DIR:-~/vatpac-statsV2}"
DEPLOY_REPO="${DEPLOY_REPO:-t288matt/vatpac-statsV2}"
MIN_MEM_MB="${DEPLOY_MIN_MEM_MB:-3000}"
MAX_DISK_PCT="${DEPLOY_MAX_DISK_PCT:-80}"
MAX_DUMP_AGE_H="${DEPLOY_MAX_DUMP_AGE_H:-26}"
MIN_DUMP_BYTES="${DEPLOY_MIN_DUMP_BYTES:-1000000}"
WARN_GAP_S=90      # a polling gap from here is a WARN (a restart costs about 45 s; 90 s is three missed polls)
FAIL_GAP_S=300     # a polling gap above this is a FAIL
MAX_LOAD="${DEPLOY_MAX_LOAD:-2.0}"
CMD_TIMEOUT="${DEPLOY_CMD_TIMEOUT:-60}"
LOG_SINCE="${DEPLOY_LOG_SINCE:-6h}"
DUMP_RESERVE_MB="${DEPLOY_DUMP_RESERVE_MB:-160}"   # a fresh dump is about this big (owner decision 19-11-2026)
SAFE_MARGIN_MB="${DEPLOY_SAFE_MARGIN_MB:-1000}"    # free space that must still be left after that dump (owner decision 19-11-2026)
DUMP_NEED_MB=$(( DUMP_RESERVE_MB + SAFE_MARGIN_MB ))
WAIT_HEALTHY_S="${DEPLOY_WAIT_S:-300}"
SERVICES="db collector grafana backup watchdog vatsim-atc-stats"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY=python; command -v python >/dev/null 2>&1 || PY=python3   # counts the invariant pack with ops/watchdog/integrity.py (stdlib only, no psycopg needed)
EXPECTED_FILE="${DEPLOY_EXPECTED_FILE:-$HERE/deploy_expected_warnings.txt}"
ROOT="$(cd "$HERE/.." && pwd)"
# shellcheck source=ops/release_common.sh
IMAGE_MODE_SOURCE_ONLY=1 . "$HERE/image_mode.sh"   # sha_pin_warning: the one wording of the sha- pin warning (issue 213)
SHA_PIN_WARNING="$(sha_pin_warning)"
. "$HERE/env_key.sh"   # env_key: sent to the VM as text by vm_env_key, so the remote read is the same code as the local ones
. "$HERE/release_common.sh"   # gh_bin, RELEASE_MARKER_CMD, release_marker_commit (shared with stale_issues.sh)

VM_DOWN=0
g() { git -C "$ROOT" "$@"; }
PASSES=0; WARNS=0; FAILS=0

# ---------------------------------------------------------------- pure logic (no I/O except `date -d`)

# mem_ok <MemAvailable in MB>: succeeds when it is a number at least the minimum.
mem_ok() { case "$1" in ''|*[!0-9]*) return 1 ;; esac; [ "$1" -ge "$MIN_MEM_MB" ]; }

# registry_mode <prefix> -> local (no prefix) | registry (prefix set; the tag defaults to latest)
registry_mode() { if [ -n "$1" ]; then echo registry; else echo local; fi; }

# short_tag <commit> -> sha-<first 7 characters>, the tag the release workflow gives an image
short_tag() { printf 'sha-%s' "${1:0:7}"; }

# valid_prefix <prefix>: a registry path ending in a slash (it is placed in a remote command, so only safe characters)
valid_prefix() { [[ "$1" =~ ^[a-z0-9][a-z0-9._:/-]*/$ ]]; }

# head_matches <checkout HEAD> <image revision label>: the label (7 to 40 hex) is a prefix of HEAD
head_matches() { [[ "$2" =~ ^[0-9a-f]{7,40}$ ]] && [ "${1#"$2"}" != "$1" ]; }

# in_backup_window <epoch> [HH:MM backup time, default 02:00]: succeeds from 10 minutes before to 30 minutes after it (UTC).
in_backup_window() {
  local t="${2:-02:00}" s c off
  [[ "$t" =~ ^([0-9]{2}):([0-9]{2})$ ]] || t="02:00"
  [[ "$t" =~ ^([0-9]{2}):([0-9]{2})$ ]]
  c=$(( 10#${BASH_REMATCH[1]} * 3600 + 10#${BASH_REMATCH[2]} * 60 )); s=$(( $1 % 86400 ))
  for off in -86400 0 86400; do
    [ "$s" -ge $(( c + off - 600 )) ] && [ "$s" -le $(( c + off + 1800 )) ] && return 0
  done
  return 1
}

# dmy_to_epoch "DD-MM-YYYY HH:MM" -> epoch seconds (UTC); fails on a malformed value.
dmy_to_epoch() {
  [[ "$1" =~ ^([0-9]{2})-([0-9]{2})-([0-9]{4})\ ([0-9]{2}):([0-9]{2})$ ]] || return 1
  date -u -d "${BASH_REMATCH[3]}-${BASH_REMATCH[2]}-${BASH_REMATCH[1]} ${BASH_REMATCH[4]}:${BASH_REMATCH[5]}:00" +%s 2>/dev/null
}

# expected_warning_status <check name> <now epoch> <file> -> none | valid <max> <expiry> | expired <max> <expiry> | malformed
# File lines: name|max count|DD-MM-YYYY HH:MM (UTC expiry)|reason ; # starts a comment. A name must match exactly.
expected_warning_status() {
  local want="$1" now="$2" file="$3" name max exp reason ep
  [ -r "$file" ] || { echo none; return 0; }
  while IFS='|' read -r name max exp reason; do
    case "$name" in ''|'#'*) continue ;; esac
    [ "$name" = "$want" ] || continue
    case "$max" in ''|*[!0-9]*) echo malformed; return 0 ;; esac
    if ! ep="$(dmy_to_epoch "$exp")" || [ -z "$ep" ]; then echo malformed; return 0; fi
    if [ "$now" -lt "$ep" ]; then echo "valid $max $exp"; else echo "expired $max $exp"; fi
    return 0
  done < "$file"
  echo none
}

# classify_invariant <name> <violations> <now epoch> <file> -> prints "STATUS|value"
classify_invariant() {
  local name="$1" n="$2" now="$3" file="$4" st rest max exp
  case "$n" in ''|*[!0-9]*) echo "FAIL|$name: unreadable count '$n'"; return 0 ;; esac
  if [ "$n" -eq 0 ]; then echo "PASS|"; return 0; fi
  st="$(expected_warning_status "$name" "$now" "$file")"; rest="${st#* }"; max="${rest%% *}"; exp="${rest#* }"
  case "$st" in
    valid*) if [ "$n" -le "$max" ]; then echo "WARN|$name = $n (expected, at most $max, until $exp UTC)"
            else echo "FAIL|$name = $n (above the expected maximum of $max)"; fi ;;
    expired*) echo "FAIL|$name = $n (the expected-warning entry expired $exp UTC)" ;;
    malformed*) echo "FAIL|$name = $n (malformed entry in the expected-warnings file)" ;;
    *) echo "FAIL|$name = $n" ;;
  esac
}

# classify_gap <max gap s> <newest row age s> -> PASS | WARN | FAIL | BAD
classify_gap() {
  case "$1" in ''|*[!0-9]*) echo BAD; return ;; esac
  case "$2" in ''|*[!0-9]*) echo BAD; return ;; esac
  if [ "$2" -ge "$WARN_GAP_S" ] || [ "$1" -gt "$FAIL_GAP_S" ]; then echo FAIL
  elif [ "$1" -ge "$WARN_GAP_S" ]; then echo WARN
  else echo PASS; fi
}

# ---------------------------------------------------------------- output

emit() {  # emit PASS|WARN|FAIL <check> <value>
  case "$1" in PASS) PASSES=$((PASSES + 1)) ;; WARN) WARNS=$((WARNS + 1)) ;; *) FAILS=$((FAILS + 1)) ;; esac
  printf '%-5s %-30s %s\n' "$1" "$2" "$3"
}
summary() {
  echo "----"
  printf 'SUMMARY  %d PASS, %d WARN, %d FAIL\n' "$PASSES" "$WARNS" "$FAILS"
  if [ "$FAILS" -eq 0 ]; then echo "RESULT   $1 OK"; return 0; fi
  echo "RESULT   $1 FAILED"; return 1
}

# ---------------------------------------------------------------- access to the VM and GitHub

SSH_OPTS="-o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=10 -o ServerAliveCountMax=3"
with_timeout() { if command -v timeout >/dev/null 2>&1; then timeout "${TMO:-$CMD_TIMEOUT}" "$@"; else "$@"; fi; }
# DEPLOY_LOCAL=1 (make deploy on the VM, issue 411): the same command runs in this checkout instead of over ssh to the VM itself.
# shellcheck disable=SC2086
vm()    { [ "$VM_DOWN" = 1 ] && return 255
          if [ "${DEPLOY_LOCAL:-0}" = 1 ]; then with_timeout bash -c "cd \"$ROOT\" && $1" </dev/null; else with_timeout ssh -n $SSH_OPTS "$DEPLOY_SSH" "cd $DEPLOY_DIR && $1"; fi; }
# shellcheck disable=SC2086
vm_in() { [ "$VM_DOWN" = 1 ] && return 255
          if [ "${DEPLOY_LOCAL:-0}" = 1 ]; then with_timeout bash -c "cd \"$ROOT\" && $1"; else with_timeout ssh $SSH_OPTS "$DEPLOY_SSH" "cd $DEPLOY_DIR && $1"; fi; }

# psql inside the db container, read-only, SQL on stdin. The single quotes keep $POSTGRES_* for the container's shell.
PSQL='docker compose exec -T db sh -c '\''psql -X -q -tA -F"|" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1'\'''
# One line per container: name|started|restarts|health-or-state|image|revision label. The last two were appended for registry
# mode: fields 1 to 4 keep their positions, and an image without the label gives an empty 6th field.
INSPECT='docker inspect -f "{{.Name}}|{{.State.StartedAt}}|{{.RestartCount}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}|{{.Config.Image}}|{{index .Config.Labels \"org.opencontainers.image.revision\"}}" $(docker compose ps -aq)'
# Newest dump: epoch size name (validity checked with pg_restore -l in the backup container, which only reads)
NEWEST_DUMP='docker compose exec -T backup sh -c '\''f=$(find /backups -maxdepth 1 -name "*.dump" -printf "%T@ %s %f\n" | sort -n | tail -n 1); echo "$f"; n=${f##* }; [ -n "$n" ] && pg_restore -l "/backups/$n" >/dev/null 2>&1 && echo valid || echo invalid'\'''

# local_head: the full commit the local checkout (where this script and ops/audit/*.sql are read from) is at; empty when unreadable
local_head() { g rev-parse HEAD 2>/dev/null | tr -d '\r' | head -n 1; }
# load_vm_head: sets VM_HEAD to the full commit the VM checkout is at (empty when the VM cannot be read); read once per run
VM_HEAD=""; VM_HEAD_LOADED=0
load_vm_head() {
  [ "$VM_HEAD_LOADED" = 1 ] && return 0
  VM_HEAD="$(vm 'git rev-parse HEAD' 2>/dev/null | tr -d '\r' | head -n 1)"; VM_HEAD_LOADED=1
}

# reach_what: how the VM is reached, for messages: "ssh <host>", or "the local shell" when DEPLOY_LOCAL=1
reach_what() { if [ "${DEPLOY_LOCAL:-0}" = 1 ]; then echo "the local shell"; else echo "ssh $DEPLOY_SSH"; fi; }
# vm_up: probes the VM once per run (the answer is kept); succeeds when ssh answers, and sets VM_DOWN=1 when it does not
VM_PROBE=""
vm_up() {
  if [ -z "$VM_PROBE" ]; then
    if [ "$(vm 'echo reachable' 2>/dev/null | tr -d '\r')" = reachable ]; then VM_PROBE=up; else VM_PROBE=down; VM_DOWN=1; fi
  fi
  [ "$VM_PROBE" = up ]
}
# stale_hint <commit>: what to do when this checkout is not at the commit the VM runs (or the commit being marked). It must name THAT commit:
# origin/main may have moved on since the release.
stale_hint() { echo "run it from a checkout of the VM's commit, for example a fresh worktree: git worktree add --detach .worktrees/verify ${1:0:10}"; }

# dirty_ops: prints up to three changed or untracked files under ops/ ("?" when git cannot answer); empty when clean
dirty_ops() {
  local out
  out="$(g status --porcelain -- ops 2>/dev/null)" || { echo "?"; return; }
  printf '%s\n' "$out" | sed '/^$/d' | head -n 3 | cut -c4- | tr '\n' ' '
}

vm_now() { vm 'date -u +%s' 2>/dev/null | tr -d '\r' | grep -E '^[0-9]+$' | head -n 1; }
iso() { date -u -d "@$1" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null; }

# ---------------------------------------------------------------- shared container facts

CONTAINERS=""
load_containers() { CONTAINERS="$(vm "$INSPECT" 2>/dev/null | tr -d '\r' | grep '|')"; }

# vm_env_key <NAME>: the value of one key from the VM's .env; nothing else of the file is read (used for the two image keys only)
# The read is ops/env_key.sh's env_key (rules there), sent to the VM as text because the VM checkout may still be the old commit before a deploy.
vm_env_key() { vm "$(declare -f env_key); env_key .env $1" 2>/dev/null | tr -d '\r'; }

# load_image_keys: sets IMG_PREFIX and IMG_TAG from the VM's .env, read once per run
IMG_PREFIX=""; IMG_TAG=""; IMG_LOADED=0
load_image_keys() {
  [ "$IMG_LOADED" = 1 ] && return 0
  IMG_PREFIX="$(vm_env_key VATPAC_IMAGE_PREFIX)"; IMG_TAG="$(vm_env_key VATPAC_IMAGE_TAG)"; IMG_LOADED=1
}

# svc_line <service>: the inspect line of the service's container
svc_line() { printf '%s\n' "$CONTAINERS" | grep -E "^/vatpac-$1-[0-9]+\|" | head -n 1; }

# vm_services <list>: those of the SERVICES that the VM's own compose file defines TODAY (its checkout is still the old commit before a
# deploy), so a service that is new in the commit being deployed is not "missing" in a preflight. Empty output = could not be read.
vm_services() {
  local defined svc out=""
  defined="$(vm 'docker compose config --services' 2>/dev/null | tr -d '\r')"
  for svc in $SERVICES; do
    printf '%s\n' "$defined" | grep -qx "$svc" && out="$out $svc"
  done
  printf '%s' "${out# }"
}

# unhealthy <services>: the services of the list that are not healthy yet, as name=state; empty when all are healthy
unhealthy() {
  local svc h out=""
  for svc in $1; do h="$(svc_line "$svc" | cut -d'|' -f4)"; [ "$h" = healthy ] || out="$out $svc=${h:-missing}"; done
  printf '%s' "${out# }"
}

# check_healthy [<services>]: every named service (default: all of SERVICES) must be healthy; the preflight passes the VM's current ones
check_healthy() {
  local bad list="${1-$SERVICES}"
  [ -n "$CONTAINERS" ] || { emit FAIL containers_healthy "could not read the container list"; return; }
  [ -n "$list" ] || { emit FAIL containers_healthy "could not read the VM's service list"; return; }
  bad="$(unhealthy "$list")"
  if [ -z "$bad" ]; then emit PASS containers_healthy "all of: $list"; else emit FAIL containers_healthy "not healthy: $bad"; fi
}

REGISTRY_SERVICES="collector watchdog backup migrate loader"
# The compose service of an image whose name differs (the loader image is vatpac-stats-loader, its service vatsim-atc-stats).
svc_of() { case "$1" in loader) echo vatsim-atc-stats ;; *) echo "$1" ;; esac; }

# image_problems <checkout HEAD> <.env tag, empty = latest>: prints what is wrong, empty when every registry-image container (from $CONTAINERS) runs
# the .env tag and carries a revision label that HEAD starts with. The images carry the code and the migrations (nothing is mounted
# over the migrate image), so this proves the deployed commit is the one running. A latest tag is checked by the label alone; a `sha-` pin must also
# name the checkout's commit.
image_problems() {
  local head="$1" tag="${2:-latest}" out="" svc l img rev
  case "$tag" in sha-*) [ "$tag" = "$(short_tag "$head")" ] || out="$out .env tag is the pin $tag but the checkout is $(short_tag "$head");" ;; esac
  for svc in $REGISTRY_SERVICES; do
    l="$(svc_line "$(svc_of "$svc")")"
    if [ -z "$l" ]; then out="$out $svc: no container;"; continue; fi
    img="$(printf '%s' "$l" | cut -d'|' -f5)"; rev="$(printf '%s' "$l" | cut -d'|' -f6)"
    if [ "${img##*:}" != "$tag" ]; then out="$out $svc runs image tag '${img##*:}' not $tag;"
    elif ! head_matches "$head" "$rev"; then out="$out $svc revision label '${rev:0:12}' is not the checkout ${head:0:12};"; fi
  done
  printf '%s' "$out"
}

check_images() {
  local pfx tag mode head probs
  [ "$VM_DOWN" = 1 ] && { emit FAIL image_revisions "VM unreachable"; return; }
  load_image_keys; pfx="$IMG_PREFIX"; tag="$IMG_TAG"; mode="$(registry_mode "$pfx")"
  case "$mode" in
    local) emit PASS image_revisions "local build mode" ;;
    *) load_vm_head; head="$VM_HEAD"
       if [ -z "$CONTAINERS" ] || ! [[ "$head" =~ ^[0-9a-f]{40}$ ]]; then emit FAIL image_revisions "could not read the containers or the checkout HEAD"; return; fi
       probs="$(image_problems "$head" "$tag")"
       if [ -z "$probs" ]; then emit PASS image_revisions "registry mode: $REGISTRY_SERVICES run ${tag:-latest}, built from the checkout ${head:0:10}"
       else emit FAIL image_revisions "registry mode:$probs"; fi ;;
  esac
}

check_restarts() {
  local bad
  [ -n "$CONTAINERS" ] || { emit FAIL restart_counts "could not read the container list"; return; }
  bad="$(printf '%s\n' "$CONTAINERS" | awk -F'|' '$3 != "0" {printf " %s=%s", $1, $3}')"
  if [ -z "$bad" ]; then emit PASS restart_counts "all 0 ($(printf '%s\n' "$CONTAINERS" | wc -l | tr -d ' ') containers)"; else emit FAIL restart_counts "non-zero:$bad"; fi
}

dump_facts() {  # sets D_EPOCH D_SIZE D_NAME D_VALID from the newest dump; returns 1 if unreadable
  local out first
  out="$(vm "$NEWEST_DUMP" 2>/dev/null | tr -d '\r')" || return 1
  first="$(printf '%s\n' "$out" | sed -n 1p)"; D_VALID="$(printf '%s\n' "$out" | sed -n 2p)"
  D_EPOCH="$(printf '%s' "$first" | awk '{print $1}')"; D_EPOCH="${D_EPOCH%%.*}"
  D_SIZE="$(printf '%s' "$first" | awk '{print $2}')"; D_NAME="$(printf '%s' "$first" | awk '{print $3}')"
  case "$D_EPOCH$D_SIZE" in ''|*[!0-9]*) return 1 ;; esac
  [ -n "$D_NAME" ] && [ -n "$D_VALID" ]
}

# ---------------------------------------------------------------- --preflight

preflight() {
  local want="$1" full tip behind now out ci_lines ci_bad pct free tdc mem foreign dumpage ageh bt load
  [[ "$want" =~ ^[0-9a-fA-F]{7,40}$ ]] || { echo "usage: deploy.sh --preflight <commit (7 to 40 hex characters)>" >&2; return 2; }
  echo "deploy preflight for $want against $(reach_what | sed 's/^ssh //') (read-only)"
  echo "local checkout is at $(local_head | cut -c1-10)"

  g fetch -q origin main 2>/dev/null
  full="$(g rev-parse --verify -q "${want}^{commit}" 2>/dev/null)"
  tip="$(g rev-parse --verify -q origin/main 2>/dev/null)"
  if [ -z "$full" ] || [ -z "$tip" ]; then
    emit FAIL commit_on_main "cannot resolve '$want' or origin/main"
  elif ! g merge-base --is-ancestor "$full" "$tip" 2>/dev/null; then
    emit FAIL commit_on_main "${full:0:10} is not on origin/main"
  elif [ "$full" = "$tip" ]; then
    emit PASS commit_on_main "${full:0:10} is the tip of origin/main"
  else
    behind="$(g rev-list --count "$full..$tip" 2>/dev/null)"
    emit PASS commit_on_main "${full:0:10} is on origin/main (${behind:-?} newer commit(s) after it)"
  fi

  # CI green for that exact sha: the newest run of the ci workflow on it must be completed/success. Other workflows on the
  # same commit (release, docs-secret-scan, ci-failure-issue) are not CI and must not turn this red.
  if [ -z "$full" ]; then
    emit FAIL ci_green "no commit to look up"
  else
    ci_line="$("$(gh_bin)" run list --repo "$DEPLOY_REPO" --workflow ci --commit "$full" --limit 1 --json status,conclusion --jq '.[] | .status + " " + (.conclusion // "")' 2>/dev/null | tr -d '\r' | head -n 1)"
    if [ -z "$ci_line" ]; then
      emit FAIL ci_green "no ci run found for ${full:0:10} (gh missing, not logged in, or no run)"
    elif [ "$ci_line" = "completed success" ]; then
      emit PASS ci_green "ci run completed success for ${full:0:10}"
    else
      emit FAIL ci_green "ci run: $ci_line"
    fi
  fi

  if ! vm_up; then
    emit FAIL vm_reachable "$(reach_what) did not answer"
  else
    emit PASS vm_reachable "$(reach_what) answers"
  fi
  tdc="$(vm 'timedatectl' 2>/dev/null | tr -d '\r')"
  if printf '%s\n' "$tdc" | grep -q 'System clock synchronized: yes' && printf '%s\n' "$tdc" | grep -q 'NTP service: active'; then
    emit PASS vm_clock "synchronized: yes, NTP service: active"
  else
    emit FAIL vm_clock "$(printf '%s\n' "$tdc" | grep -E 'synchronized|NTP service' | tr -s ' ' | tr '\n' ';' | cut -c1-100)"
  fi

  # MemAvailable (the kernel's estimate), never the "free" column
  mem="$(vm 'awk "/^MemAvailable:/{print int(\$2/1024)}" ${DEPLOY_MEMINFO:-/proc/meminfo}' 2>/dev/null | tr -d '\r' | head -n 1)"
  if mem_ok "$mem"; then emit PASS mem_available "$mem MB (needs $MIN_MEM_MB)"
  else emit FAIL mem_available "${mem:-could not read} MB (needs $MIN_MEM_MB)"; fi

  # load average (1 minute): parallel test containers caused snapshot apply timeouts on 10-10-2026 (issue 113)
  load="$(vm 'awk "{print \$1}" ${DEPLOY_LOADAVG:-/proc/loadavg}' 2>/dev/null | tr -d '\r' | head -n 1)"
  if [[ "$load" =~ ^[0-9]+(\.[0-9]+)?$ ]] && awk -v l="$load" -v m="$MAX_LOAD" 'BEGIN{exit !(l<=m)}'; then emit PASS load "1-minute load average $load (limit $MAX_LOAD)"
  else emit FAIL load "1-minute load average ${load:-unreadable} (limit $MAX_LOAD)"; fi

  out="$(vm 'df -P / | awk "NR==2{print \$5, int(\$4/1024/1024)}"' 2>/dev/null | tr -d '\r' | head -n 1)"
  pct="${out%% *}"; pct="${pct%\%}"; free="${out##* }"
  case "$pct" in ''|*[!0-9]*) emit FAIL disk_free "could not read" ;;
    *) if [ "$pct" -le "$MAX_DISK_PCT" ]; then emit PASS disk_free "/ ${pct}% used, ${free} GB free (limit ${MAX_DISK_PCT}%)"
       else emit FAIL disk_free "/ ${pct}% used, ${free} GB free (limit ${MAX_DISK_PCT}%)"; fi ;;
  esac

  load_containers
  check_healthy "$(vm_services)"   # the stack running now, not the one about to be deployed
  check_restarts

  # backup window: BACKUP_TIME_UTC from the VM's .env if set (only that one value is read, and only an HH:MM is ever used)
  now="$(vm_now)"
  bt="$(vm_env_key BACKUP_TIME_UTC)"
  [[ "$bt" =~ ^[0-9]{2}:[0-9]{2}$ ]] || bt="02:00"
  if [ -z "$now" ]; then emit FAIL backup_window "could not read the VM time"
  elif in_backup_window "$now" "$bt"; then emit FAIL backup_window "$(iso "$now" | cut -c12-16) UTC is within -10/+30 minutes of the $bt UTC backup and restore test"
  else emit PASS backup_window "$(iso "$now" | cut -c12-16) UTC is outside the $bt UTC backup window"; fi

  out="$(vm "docker ps -a --format '{{.Names}}'" 2>/dev/null | tr -d '\r')"
  if [ -z "$out" ]; then emit FAIL foreign_containers "could not list containers"
  else
    foreign="$(printf '%s\n' "$out" | grep -v '^vatpac-' | tr '\n' ' ')"
    if [ -z "$foreign" ]; then emit PASS foreign_containers "none"; else emit FAIL foreign_containers "non-vatpac containers present: $foreign"; fi
  fi

  # newest dump: readable, big enough, not from the future, under 26 h (FAIL); over 2 h old is a WARN
  if [ "${DEPLOY_GATE:-0}" = 1 ]; then printf 'SKIP  %-30s %s\n' recent_dump "the fresh dump that make deploy takes next replaces this check"
  elif ! dump_facts; then emit FAIL recent_dump "could not read the backup volume"
  elif [ -z "$now" ]; then emit FAIL recent_dump "no VM time to compute the age"
  else
    dumpage=$(( now - D_EPOCH )); ageh=$(( dumpage / 3600 ))
    if [ "$D_VALID" != valid ] || [ "$D_SIZE" -lt "$MIN_DUMP_BYTES" ] || [ "$dumpage" -lt 0 ] || [ "$ageh" -ge "$MAX_DUMP_AGE_H" ]; then
      emit FAIL recent_dump "$D_NAME, ${ageh} h old (max $MAX_DUMP_AGE_H), $D_SIZE bytes (min $MIN_DUMP_BYTES), readable: $D_VALID"
    elif [ "$dumpage" -gt 7200 ]; then
      emit WARN recent_dump "$D_NAME is ${ageh} h old, $D_SIZE bytes, readable; run 'docker compose exec backup backup.sh once' before the deploy"
    else
      emit PASS recent_dump "$D_NAME, ${ageh} h old, $D_SIZE bytes, pg_restore -l ok"
    fi
  fi

  check_registry_preflight "${full:-$want}"

  [ "${DEPLOY_GATE:-0}" = 1 ] && return 0   # the deploy gate goes on to the dump and reads the counts itself
  summary preflight
}

# registry checks of --preflight; $1 is the commit being deployed
check_registry_preflight() {
  local c7 pfx tag mode n bad=""
  c7="$(short_tag "$1")"
  [ "$VM_DOWN" = 1 ] && { emit FAIL registry_mode "VM unreachable, registry checks skipped"; return; }
  load_image_keys; pfx="$IMG_PREFIX"; tag="$IMG_TAG"; mode="$(registry_mode "$pfx")"
  case "$mode" in
    local) emit PASS registry_mode "local build mode"; return ;;
  esac
  emit PASS registry_mode "registry mode (VATPAC_IMAGE_PREFIX is set in the VM's .env)"
  if ! valid_prefix "$pfx"; then emit FAIL registry_images "VATPAC_IMAGE_PREFIX in the VM's .env is not a registry path ending in a slash"
  else
    for n in $REGISTRY_SERVICES; do
      TMO=120 vm "docker manifest inspect ${pfx}vatpac-stats-$n:$c7 >/dev/null 2>&1" || bad="$bad $n"
    done
    if [ -z "$bad" ]; then emit PASS registry_images "$REGISTRY_SERVICES are published as $c7"
    else emit FAIL registry_images "not readable as $c7:$bad; check the release workflow published this commit, or the token expired: docker login ghcr.io"; fi
  fi
  case "$tag" in
    sha-*) emit WARN image_tag_env "$SHA_PIN_WARNING (the VM's .env pins $tag)" ;;
    '') emit PASS image_tag_env "no VATPAC_IMAGE_TAG line: images follow latest" ;;
    *) emit PASS image_tag_env "VATPAC_IMAGE_TAG override is set (not a sha- pin)" ;;
  esac
}

# ---------------------------------------------------------------- --verify

# check_checkout_clean: uncommitted edits under ops/ are what --verify and --mark would run, whatever HEAD says. Returns 1 when dirty.
check_checkout_clean() {
  local d
  d="$(dirty_ops)"
  if [ "$d" = "?" ]; then emit FAIL checkout_clean "cannot read the git status of this checkout (git missing or not a repository)"; return 1
  elif [ -n "$d" ]; then emit FAIL checkout_clean "uncommitted changes under ops/: ${d% }; commit, stash or use a clean worktree at the VM's commit"; return 1
  else emit PASS checkout_clean "no uncommitted changes under ops/"; fi
}

# check_checkout_current: --verify counts the invariants and runs the flap check from the LOCAL ops/audit/*.sql, so a checkout that is
# not at the commit the VM runs gives wrong results (22-10-2026: 21 invariants counted against 20 in production). Returns 1 on a
# mismatch (the caller stops: nothing after it could be trusted); an unreadable HEAD is a FAIL line but does not stop the run.
check_checkout_current() {
  local lh vh
  load_vm_head; lh="$(local_head)"; vh="$VM_HEAD"
  if ! [[ "$lh" =~ ^[0-9a-f]{40}$ ]]; then emit FAIL checkout_current "cannot read the local checkout HEAD"
  elif ! [[ "$vh" =~ ^[0-9a-f]{40}$ ]]; then emit FAIL checkout_current "cannot read the VM checkout HEAD, so the local checkout ${lh:0:10} cannot be compared with it"
  elif [ "$lh" != "$vh" ]; then
    echo "!!!! STALE CHECKOUT: this checkout is at ${lh:0:10} but the VM checkout is at ${vh:0:10}. Nothing below can be trusted, so no check was run."
    emit FAIL checkout_current "local ${lh:0:10} is not the VM's ${vh:0:10}; $(stale_hint "$vh")"
    return 1
  else emit PASS checkout_current "local checkout and VM checkout are both at ${lh:0:10}"; fi
  return 0
}

verify() {
  local out line name cnt detail res st age maxgap rows ts bad total seen hits lines gapclass hs en er ag
  echo "deploy verify against $(reach_what | sed 's/^ssh //') (read-only)"
  if ! vm_up; then emit FAIL vm_reachable "$(reach_what) did not answer"; else emit PASS vm_reachable "$(reach_what | sed 's/^ssh .*/ssh/') answers"; fi
  check_checkout_current || { summary verify; return 1; }
  check_checkout_clean || { summary verify; return 1; }

  # smoke: only the guarded version (issue 86). The old VM copy had unguarded DELETE and INSERT attempts, so it is never run.
  out="$(vm 'grep -c ro-check-begin ops/smoke.sh' 2>/dev/null | tr -d '\r' | head -n 1)"
  if [ "$out" != 1 ]; then
    emit FAIL smoke "SKIPPED: the VM's ops/smoke.sh lacks the 'ro-check-begin' marker (the old unguarded version); deploy the guarded one first"
  else
    out="$(TMO=600 vm 'sh ops/smoke.sh' 2>&1 | tr -d '\r')"
    if printf '%s\n' "$out" | tail -n 1 | grep -q '^SMOKE PASS$'; then emit PASS smoke "SMOKE PASS ($(printf '%s\n' "$out" | grep -c '^PASS') checks)"
    else emit FAIL smoke "$(printf '%s\n' "$out" | grep -E '^FAIL' | head -n 2 | tr '\n' ';' | cut -c1-150) no SMOKE PASS"; fi
  fi

  out="$(vm 'python3 ops/check_panels.py' 2>&1 | tr -d '\r')"
  line="$(printf '%s\n' "$out" | grep -E '^panel queries: ' | tail -n 1)"
  if [ -z "$line" ]; then emit FAIL panels "could not run ops/check_panels.py"
  elif printf '%s' "$line" | grep -qE ' 0 errors$'; then emit PASS panels "$line"
  else emit FAIL panels "$line"; fi

  load_containers
  check_restarts
  check_images
  line="$(svc_line collector)"
  if [ -n "$line" ]; then emit PASS collector_started "$(printf '%s' "$line" | cut -d'|' -f2 | cut -c1-19) UTC (health: $(printf '%s' "$line" | cut -d'|' -f4))"
  else emit FAIL collector_started "collector container not found"; fi

  # polling: newest row under 90 s old and no gap over 300 s in the newest 20 rows; a gap of 90 to 300 s is a WARN with its time
  out="$(printf '%s\n' "SET default_transaction_read_only = on; SELECT coalesce(max(gap),-1)::int, (SELECT extract(epoch FROM now() - max(polled_at))::int FROM poll_log), count(*), coalesce(to_char((array_agg(polled_at ORDER BY gap DESC NULLS LAST))[1], 'DD-MM HH24:MI:SS'), '') FROM (SELECT polled_at, extract(epoch FROM polled_at - lag(polled_at) OVER (ORDER BY polled_at)) AS gap FROM (SELECT polled_at FROM poll_log ORDER BY polled_at DESC LIMIT 20) q) g;" \
        | vm_in "$PSQL" 2>/dev/null | tr -d '\r' | grep -E '^-?[0-9]+\|-?[0-9]+\|[0-9]+\|' | head -n 1)"
  IFS='|' read -r maxgap age rows ts <<EOF
$out
EOF
  gapclass="$(classify_gap "$maxgap" "$age")"
  if [ -z "$out" ] || [ "$gapclass" = BAD ]; then emit FAIL polling_gap "could not read poll_log"
  elif [ "$rows" -lt 5 ]; then emit FAIL polling_gap "only $rows poll_log rows"
  else emit "$gapclass" polling_gap "longest gap $maxgap s (ending $ts UTC) over the newest $rows rows, newest row $age s old (WARN from ${WARN_GAP_S} s, FAIL above ${FAIL_GAP_S} s or newest row over ${WARN_GAP_S} s)"; fi

  # collector /health: parsed as JSON inside the container; archive enabled, errors 0, last write under 600 s ago
  out="$(vm 'docker compose exec -T collector python -c "import os,json,urllib.request;r=urllib.request.urlopen(\"http://127.0.0.1:%s/health\"%os.environ.get(\"HEALTH_PORT\",\"8080\"),timeout=5);a=json.load(r).get(\"archive\") or {};x=a.get(\"last_write_age_seconds\");print(r.status,a.get(\"enabled\"),a.get(\"errors\"),int(x) if x is not None else -1)"' 2>/dev/null | tr -d '\r' | tail -n 1)"
  IFS=' ' read -r hs en er ag <<EOF
$out
EOF
  if [ "$hs" = 200 ] && [ "$en" = True ] && [ "$er" = 0 ] && [[ "$ag" =~ ^[0-9]+$ ]] && [ "$ag" -le 600 ]; then
    emit PASS health "200; archive enabled, errors 0, last write $ag s ago (limit 600)"
  else emit FAIL health "status=${hs:-none} archive_enabled=${en:-?} errors=${er:-?} last_write_age_s=${ag:-?} (needs 200, True, 0, 0 to 600)"; fi

  out="$( { echo "SET default_transaction_read_only = on;"; cat "$HERE/audit/identity_flap.sql"; } | vm_in "$PSQL" 2>/dev/null | tr -d '\r' | grep -E '\|[0-9]+\|' )"
  if [ "$(printf '%s\n' "$out" | grep -c .)" -ne 2 ]; then emit FAIL identity_flap "could not read two result rows"
  else
    bad="$(printf '%s\n' "$out" | awk -F'|' '$2 != "0" {n++} END {print n+0}')"
    if [ "$bad" -eq 0 ]; then emit PASS identity_flap "0, 0"; else emit FAIL identity_flap "counts: $(printf '%s\n' "$out" | cut -d'|' -f2 | tr '\n' ' ')"; fi
  fi

  # invariant pack: exactly as many rows as the local file defines, every row parseable (a NULL or text count is a FAIL)
  names="$("$PY" "$HERE/watchdog/integrity.py" --names "$HERE/audit/invariants.sql" 2>/dev/null)"; names_rc=$?
  total="$(printf '%s\n' "$names" | grep -c .)"
  now="$(vm_now)"
  out="$( { echo "SET default_transaction_read_only = on;"; cat "$HERE/audit/invariants.sql"; } | vm_in "$PSQL" 2>/dev/null | tr -d '\r' | grep -v '^$')"
  seen=0; bad=0
  if [ -z "$now" ] || [ -z "$out" ]; then emit FAIL invariants "could not run the invariant pack (or read the VM time)"
  else
    while IFS='|' read -r name cnt detail; do
      seen=$((seen + 1))
      res="$(classify_invariant "$name" "$cnt" "$now" "$EXPECTED_FILE")"; st="${res%%|*}"
      [ "$st" = PASS ] && continue
      emit "$st" "invariant" "${res#*|}"; [ "$st" = FAIL ] && bad=1
    done <<EOF
$out
EOF
    if [ "$names_rc" -ne 0 ]; then emit FAIL invariants "could not read the invariant names: $PY ops/watchdog/integrity.py --names failed (is Python installed?)"
    elif [ "$seen" -ne "$total" ] || [ "$total" -lt 1 ]; then emit FAIL invariants "got $seen rows, the pack defines $total"
    elif [ "$bad" -eq 0 ]; then emit PASS invariants "$seen invariants checked, none failing"; fi
  fi

  out="$(vm "docker compose logs --since $LOG_SINCE watchdog 2>&1; echo \"logs_rc=\$?\"" 2>/dev/null | tr -d '\r')"
  if ! printf '%s\n' "$out" | grep -q '^logs_rc=0$'; then emit FAIL watchdog_log "could not read the watchdog log"
  else
    hits="$(printf '%s\n' "$out" | grep -cE 'Traceback|could not format the alert')"
    lines="$(printf '%s\n' "$out" | grep -vc '^logs_rc=')"
    if [ "$hits" -eq 0 ]; then emit PASS watchdog_log "$lines lines in the last $LOG_SINCE, no Traceback, no 'could not format the alert'"
    else emit FAIL watchdog_log "$hits bad line(s) in the last $LOG_SINCE"; fi
  fi

  summary verify
}

# ---------------------------------------------------------------- --status

status() {
  local out now
  echo "deploy status of $DEPLOY_SSH (read-only)"
  echo "local checkout is at $(local_head | cut -c1-10)"
  if ! vm_up; then echo "FAIL  $(reach_what) did not answer"; return 1; fi
  echo "== release marker"
  out="$(vm 'cat .deploy/release 2>/dev/null || echo "no marker: no deploy has been marked yet"' 2>/dev/null | tr -d '\r')"
  printf '%s\n' "${out:-could not read the VM tree}"
  echo "== containers (name | started | restarts | health)"
  load_containers
  if [ -z "$CONTAINERS" ]; then echo "could not read the container list"; else printf '%s\n' "$CONTAINERS" | cut -d'|' -f1-4; fi
  echo "== images (name | image | revision label)"
  if [ -n "$CONTAINERS" ]; then printf '%s\n' "$CONTAINERS" | awk -F'|' '{printf "%s | %s | %s\n", $1, ($5 == "" ? "?" : $5), ($6 == "" ? "(no label)" : substr($6, 1, 12))}'; fi
  load_image_keys; load_vm_head
  echo "image mode: $(registry_mode "$IMG_PREFIX") (only the two image keys of the VM's .env are read); checkout HEAD: ${VM_HEAD:0:12}"
  echo "== newest dump"
  now="$(vm_now)"
  if dump_facts && [ -n "$now" ]; then echo "$D_NAME, $(( (now - D_EPOCH) / 3600 )) h old, $D_SIZE bytes, pg_restore -l: $D_VALID"
  else echo "could not read the backup volume"; fi
}

# third_party_digests: "db=<digest> grafana=<digest> migrate_base=<digest>" for the running third-party images (ADR-032), read from the
# VM's docker with `docker inspect` on the tag that docker-compose.yml / ops/migrate/Dockerfile names. Nothing from .env. Any failure
# (no docker, image not present, no digest) records `unknown` for that image and never blocks the mark.
third_party_digests() {
  local out="" spec name file rx ref d
  for spec in "db|docker-compose.yml|postgres" "grafana|docker-compose.yml|grafana/grafana" "migrate_base|ops/migrate/Dockerfile|amacneil/dbmate"; do
    name="${spec%%|*}"; file="$(printf '%s' "$spec" | cut -d'|' -f2)"; rx="${spec##*|}"
    ref="$(vm "sed -n 's#^[ ]*\(image:[ ]*\|FROM[ ]*\)\($rx:[A-Za-z0-9._-]*\).*#\2#p' $file | head -n 1" 2>/dev/null | tr -cd 'A-Za-z0-9._:/-')"
    d=""
    [ -z "$ref" ] || d="$(vm "docker inspect -f '{{index .RepoDigests 0}}' $ref" 2>/dev/null | tr -cd 'A-Za-z0-9._:@/-')"
    out="$out$name=${d:-unknown} "
  done
  printf '%s' "${out% }"
}

# ---------------------------------------------------------------- --mark (the only write)

mark() {
  local want="$1" allow="${2:-}" d full lh now stamp who prev rc vmhead pfx tag imgs crev
  [ -z "$allow" ] || [ "$allow" = --allow-silent-issues ] || { echo "usage: deploy.sh --mark <commit> [--allow-silent-issues]" >&2; return 2; }
  [[ "$want" =~ ^[0-9a-fA-F]{7,40}$ ]] || { echo "usage: deploy.sh --mark <commit (7 to 40 hex characters)>" >&2; return 2; }
  full="$(g rev-parse --verify -q "${want}^{commit}" 2>/dev/null)"
  [ -n "$full" ] || { echo "FAIL  cannot resolve '$want' to a commit" >&2; return 1; }
  # the silent-issue check below runs ops/stale_issues.sh from this checkout: refuse unless HEAD is exactly the commit being marked
  lh="$(local_head)"
  if [ "$lh" != "$full" ]; then
    echo "!!!! STALE CHECKOUT: this checkout is at ${lh:0:10} but the commit being marked is ${full:0:10}; nothing written. $(stale_hint "$full")" >&2
    return 1
  fi
  d="$(dirty_ops)"
  if [ -n "$d" ]; then
    echo "!!!! DIRTY CHECKOUT: uncommitted changes under ops/ (or git status unreadable): ${d% }; commit, stash or use a clean worktree at the commit being marked; nothing written" >&2
    return 1
  fi
  g fetch -q origin main 2>/dev/null
  g merge-base --is-ancestor "$full" origin/main 2>/dev/null || { echo "FAIL  ${full:0:10} is not on origin/main; nothing written" >&2; return 1; }
  now="$(vm_now)"; [ -n "$now" ] || { echo "FAIL  could not read the VM time; nothing written" >&2; return 1; }
  # silent-issue guard (fails closed): each issue named since the previous marker needs a comment newer than its newest commit
  prev="$(vm "$RELEASE_MARKER_CMD" 2>/dev/null | release_marker_commit)"
  if [ -z "$prev" ]; then rc=2; echo "no previous marker could be read, so the commits to check are unknown" >&2
  else DEPLOY_SSH="$DEPLOY_SSH" DEPLOY_DIR="$DEPLOY_DIR" GH_BIN="$(gh_bin)" REPO="$DEPLOY_REPO" bash "$HERE/stale_issues.sh" --range "$prev..$full"; rc=$?; fi
  if [ "$rc" -ne 0 ]; then
    if [ "$allow" = --allow-silent-issues ]; then echo "WARN  marking with silent or unchecked issues (--allow-silent-issues); comment on them after this deploy" >&2
    else
      if [ "$rc" -eq 1 ]; then echo "FAIL  the issues above have commits but no newer comment: comment on each (what shipped, the commit, how verified), then mark again; nothing written" >&2
      else echo "FAIL  the silent-issue check could not run (fails closed); fix gh or use --allow-silent-issues; nothing written" >&2; fi
      return 1
    fi
  fi
  load_vm_head; vmhead="$VM_HEAD"
  if [ "$vmhead" != "$full" ]; then
    echo "WARN  the VM checkout HEAD (${vmhead:-unreadable}) is not the commit being marked (${full:0:10}); mark only a commit that is really deployed" >&2
  fi
  load_image_keys; pfx="$IMG_PREFIX"; tag="$IMG_TAG"
  case "$(registry_mode "$pfx")" in
    registry) load_containers   # record the revision actually running (the tag is the moving name latest), not the tag
              crev="$(svc_line collector | cut -d'|' -f6 | tr -cd '0-9a-f')"
              imgs="${crev:0:12}"; [ -n "$imgs" ] || imgs=unknown ;;
    local) imgs=local ;;
  esac
  tp="$(third_party_digests)"; [ -n "$tp" ] || tp="db=unknown grafana=unknown migrate_base=unknown"; tpq="'$tp'"
  stamp="$(iso "$now")"; who="$(printf '%s' "${USER:-${USERNAME:-unknown}}" | tr -cd 'A-Za-z0-9._-')"
  # the log line goes first and the marker is replaced last, so a failed log append leaves the old marker in place.
  # The release file keeps exactly three lines (other tools read it); only the log entry gets the extra images= line.
  printf 'commit=%s\nmarked_utc=%s\nmarked_by=%s\n' "$full" "$stamp" "$who" \
    | vm_in "mkdir -p .deploy && cat > .deploy/release.new && { cat .deploy/release.new; echo images=$imgs; echo thirdparty=$tpq; echo ---; } >> .deploy/release.log && mv .deploy/release.new .deploy/release" \
    || { echo "FAIL  could not write the marker or its log; the previous marker (if any) is unchanged" >&2; return 1; }
  echo "PASS  marked ${full:0:10} at $stamp by $who (~/vatpac-statsV2/.deploy/release on the VM)"
}

# ---------------------------------------------------------------- make deploy on the VM (issue 411)

# disk_ok <free MB on />: succeeds when it is a number at least what one fresh dump plus the safety margin needs
disk_ok() { case "$1" in ''|*[!0-9]*) return 1 ;; esac; [ "$1" -ge "$DUMP_NEED_MB" ]; }

# check_dump_space: free space on / before the dump (the dump is about DUMP_RESERVE_MB; SAFE_MARGIN_MB must stay free after it)
check_dump_space() {
  local free
  free="$(vm 'df -Pm / | awk "NR==2{print \$4}"' 2>/dev/null | tr -d '\r' | head -n 1)"
  if disk_ok "$free"; then emit PASS dump_space "$free MB free on / (needs $DUMP_NEED_MB: a dump of about $DUMP_RESERVE_MB MB and $SAFE_MARGIN_MB MB left over)"; return 0; fi
  emit FAIL dump_space "${free:-could not read} MB free on / (needs $DUMP_NEED_MB: a dump of about $DUMP_RESERVE_MB MB and $SAFE_MARGIN_MB MB left over)"
  return 1
}

# take_fresh_dump <start epoch>: one dump by the backup container's own script (backup.sh once: pg_dump, pg_restore -l, content check,
# off-box copy, rotation; a rejected dump is kept aside as *.rejected). Then the newest dump must be readable (the same pg_restore -l
# as --status), at least the minimum size, and not older than the start of this deploy.
take_fresh_dump() {
  local out rc=0 last
  out="$(TMO=900 vm 'docker compose exec -T backup backup.sh once' 2>&1)" || rc=$?
  out="$(printf '%s\n' "$out" | tr -d '\r')"; last="$(printf '%s\n' "$out" | tail -n 1 | cut -c1-100)"
  printf '%s\n' "$out" | tail -n 5
  if [ "$rc" -ne 0 ]; then emit FAIL dump_taken "backup.sh once failed (exit $rc): $last"; return 1; fi
  emit PASS dump_taken "backup.sh once: $last"
  if ! dump_facts; then emit FAIL dump_valid "could not read the newest dump"; return 1; fi
  if [ "$D_VALID" != valid ] || [ "$D_SIZE" -lt "$MIN_DUMP_BYTES" ]; then emit FAIL dump_valid "$D_NAME: $D_SIZE bytes (min $MIN_DUMP_BYTES), readable: $D_VALID"; return 1; fi
  if [ "$D_EPOCH" -lt "$1" ]; then emit FAIL dump_valid "newest dump $D_NAME is older than the start of this deploy"; return 1; fi
  emit PASS dump_valid "$D_NAME, $D_SIZE bytes, pg_restore -l ok, newer than the start of this deploy"
}

# deploy_gate <commit>: the first step of make deploy, before anything changes. (1) the preflight checks, any FAIL stops; (2) free
# space, then a fresh dump; (3) that dump is valid and new. Any FAIL returns 1 here, so the pull, the image check and up never run.
deploy_gate() {
  local start
  DEPLOY_GATE=1 preflight "$1" || return $?
  if [ "$FAILS" -eq 0 ]; then
    start="$(vm_now)"
    if [ -z "$start" ]; then emit FAIL dump_taken "could not read the VM time"
    elif check_dump_space; then take_fresh_dump "$start"; fi
  fi
  if [ "$FAILS" -ne 0 ]; then
    summary deploy-gate
    echo "make deploy: stopped before anything changed" >&2
    return 1
  fi
  summary deploy-gate
}

# wait_healthy: after up, polls until every service the VM's compose file defines is healthy, or WAIT_HEALTHY_S passes. Reads only.
wait_healthy() {
  local t0 bad list
  t0="$(date +%s)"
  while :; do
    load_containers; list="$(vm_services)"
    if [ -z "$list" ]; then bad="could not read the VM's service list"
    else
      bad="$(unhealthy "$list")"
      if [ -z "$bad" ]; then emit PASS containers_healthy "all of: $list (after $(( $(date +%s) - t0 )) s)"; return 0; fi
    fi
    if [ $(( $(date +%s) - t0 )) -ge "$WAIT_HEALTHY_S" ]; then
      emit FAIL containers_healthy "not healthy after $WAIT_HEALTHY_S s: $bad"
      summary wait-healthy
      echo "make deploy: containers not healthy; the release marker was NOT written" >&2
      return 1
    fi
    sleep 10
  done
}

# ---------------------------------------------------------------- main

main() {
  case "${1:-}" in
    --preflight) preflight "${2:-}" ;;
    --verify) verify ;;
    --status) status ;;
    --mark) mark "${2:-}" "${3:-}" ;;
    --deploy-gate) deploy_gate "${2:-}" ;;
    --wait-healthy) wait_healthy ;;
    --deploy-mark) mark "${2:-}" --allow-silent-issues ;;   # make deploy, after a passing verify: silent issues warn, never refuse (issue 411)
    *) echo "usage: bash ops/deploy.sh --preflight <commit> | --verify | --status | --mark <commit> [--allow-silent-issues]" >&2; return 2 ;;
  esac
}

if [ -z "${DEPLOY_SOURCE_ONLY:-}" ]; then main "$@"; exit $?; fi
