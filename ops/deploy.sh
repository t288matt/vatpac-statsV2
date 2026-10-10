#!/bin/bash
# Read-only deploy checks for the production VM (issue 104, trimmed slice). Run from the owner's PC (Git Bash), repo root:
#   bash ops/deploy.sh --preflight <commit>   before the deploy: is it safe to start? (reads only)
#   bash ops/deploy.sh --verify               after the deploy: is the stack healthy? (reads only)
#   bash ops/deploy.sh --status               what is live: release marker, containers, newest dump (reads only)
#   bash ops/deploy.sh --mark <commit>        after a VERIFIED deploy: write ~/vatpac-statsV2/.deploy/release (the ONLY write)
# It never starts, stops, builds or restarts anything and never prints .env values. The owner's explicit go for each
# deploy is unchanged; this script only replaces remembering the checks. Exit 0 means no FAIL (WARN is allowed).
# Settings (environment): DEPLOY_SSH (default vatpac-v3), DEPLOY_DIR (default ~/vatpac-statsV2), DEPLOY_GH (path to gh),
# DEPLOY_REPO (default t288matt/vatpac-statsV2), DEPLOY_MIN_MEM_MB, DEPLOY_MAX_DISK_PCT, DEPLOY_MAX_DUMP_AGE_H,
# DEPLOY_MIN_DUMP_BYTES, DEPLOY_MAX_GAP_S, DEPLOY_LOG_SINCE.

DEPLOY_SSH="${DEPLOY_SSH:-vatpac-v3}"
DEPLOY_DIR="${DEPLOY_DIR:-~/vatpac-statsV2}"
DEPLOY_REPO="${DEPLOY_REPO:-t288matt/vatpac-statsV2}"
MIN_MEM_MB="${DEPLOY_MIN_MEM_MB:-3000}"
MAX_DISK_PCT="${DEPLOY_MAX_DISK_PCT:-80}"
MAX_DUMP_AGE_H="${DEPLOY_MAX_DUMP_AGE_H:-26}"
MIN_DUMP_BYTES="${DEPLOY_MIN_DUMP_BYTES:-1000000}"
MAX_GAP_S="${DEPLOY_MAX_GAP_S:-90}"
LOG_SINCE="${DEPLOY_LOG_SINCE:-6h}"
SERVICES="db collector grafana backup watchdog"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXPECTED_FILE="${DEPLOY_EXPECTED_FILE:-$HERE/deploy_expected_warnings.txt}"
BACKUP_WINDOW_START=6600   # 01:50 UTC, seconds since midnight
BACKUP_WINDOW_END=9000     # 02:30 UTC

VM_DOWN=0
PASSES=0; WARNS=0; FAILS=0

# ---------------------------------------------------------------- pure logic (no I/O except `date -d`)

# mem_ok <MemAvailable in MB>: succeeds when it is a number at least the minimum.
mem_ok() { case "$1" in ''|*[!0-9]*) return 1 ;; esac; [ "$1" -ge "$MIN_MEM_MB" ]; }

# in_backup_window <epoch seconds>: succeeds from 01:50 to 02:30 UTC inclusive.
in_backup_window() {
  local s=$(( $1 % 86400 ))
  [ "$s" -ge "$BACKUP_WINDOW_START" ] && [ "$s" -le "$BACKUP_WINDOW_END" ]
}

# dmy_to_epoch "DD-MM-YYYY HH:MM" -> epoch seconds (UTC); fails on a malformed value.
dmy_to_epoch() {
  [[ "$1" =~ ^([0-9]{2})-([0-9]{2})-([0-9]{4})\ ([0-9]{2}):([0-9]{2})$ ]] || return 1
  date -u -d "${BASH_REMATCH[3]}-${BASH_REMATCH[2]}-${BASH_REMATCH[1]} ${BASH_REMATCH[4]}:${BASH_REMATCH[5]}:00" +%s 2>/dev/null
}

# expected_warning_status <check name> <now epoch> <file> -> prints: none | valid <expiry> | expired <expiry> | malformed <expiry>
# File lines: name|DD-MM-YYYY HH:MM (UTC expiry)|reason ; # starts a comment. A name must match exactly.
expected_warning_status() {
  local want="$1" now="$2" file="$3" name exp reason ep
  [ -r "$file" ] || { echo none; return 0; }
  while IFS='|' read -r name exp reason; do
    case "$name" in ''|'#'*) continue ;; esac
    [ "$name" = "$want" ] || continue
    if ! ep="$(dmy_to_epoch "$exp")" || [ -z "$ep" ]; then echo "malformed $exp"; return 0; fi
    if [ "$now" -lt "$ep" ]; then echo "valid $exp"; else echo "expired $exp"; fi
    return 0
  done < "$file"
  echo none
}

# classify_invariant <name> <violations> <now epoch> <file> -> prints "STATUS|value"
classify_invariant() {
  local name="$1" n="$2" now="$3" file="$4" st
  case "$n" in ''|*[!0-9]*) echo "FAIL|$name: unreadable count '$n'"; return 0 ;; esac
  if [ "$n" -eq 0 ]; then echo "PASS|"; return 0; fi
  st="$(expected_warning_status "$name" "$now" "$file")"
  case "$st" in
    valid*) echo "WARN|$name = $n (expected until ${st#valid } UTC)" ;;
    expired*) echo "FAIL|$name = $n (the expected-warning entry expired ${st#expired } UTC)" ;;
    malformed*) echo "FAIL|$name = $n (malformed expiry '${st#malformed }' in the expected-warnings file)" ;;
    *) echo "FAIL|$name = $n" ;;
  esac
}

# gap_ok <max gap seconds> <age of newest row seconds>: both numbers and under the limit.
gap_ok() {
  case "$1$2" in *[!0-9]*|'') return 1 ;; esac
  [ "$1" -lt "$MAX_GAP_S" ] && [ "$2" -lt "$MAX_GAP_S" ]
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

vm()    { [ "$VM_DOWN" = 1 ] && return 255; ssh -n -o BatchMode=yes -o ConnectTimeout=10 "$DEPLOY_SSH" "cd $DEPLOY_DIR && $1"; }
vm_in() { [ "$VM_DOWN" = 1 ] && return 255; ssh -o BatchMode=yes -o ConnectTimeout=10 "$DEPLOY_SSH" "cd $DEPLOY_DIR && $1"; }

gh_bin() {
  if [ -n "${DEPLOY_GH:-}" ]; then echo "$DEPLOY_GH"; return; fi
  command -v gh 2>/dev/null || echo "/c/Program Files/GitHub CLI/gh.exe"
}

# psql inside the db container, read-only, SQL on stdin. The single quotes keep $POSTGRES_* for the container's shell.
PSQL='docker compose exec -T db sh -c '\''psql -X -q -tA -F"|" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1'\'''
# One line per container: name|started|restarts|health-or-state
INSPECT='docker inspect -f "{{.Name}}|{{.State.StartedAt}}|{{.RestartCount}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}" $(docker compose ps -aq)'
# Newest dump: epoch size name (validity checked with pg_restore -l in the backup container, which only reads)
NEWEST_DUMP='docker compose exec -T backup sh -c '\''f=$(find /backups -maxdepth 1 -name "*.dump" -printf "%T@ %s %f\n" | sort -n | tail -n 1); echo "$f"; n=${f##* }; [ -n "$n" ] && pg_restore -l "/backups/$n" >/dev/null 2>&1 && echo valid || echo invalid'\'''

vm_now() { vm 'date -u +%s' 2>/dev/null | tr -d '\r' | grep -E '^[0-9]+$' | head -n 1; }
iso() { date -u -d "@$1" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null; }

# ---------------------------------------------------------------- shared container facts

CONTAINERS=""
load_containers() { CONTAINERS="$(vm "$INSPECT" 2>/dev/null | tr -d '\r' | grep '|')"; }

# svc_line <service>: the inspect line of the service's container
svc_line() { printf '%s\n' "$CONTAINERS" | grep -E "^/vatpac-$1-[0-9]+\|" | head -n 1; }

check_healthy() {
  local bad="" svc l h
  [ -n "$CONTAINERS" ] || { emit FAIL containers_healthy "could not read the container list"; return; }
  for svc in $SERVICES; do
    l="$(svc_line "$svc")"; h="$(printf '%s' "$l" | cut -d'|' -f4)"
    [ "$h" = healthy ] || bad="$bad $svc=${h:-missing}"
  done
  if [ -z "$bad" ]; then emit PASS containers_healthy "all of: $SERVICES"; else emit FAIL containers_healthy "not healthy:$bad"; fi
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
  local want="$1" full tip behind now out ci_lines ci_bad pct free tdc mem inwin foreign dumpage ageh reach
  [[ "$want" =~ ^[0-9a-fA-F]{7,40}$ ]] || { echo "usage: deploy.sh --preflight <commit (7 to 40 hex characters)>" >&2; return 2; }
  echo "deploy preflight for $want against $DEPLOY_SSH (read-only)"

  # commit on origin/main
  git fetch -q origin main 2>/dev/null
  full="$(git rev-parse --verify -q "${want}^{commit}" 2>/dev/null)"
  tip="$(git rev-parse --verify -q origin/main 2>/dev/null)"
  if [ -z "$full" ] || [ -z "$tip" ]; then
    emit FAIL commit_on_main "cannot resolve '$want' or origin/main"
  elif ! git merge-base --is-ancestor "$full" "$tip" 2>/dev/null; then
    emit FAIL commit_on_main "${full:0:10} is not on origin/main"
  elif [ "$full" = "$tip" ]; then
    emit PASS commit_on_main "${full:0:10} is the tip of origin/main"
  else
    behind="$(git rev-list --count "$full..$tip" 2>/dev/null)"
    emit PASS commit_on_main "${full:0:10} is on origin/main (${behind:-?} newer commit(s) after it)"
  fi

  # CI green for that exact sha: every run for it must be completed/success, and there must be at least one
  if [ -z "$full" ]; then
    emit FAIL ci_green "no commit to look up"
  else
    ci_lines="$("$(gh_bin)" run list --repo "$DEPLOY_REPO" --branch main --limit 50 \
      --json headSha,workflowName,status,conclusion \
      --jq ".[] | select(.headSha==\"$full\") | .workflowName + \" \" + .status + \" \" + (.conclusion // \"\")" 2>/dev/null | tr -d '\r')"
    if [ -z "$ci_lines" ]; then
      emit FAIL ci_green "no CI run found for ${full:0:10} (gh missing, not logged in, or no run)"
    else
      ci_bad="$(printf '%s\n' "$ci_lines" | grep -vE ' completed success$')"
      if [ -z "$ci_bad" ]; then emit PASS ci_green "$(printf '%s\n' "$ci_lines" | wc -l | tr -d ' ') run(s) completed success for ${full:0:10}"
      else emit FAIL ci_green "$(printf '%s' "$ci_bad" | head -n 1)"; fi
    fi
  fi

  # VM reachable and its clock
  reach="$(vm 'echo reachable' 2>/dev/null | tr -d '\r')"
  if [ "$reach" != reachable ]; then
    VM_DOWN=1; emit FAIL vm_reachable "ssh $DEPLOY_SSH did not answer"
  else
    emit PASS vm_reachable "ssh $DEPLOY_SSH answers"
  fi
  tdc="$(vm 'timedatectl' 2>/dev/null | tr -d '\r')"
  if printf '%s\n' "$tdc" | grep -q 'System clock synchronized: yes' && printf '%s\n' "$tdc" | grep -q 'NTP service: active'; then
    emit PASS vm_clock "synchronized: yes, NTP service: active"
  else
    emit FAIL vm_clock "$(printf '%s\n' "$tdc" | grep -E 'synchronized|NTP service' | tr -s ' ' | tr '\n' ';' | cut -c1-120)${tdc:+}${tdc:-could not run timedatectl}"
  fi

  # MemAvailable (the kernel's estimate), never the "free" column
  mem="$(vm 'awk "/^MemAvailable:/{print int(\$2/1024)}" ${DEPLOY_MEMINFO:-/proc/meminfo}' 2>/dev/null | tr -d '\r' | head -n 1)"
  if mem_ok "$mem"; then emit PASS mem_available "$mem MB (needs $MIN_MEM_MB)"
  else emit FAIL mem_available "${mem:-could not read} MB (needs $MIN_MEM_MB)"; fi

  # disk
  out="$(vm 'df -P / | awk "NR==2{print \$5, int(\$4/1024/1024)}"' 2>/dev/null | tr -d '\r' | head -n 1)"
  pct="${out%% *}"; pct="${pct%\%}"; free="${out##* }"
  case "$pct" in ''|*[!0-9]*) emit FAIL disk_free "could not read" ;;
    *) if [ "$pct" -le "$MAX_DISK_PCT" ]; then emit PASS disk_free "/ ${pct}% used, ${free} GB free (limit ${MAX_DISK_PCT}%)"
       else emit FAIL disk_free "/ ${pct}% used, ${free} GB free (limit ${MAX_DISK_PCT}%)"; fi ;;
  esac

  # containers
  load_containers
  check_healthy
  check_restarts

  # backup window (VM time)
  now="$(vm_now)"
  if [ -z "$now" ]; then emit FAIL backup_window "could not read the VM time"
  elif in_backup_window "$now"; then emit FAIL backup_window "$(iso "$now" | cut -c12-16) UTC is inside 01:50 to 02:30 UTC (backup and restore test)"
  else emit PASS backup_window "$(iso "$now" | cut -c12-16) UTC is outside 01:50 to 02:30 UTC"; fi

  # leftovers that are not part of the stack
  out="$(vm "docker ps -a --format '{{.Names}}'" 2>/dev/null | tr -d '\r')"
  if [ -z "$out" ]; then emit FAIL foreign_containers "could not list containers"
  else
    foreign="$(printf '%s\n' "$out" | grep -v '^vatpac-' | tr '\n' ' ')"
    if [ -z "$foreign" ]; then emit PASS foreign_containers "none"; else emit FAIL foreign_containers "non-vatpac containers present: $foreign"; fi
  fi

  # newest dump: recent, big enough, and readable by pg_restore -l
  if ! dump_facts; then emit FAIL recent_dump "could not read the backup volume"
  elif [ -z "$now" ]; then emit FAIL recent_dump "no VM time to compute the age"
  else
    dumpage=$(( now - D_EPOCH )); ageh=$(( dumpage / 3600 ))
    if [ "$D_VALID" = valid ] && [ "$D_SIZE" -ge "$MIN_DUMP_BYTES" ] && [ "$ageh" -lt "$MAX_DUMP_AGE_H" ]; then
      emit PASS recent_dump "$D_NAME, ${ageh} h old, $D_SIZE bytes, pg_restore -l ok"
    else
      emit FAIL recent_dump "$D_NAME, ${ageh} h old (max $MAX_DUMP_AGE_H), $D_SIZE bytes (min $MIN_DUMP_BYTES), readable: $D_VALID"
    fi
  fi

  # mid-deploy flag: the marker folder holds a "lock" file only if somebody put it there by hand
  out="$(vm 'if [ -e .deploy/lock ]; then echo "LOCKED: $(cat .deploy/lock 2>/dev/null | head -c 100)"; else echo free; fi' 2>/dev/null | tr -d '\r' | head -n 1)"
  case "$out" in
    free) emit PASS not_mid_deploy "no .deploy/lock on the VM" ;;
    LOCKED*) emit FAIL not_mid_deploy "$out" ;;
    *) emit FAIL not_mid_deploy "could not read the VM tree" ;;
  esac

  summary preflight
}

# ---------------------------------------------------------------- --verify

verify() {
  local out rc now line name n detail res st age maxgap rows tb bad first lines hits rows3 sample
  echo "deploy verify against $DEPLOY_SSH (read-only)"
  out="$(vm 'echo reachable' 2>/dev/null | tr -d '\r')"
  if [ "$out" != reachable ]; then VM_DOWN=1; emit FAIL vm_reachable "ssh $DEPLOY_SSH did not answer"; else emit PASS vm_reachable "ssh answers"; fi

  # existing scripts
  out="$(vm 'sh ops/smoke.sh' 2>&1 | tr -d '\r')"; rc=$?
  if printf '%s\n' "$out" | tail -n 1 | grep -q '^SMOKE PASS$'; then emit PASS smoke "SMOKE PASS ($(printf '%s\n' "$out" | grep -c '^PASS') checks)"
  else emit FAIL smoke "$(printf '%s\n' "$out" | grep '^FAIL' | head -n 2 | tr '\n' ';')${out:+}${out:-could not run ops/smoke.sh}" ; fi

  out="$(vm 'python3 ops/check_panels.py' 2>&1 | tr -d '\r')"
  line="$(printf '%s\n' "$out" | grep -E '^panel queries: ' | tail -n 1)"
  if [ -z "$line" ]; then emit FAIL panels "could not run ops/check_panels.py"
  elif printf '%s' "$line" | grep -qE ' 0 errors$'; then emit PASS panels "$line"
  else emit FAIL panels "$line"; fi

  # containers
  load_containers
  check_restarts
  line="$(svc_line collector)"
  if [ -n "$line" ]; then emit PASS collector_started "$(printf '%s' "$line" | cut -d'|' -f2 | cut -c1-19) UTC (health: $(printf '%s' "$line" | cut -d'|' -f4))"
  else emit FAIL collector_started "collector container not found"; fi

  # polling gap over the newest 20 poll_log rows, and age of the newest row
  out="$(printf '%s\n' "SET default_transaction_read_only = on; SELECT coalesce(max(gap),-1)::int, (SELECT extract(epoch FROM now() - max(polled_at))::int FROM poll_log), count(*) FROM (SELECT extract(epoch FROM polled_at - lag(polled_at) OVER (ORDER BY polled_at)) AS gap FROM (SELECT polled_at FROM poll_log ORDER BY polled_at DESC LIMIT 20) q) g;" \
        | vm_in "$PSQL" 2>/dev/null | tr -d '\r' | grep -E '^-?[0-9]+\|-?[0-9]+\|[0-9]+$' | head -n 1)"
  maxgap="${out%%|*}"; rows="${out##*|}"; age="${out#*|}"; age="${age%%|*}"
  if [ -z "$out" ]; then emit FAIL polling_gap "could not read poll_log"
  elif [ "$rows" -lt 5 ]; then emit FAIL polling_gap "only $rows poll_log rows"
  elif gap_ok "$maxgap" "$age"; then emit PASS polling_gap "longest gap $maxgap s over the newest $rows rows, newest row $age s old (limit $MAX_GAP_S)"
  else emit FAIL polling_gap "longest gap $maxgap s over the newest $rows rows, newest row $age s old (limit $MAX_GAP_S)"; fi

  # collector /health, including the archive block (runbook 4c.3)
  out="$(vm 'docker compose exec -T collector python -c "import os,urllib.request;r=urllib.request.urlopen(\"http://127.0.0.1:%s/health\"%os.environ.get(\"HEALTH_PORT\",\"8080\"),timeout=5);print(r.status);print(r.read().decode())"' 2>/dev/null | tr -d '\r')"
  if [ "$(printf '%s\n' "$out" | sed -n 1p)" != 200 ]; then emit FAIL health "no HTTP 200 from the collector /health"
  else
    sample="$(printf '%s\n' "$out" | sed -n 2p | sed -n 's/.*"archive": *{\([^}]*\)}.*/\1/p')"
    if printf '%s' "$sample" | grep -q '"enabled": *true' && printf '%s' "$sample" | grep -q '"errors": *0,'; then
      emit PASS health "200; archive enabled, errors 0, last_write_age_seconds $(printf '%s' "$sample" | sed -n 's/.*"last_write_age_seconds": *\([0-9]*\).*/\1/p')"
    else emit FAIL health "200 but the archive block is missing, disabled or has errors: ${sample:-none}"; fi
  fi

  # identity flap: three zero counts
  out="$( { echo "SET default_transaction_read_only = on;"; cat "$HERE/check_identity_flap.sql"; } | vm_in "$PSQL" 2>/dev/null | tr -d '\r' | grep -E '\|[0-9]+\|' )"
  if [ "$(printf '%s\n' "$out" | grep -c .)" -ne 3 ]; then emit FAIL identity_flap "could not read three result rows"
  else
    bad="$(printf '%s\n' "$out" | awk -F'|' '$2 != "0" {n++} END {print n+0}')"
    if [ "$bad" -eq 0 ]; then emit PASS identity_flap "0, 0, 0"; else emit FAIL identity_flap "counts: $(printf '%s\n' "$out" | cut -d'|' -f2 | tr '\n' ' ')"; fi
  fi

  # invariant pack, read-only, compared with the expected-warnings file
  now="$(vm_now)"
  out="$( { echo "SET default_transaction_read_only = on;"; cat "$HERE/audit/invariants.sql"; } | vm_in "$PSQL" 2>/dev/null | tr -d '\r' | grep -E '\|[0-9]+\|' )"
  n="$(printf '%s\n' "$out" | grep -c .)"
  if [ -z "$now" ]; then emit FAIL invariants "could not read the VM time"
  elif [ "$n" -lt 1 ]; then emit FAIL invariants "no invariant rows came back"
  else
    bad=0; rows3=0
    while IFS='|' read -r name cnt detail; do
      res="$(classify_invariant "$name" "$cnt" "$now" "$EXPECTED_FILE")"; st="${res%%|*}"
      rows3=$((rows3 + 1))
      [ "$st" = PASS ] && continue
      emit "$st" "invariant" "${res#*|}"; [ "$st" = FAIL ] && bad=1
    done <<EOF
$out
EOF
    [ "$bad" -eq 0 ] && emit PASS invariants "$rows3 invariants checked, none failing"
  fi

  # watchdog log: no Traceback, no failed alert formatting
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
  echo "== release marker"
  out="$(vm 'cat .deploy/release 2>/dev/null || echo "no marker: no deploy has been marked yet"' 2>/dev/null | tr -d '\r')"
  printf '%s\n' "${out:-could not read the VM tree}"
  echo "== containers (name | started | restarts | health)"
  load_containers
  if [ -z "$CONTAINERS" ]; then echo "could not read the container list"; else printf '%s\n' "$CONTAINERS"; fi
  echo "== newest dump"
  now="$(vm_now)"
  if dump_facts && [ -n "$now" ]; then echo "$D_NAME, $(( (now - D_EPOCH) / 3600 )) h old, $D_SIZE bytes, pg_restore -l: $D_VALID"
  else echo "could not read the backup volume"; fi
}

# ---------------------------------------------------------------- --mark (the only write)

mark() {
  local want="$1" full now stamp who
  [[ "$want" =~ ^[0-9a-fA-F]{7,40}$ ]] || { echo "usage: deploy.sh --mark <commit (7 to 40 hex characters)>" >&2; return 2; }
  full="$(git rev-parse --verify -q "${want}^{commit}" 2>/dev/null)"
  [ -n "$full" ] || { echo "FAIL  cannot resolve '$want' to a commit" >&2; return 1; }
  now="$(vm_now)"; [ -n "$now" ] || { echo "FAIL  could not read the VM time; nothing written" >&2; return 1; }
  stamp="$(iso "$now")"; who="${USER:-${USERNAME:-unknown}}"
  who="$(printf '%s' "$who" | tr -cd 'A-Za-z0-9._-')"
  printf 'commit=%s\nmarked_utc=%s\nmarked_by=%s\n' "$full" "$stamp" "$who" \
    | vm_in 'mkdir -p .deploy && cat > .deploy/release.new && mv .deploy/release.new .deploy/release && cat .deploy/release >> .deploy/release.log && echo "---" >> .deploy/release.log' \
    || { echo "FAIL  could not write the marker" >&2; return 1; }
  echo "PASS  marked ${full:0:10} at $stamp by $who (~/vatpac-statsV2/.deploy/release on the VM)"
}

# ---------------------------------------------------------------- main

main() {
  case "${1:-}" in
    --preflight) preflight "${2:-}" ;;
    --verify) verify ;;
    --status) status ;;
    --mark) mark "${2:-}" ;;
    *) echo "usage: bash ops/deploy.sh --preflight <commit> | --verify | --status | --mark <commit>" >&2; return 2 ;;
  esac
}

if [ -z "${DEPLOY_SOURCE_ONLY:-}" ]; then main "$@"; exit $?; fi
