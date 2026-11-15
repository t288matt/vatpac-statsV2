#!/bin/sh
# The one definition of the repository checks (issue 241). CI (.github/workflows/ci.yml), the Makefile and a plain
# Git Bash on the owner's PC all run the checks through this script, so green here means green in CI.
#
#   sh ops/run_checks.sh <check-or-group> [<check-or-group> ...]
#
# Checks (each is one named CI step):
#   lint          ruff-collector mypy-collector ruff-loader mypy-loader ruff-ops mypy-ops shell-syntax
#   tests         pytest-unit unit-skips pytest-loader
#   repo-checks   config-drift dashboards dashboard-nav size-budget actionlint attribution
#   config        compose-config compose-images compose-blank-secrets      (needs the docker CLI; no daemon is used)
#   all           tool-versions lint tests repo-checks config
# `tool-versions` is local only: it compares the tools installed here with the versions ci.yml pins.
#
# Every named check runs even when an earlier one failed; each prints PASS or FAIL with its time, and the exit status is 1 if any
# failed. A missing tool is a failure, never a skip. NOT covered here (they need CI-only secrets, services or a registry): the
# secret scan (gitleaks), the integration tests, the collector's and the loader's (a Postgres service and the production migrate image), the image build, the
# dashboards-scope pytest runs, the scope job and the ci-result verify job.
# POSIX sh; runs on Linux and Git Bash for Windows.
set -u
cd "$(dirname "$0")/.." || exit 2
PY=python
command -v python >/dev/null 2>&1 || PY=python3
WORK="${RUNNER_TEMP:-${TMPDIR:-/tmp}}"
command -v cygpath >/dev/null 2>&1 && WORK="$(cygpath -m "$WORK")"
JUNIT="$WORK/vatpac-unit.xml"

need() { # need <command...>: fail loudly when a tool is missing
  "$@" >/dev/null 2>&1 || { echo "missing tool: $* (install it as CI does: see the pip install lines in .github/workflows/ci.yml)" >&2; return 1; }
}

ck_ruff_collector() { need $PY -m ruff --version && (cd collector && $PY -m ruff check .); }
ck_mypy_collector() { need $PY -m mypy --version && (cd collector && $PY -m mypy src); }
# The ATC stats loader (issue 387; stats_loader/pyproject.toml holds its ruff, mypy and pytest settings). Its unit tests run in
# their own pytest session: its test files share names with the collector's (test_settings.py and others).
ck_ruff_loader() { need $PY -m ruff --version && (cd stats_loader && $PY -m ruff check .); }
ck_mypy_loader() { need $PY -m mypy --version && (cd stats_loader && $PY -m mypy); }
ck_pytest_loader() { (cd stats_loader && $PY -m pytest -q -m "not integration" tests); }
ck_ruff_ops() { need $PY -m ruff --version && $PY -m ruff check --config ops/ruff.toml ops; }
ck_mypy_ops() {
  need $PY -m mypy --version && $PY -m mypy --strict --ignore-missing-imports \
    ops/watchdog/watchdog.py ops/watchdog/integrity.py ops/watchdog/messages.py ops/check_config_drift.py ops/check_size_budget.py \
    ops/check_panels.py ops/grafana_public.py ops/grafana_api.py ops/env_file.py ops/ci_scope.py ops/check_skips.py \
    ops/check_tool_versions.py
}
ck_shell_syntax() {
  rc=0
  for f in ops/*.sh ops/migrate/*.sh; do [ "$f" = ops/deploy.sh ] || [ "$f" = ops/stale_issues.sh ] || sh -n "$f" || rc=1; done
  for f in ops/backup/*.sh ops/deploy.sh ops/stale_issues.sh; do bash -n "$f" || rc=1; done
  return $rc
}
ck_pytest_unit() {
  rm -f "$JUNIT"
  PYTHONPATH=collector/src $PY -m pytest -q -m "not integration" tests collector/tests --ignore=tests/.venv --junitxml="$JUNIT"
}
# Issue 343: the expected number of skipped tests is kept in ops/check_skips.py; this reads the report the previous check wrote.
ck_unit_skips() { $PY ops/check_skips.py "$JUNIT"; }
ck_config_drift() { $PY ops/check_config_drift.py; }
# sqlglot is installed in CI, so every dashboard query is parsed as PostgreSQL; the grep fails the step if the validator ever goes
# back to "sql parsing: skipped". No pipe: the validator's exit status must not be hidden.
ck_dashboards() {
  out="$WORK/vatpac-dashboards.txt"
  $PY grafana/validate_dashboards.py > "$out"; rc=$?
  cat "$out"
  [ "$rc" -eq 0 ] || return 1
  grep -q '^sql statements parsed with sqlglot' "$out"
}
ck_dashboard_nav() { $PY ops/dashboard_nav.py --check; }
# The collector (1800 lines) and the ATC stats loader (700 lines, issue 387); both must pass, and both are run.
ck_size_budget() { rc=0; $PY ops/check_size_budget.py || rc=1; $PY ops/check_size_budget.py stats_loader/src/vatsim_stats 700 || rc=1; return $rc; }
# Shellcheck of the run scripts is off (-shellcheck=): it was not measured on these scripts, so it is a separate decision.
ck_actionlint() { need actionlint -version && actionlint -shellcheck= .github/workflows/*.yml; }
ck_attribution() { $PY ops/check_no_claude_attribution.py; }

# A throwaway env file: .env.example has blank secrets by design, so compose cannot be validated against it.
make_env() { envdir="$(mktemp -d)" && sh ops/gen_env.sh "$envdir/ci.env" >/dev/null; }
env_path() { if command -v cygpath >/dev/null 2>&1; then cygpath -m "$envdir/ci.env"; else echo "$envdir/ci.env"; fi; }
ck_compose_config() {
  need docker compose version || return 1
  make_env || return 1
  f="$(env_path)"; rc=0
  sh ops/check_env.sh "$f" || rc=1
  docker compose --env-file "$f" config -q || rc=1
  docker compose --env-file "$f" -f docker-compose.yml -f docker-compose.dev.yml config -q || rc=1
  rm -rf "$envdir"; return $rc
}
# The five built services must resolve to vatpac-stats-<name>:latest with both variables unset, to <prefix>vatpac-stats-<name>:latest
# with the prefix alone, and to <prefix>...:<tag> with a tag override (the names the release workflow publishes; issue 173).
ck_compose_images() {
  need docker compose version || return 1
  make_env || return 1
  f="$(env_path)"; want="collector watchdog backup migrate loader"; rc=0
  got="$(env -u VATPAC_IMAGE_PREFIX -u VATPAC_IMAGE_TAG docker compose --env-file "$f" config --images | grep 'vatpac-stats-' | sort)"
  exp="$(for n in $want; do echo "vatpac-stats-$n:latest"; done | sort)"
  [ "$got" = "$exp" ] || { echo "local mode images differ"; echo "$got"; rc=1; }
  got="$(env -u VATPAC_IMAGE_TAG VATPAC_IMAGE_PREFIX=ghcr.io/t288matt/ docker compose --env-file "$f" config --images | grep 'vatpac-stats-' | sort)"
  exp="$(for n in $want; do echo "ghcr.io/t288matt/vatpac-stats-$n:latest"; done | sort)"
  [ "$got" = "$exp" ] || { echo "registry default images differ"; echo "$got"; rc=1; }
  got="$(VATPAC_IMAGE_PREFIX=ghcr.io/t288matt/ VATPAC_IMAGE_TAG=sha-abc1234 docker compose --env-file "$f" config --images | grep 'vatpac-stats-' | sort)"
  exp="$(for n in $want; do echo "ghcr.io/t288matt/vatpac-stats-$n:sha-abc1234"; done | sort)"
  [ "$got" = "$exp" ] || { echo "registry mode images differ"; echo "$got"; rc=1; }
  rm -rf "$envdir"; return $rc
}
ck_compose_blank_secrets() {
  need docker compose version || return 1
  if docker compose --env-file .env.example config -q 2>/dev/null; then
    echo ".env.example boots with blank secrets"; return 1
  fi
}
ck_tool_versions() { $PY ops/check_tool_versions.py; }

run_one() { # run_one <check-name>
  name="$1"
  fn="ck_$(printf '%s' "$name" | tr '-' '_')"
  printf '\n=== %s\n' "$name"
  start="$(date +%s)"
  if ( "$fn" ); then res=PASS; else res=FAIL; fi
  printf '%s  %s (%ss)\n' "$res" "$name" "$(( $(date +%s) - start ))"
  [ "$res" = PASS ]
}

expand() { # a group or a check name to its checks
  case "$1" in
    lint) echo "ruff-collector mypy-collector ruff-loader mypy-loader ruff-ops mypy-ops shell-syntax" ;;
    tests) echo "pytest-unit unit-skips pytest-loader" ;;
    repo-checks) echo "config-drift dashboards dashboard-nav size-budget actionlint attribution" ;;
    config) echo "compose-config compose-images compose-blank-secrets" ;;
    all) echo "tool-versions $(expand lint) $(expand tests) $(expand repo-checks) $(expand config)" ;;
    ruff-collector|mypy-collector|ruff-loader|mypy-loader|ruff-ops|mypy-ops|shell-syntax|pytest-unit|unit-skips|pytest-loader|config-drift|dashboards|dashboard-nav|size-budget|actionlint|attribution|compose-config|compose-images|compose-blank-secrets|tool-versions) echo "$1" ;;
    *) echo "unknown check or group: $1 (see the header of ops/run_checks.sh)" >&2; return 1 ;;
  esac
}

[ "$#" -gt 0 ] || { sed -n '2,22p' "$0" >&2; exit 2; }
list=""
for a in "$@"; do list="$list $(expand "$a")" || exit 2; done
failed=""
for c in $list; do run_one "$c" || failed="$failed $c"; done
printf '\n'
if [ -z "$failed" ]; then echo "run_checks: all passed:$list"; exit 0; fi
echo "run_checks: FAILED:$failed"; exit 1
