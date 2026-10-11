#!/bin/bash
# Silent-issue check (issue 104, owner decision 11-10-2026). Read-only: git log and `gh issue list`; writes nothing anywhere.
#   bash ops/stale_issues.sh                    commits on origin/main since the last release marker (read over ssh, like deploy.sh --status)
#   bash ops/stale_issues.sh --since <sha|date> commits since that commit, or since that date (anything `git log --since` accepts)
#   bash ops/stale_issues.sh --range <a>..<b>   exactly that commit range (used by deploy.sh --mark)
# Lists every OPEN issue that a commit in the range names (#93, "issues 93, 94", "Refs #N") and that has no comment newer
# than its newest such commit. Output: number, title, last comment date, newest commit. Exit 0 none, 1 some, 2 could not check.
# Settings (environment): GH_BIN (default gh on PATH, else "C:\Program Files\GitHub CLI\gh.exe"), REPO (default t288matt/vatpac-statsV2),
# DEPLOY_SSH (default vatpac-v3), DEPLOY_DIR (default ~/vatpac-statsV2).

REPO="${REPO:-t288matt/vatpac-statsV2}"
DEPLOY_SSH="${DEPLOY_SSH:-vatpac-v3}"
DEPLOY_DIR="${DEPLOY_DIR:-~/vatpac-statsV2}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
g() { git -C "$ROOT" "$@"; }
gh_bin() {
  if [ -n "${GH_BIN:-}" ]; then echo "$GH_BIN"; return; fi
  command -v gh 2>/dev/null || echo "C:\\Program Files\\GitHub CLI\\gh.exe"
}

# issue_refs: stdin is `git log --format=%x01%H%x1f%cI%x1f%B`; prints "number<TAB>sha<TAB>iso date", once per commit and number.
# Matches "#93" (also "#93, #94") and the word forms "issue 93", "issues 93, 94 and 95", "issues #93, 94".
issue_refs() {
  awk 'BEGIN { RS = "\001"; FS = "\037" }
    NF >= 3 {
      sha = $1; d = $2; txt = $3; delete seen
      while (match(txt, /#[0-9]+|[Ii]ssues?:? *#?[0-9]+( *(,|and|&) *#?[0-9]+)*/)) {
        tok = substr(txt, RSTART, RLENGTH); txt = substr(txt, RSTART + RLENGTH)
        while (match(tok, /[0-9]+/)) {
          n = substr(tok, RSTART, RLENGTH); tok = substr(tok, RSTART + RLENGTH)
          if (length(n) <= 6 && !(n in seen)) { seen[n] = 1; print n "\t" sha "\t" d }
        }
      }
    }'
}

# last_marker_commit: the commit in the VM release marker, empty when there is none or the VM cannot be read.
last_marker_commit() {
  # shellcheck disable=SC2029
  ssh -n -o BatchMode=yes -o ConnectTimeout=10 "$DEPLOY_SSH" "cd $DEPLOY_DIR && cat .deploy/release" 2>/dev/null \
    | tr -d '\r' | sed -n 's/^commit=\([0-9a-fA-F]\{7,40\}\)$/\1/p' | head -n 1
}

main() {
  local mode="" arg="" log refs open n sha iso ep c_ep last rows="" count=0
  local -a range
  while [ $# -gt 0 ]; do
    case "$1" in
      --since|--range) mode="$1"; arg="${2:-}"; [ -n "$arg" ] || { echo "$1 needs a value" >&2; return 2; }; shift 2 ;;
      *) echo "usage: bash ops/stale_issues.sh [--since <sha|date> | --range <a>..<b>]" >&2; return 2 ;;
    esac
  done
  if [ "$mode" = --range ]; then
    range=("$arg")
  else
    g fetch -q origin main 2>/dev/null
    if [ -z "$mode" ]; then
      arg="$(last_marker_commit)"
      [ -n "$arg" ] || { echo "ERROR  could not read the release marker; pass --since <sha|date>" >&2; return 2; }
      echo "since the release marker ${arg:0:10}"
    fi
    if [[ "$arg" =~ ^[0-9a-fA-F]{7,40}$ ]] && g rev-parse --verify -q "${arg}^{commit}" >/dev/null 2>&1; then
      range=("${arg}..origin/main")
    else
      range=("--since=$arg" origin/main)
    fi
  fi
  log="$(g log --format='%x01%H%x1f%cI%x1f%B' "${range[@]}" 2>/dev/null)" || { echo "ERROR  git log failed" >&2; return 2; }
  refs="$(printf '%s' "$log" | issue_refs)"
  if [ -z "$refs" ]; then echo "no issue numbers in the commits checked"; return 0; fi

  # open issues, one per line: number TAB title TAB newest comment time (empty when none)
  open="$("$(gh_bin)" issue list --repo "$REPO" --state open --limit 500 --json number,title,comments \
    --jq '.[] | [.number, .title, ((.comments | map(.createdAt) | max) // "")] | @tsv' 2>/dev/null | tr -d '\r'; exit "${PIPESTATUS[0]}")" \
    || { echo "ERROR  gh could not list the open issues of $REPO" >&2; return 2; }

  declare -A newest_ep newest_sha newest_iso title cdate
  while IFS=$'\t' read -r n sha iso; do
    ep="$(date -u -d "$iso" +%s 2>/dev/null)" || { echo "ERROR  unreadable commit date '$iso'" >&2; return 2; }
    if [ -z "${newest_ep[$n]:-}" ] || [ "$ep" -gt "${newest_ep[$n]}" ]; then newest_ep[$n]="$ep"; newest_sha[$n]="${sha:0:10}"; newest_iso[$n]="$iso"; fi
  done <<EOF
$refs
EOF
  while IFS=$'\t' read -r n sha iso; do
    if [ -n "$n" ]; then title[$n]="$sha"; cdate[$n]="$iso"; fi
  done <<EOF
$open
EOF
  for n in $(printf '%s\n' "${!newest_ep[@]}" | sort -n); do
    [ -n "${title[$n]+x}" ] || continue            # not an open issue (closed, or a pull request number)
    last="${cdate[$n]}"
    if [ -n "$last" ]; then c_ep="$(date -u -d "$last" +%s 2>/dev/null)" || c_ep=0; else c_ep=0; fi
    [ "$c_ep" -gt "${newest_ep[$n]}" ] && continue
    count=$((count + 1))
    rows+="$(printf '#%s\t%s\t%s\t%s %s' "$n" "${title[$n]:0:60}" "${last:-no comments}" "${newest_sha[$n]}" "${newest_iso[$n]}")"$'\n'
  done
  if [ "$count" -eq 0 ]; then echo "no silent issues"; return 0; fi
  echo "SILENT ISSUES ($count): a commit is newer than the last comment"
  { printf 'issue\ttitle\tlast comment\tnewest commit\n'; printf '%s' "$rows"; } | column -t -s $'\t' 2>/dev/null || printf '%s' "$rows"
  return 1
}

if [ -z "${STALE_SOURCE_ONLY:-}" ]; then main "$@"; exit $?; fi
