#!/bin/bash
# Read-only deploy checks for the production VM, and the wait-for-healthy step of `make deploy`. Run from the owner's PC (Git Bash), repo root:
#   bash ops/deploy.sh --verify               after the deploy: is the stack healthy? (reads only; FAILS first if this checkout is not
#       at the commit the VM checkout is at: it counts invariants from the local ops/audit/*.sql, so it must be an up-to-date checkout)
#   bash ops/deploy.sh --status               what is live: containers, images, newest dump (reads only)
#   bash ops/deploy.sh --wait-healthy         waits up to DEPLOY_WAIT_S (default 300) for every service to be healthy; `make deploy` runs it on
#       the VM with DEPLOY_LOCAL=1 (the same commands run in this checkout, no ssh to itself)
# Registry mode (VATPAC_IMAGE_PREFIX set in the VM's .env, issue 173): the tag defaults to `latest`, which the release workflow
# moves to the newest published commit (no .env edit per deploy; a VATPAC_IMAGE_TAG=sha-<7> line is only a deliberate rollback pin).
# --verify checks each container's image revision label against the VM checkout; --status prints the images. Local mode (neither set)
# skips the image checks. Only the two image key names are read from .env.
# It never starts, stops, builds or restarts anything and never prints .env values. The owner's explicit go for each
# deploy is unchanged. Exit 0 means no FAIL (WARN is allowed).
# Settings (environment): DEPLOY_SSH (default vatpac-v3), DEPLOY_DIR (default ~/vatpac-statsV2), DEPLOY_LOCAL (1 = run here, not over ssh),
# DEPLOY_LOG_SINCE, DEPLOY_CMD_TIMEOUT (seconds per ssh call, default 60), DEPLOY_WAIT_S (default 300).

DEPLOY_SSH="${DEPLOY_SSH:-vatpac-v3}"
DEPLOY_DIR="${DEPLOY_DIR:-~/vatpac-statsV2}"
WARN_GAP_S=90      # a polling gap from here is a WARN (a restart costs about 45 s; 90 s is three missed polls)
FAIL_GAP_S=300     # a polling gap above this is a FAIL
CMD_TIMEOUT="${DEPLOY_CMD_TIMEOUT:-60}"
LOG_SINCE="${DEPLOY_LOG_SINCE:-6h}"
WAIT_HEALTHY_S="${DEPLOY_WAIT_S:-300}"
SERVICES="db collector grafana backup watchdog vatsim-atc-stats"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY=python; command -v python >/dev/null 2>&1 || PY=python3   # counts the invariant pack with ops/watchdog/integrity.py (stdlib only, no psycopg needed)
EXPECTED_FILE="${DEPLOY_EXPECTED_FILE:-$HERE/deploy_expected_warnings.txt}"
ROOT="$(cd "$HERE/.." && pwd)"
. "$HERE/env_key.sh"   # env_key: sent to the VM as text by vm_env_key, so the remote read is the same code as the local ones

VM_DOWN=0
g() { git -C "$ROOT" "$@"; }
PASSES=0; WARNS=0; FAILS=0

# ---------------------------------------------------------------- pure logic (no I/O except `date -d`)

# registry_mode <prefix> -> local (no prefix) | registry (prefix set; the tag defaults to latest)
registry_mode() { if [ -n "$1" ]; then echo registry; else echo local; fi; }

# short_tag <commit> -> sha-<first 7 characters>, the tag the release workflow gives an image
short_tag() { printf 'sha-%s' "${1:0:7}"; }

# head_matches <checkout HEAD> <image revision label>: the label (7 to 40 hex) is a prefix of HEAD
head_matches() { [[ "$2" =~ ^[0-9a-f]{7,40}$ ]] && [ "${1#"$2"}" != "$1" ]; }

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
# stale_hint <commit>: what to do when this checkout is not at the commit the VM runs. It must name THAT commit:
# origin/main may have moved on since the release.
stale_hint() { echo "run it from a checkout of the VM's commit, for example a fresh worktree: git worktree add --detach .worktrees/verify ${1:0:10}"; }

# dirty_ops: prints up to three changed or untracked files under ops/ ("?" when git cannot answer); empty when clean
dirty_ops() {
  local out
  out="$(g status --porcelain -- ops 2>/dev/null)" || { echo "?"; return; }
  printf '%s\n' "$out" | sed '/^$/d' | head -n 3 | cut -c4- | tr '\n' ' '
}

vm_now() { vm 'date -u +%s' 2>/dev/null | tr -d '\r' | grep -E '^[0-9]+$' | head -n 1; }

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
# deploy), so a service that is new in the commit being deployed is not "missing". Empty output = could not be read.
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

# ---------------------------------------------------------------- --verify

# check_checkout_clean: uncommitted edits under ops/ are what --verify would run, whatever HEAD says. Returns 1 when dirty.
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

# ---------------------------------------------------------------- --wait-healthy (make deploy on the VM)

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
      echo "make deploy: containers not healthy" >&2
      return 1
    fi
    sleep 10
  done
}

# ---------------------------------------------------------------- main

main() {
  case "${1:-}" in
    --verify) verify ;;
    --status) status ;;
    --wait-healthy) wait_healthy ;;
    *) echo "usage: bash ops/deploy.sh --verify | --status | --wait-healthy" >&2; return 2 ;;
  esac
}

if [ -z "${DEPLOY_SOURCE_ONLY:-}" ]; then main "$@"; exit $?; fi
