CI = ".github/workflows/ci.yml"
RL = ".github/workflows/release.yml"
CS = "ops/ci_scope.py"
py("W01", "workflow", RL, "github.event.workflow_run.conclusion == 'success' && github.event.workflow_run.event == 'push'", "github.event.workflow_run.conclusion != 'cancelled' && github.event.workflow_run.event == 'push'", ["UNIT"])
py("W02", "workflow", RL, "needs: [check, images]", "needs: [check]", ["UNIT"])
py("W03", "workflow", CI, "cancel-in-progress: ${{ github.event_name == 'pull_request' }}", "cancel-in-progress: true", ["UNIT"])
# W04, W05: two layouts of ci.yml. The first is origin/main a83d841 (a Sydney pass, a separate secret-scan job); the second is the
# layout after issues 339 and 340 (one UTC pass, a quick-checks job). The first one found exactly once is used.
S_OLD = '          VATPAC_TEST_STRICT: "1"\n          VATPAC_TEST_EXPECT_TZ: Australia/Sydney\n          VATPAC_TEST_DSN: postgresql://postgres:ci-superuser-pw@localhost:5432/vatpac_test?options=-c%20TimeZone%3DAustralia/Sydney\n'
S_NEW = '          VATPAC_TEST_STRICT: "1"\n          VATPAC_TEST_EXPECT_TZ: UTC\n          VATPAC_TEST_DSN: postgresql://postgres:ci-superuser-pw@localhost:5432/vatpac_test?options=-c%20TimeZone%3DUTC\n'
NOSTRICT = '          VATPAC_TEST_STRICT: "1"\n'
py("W04", "workflow", CI, [(S_OLD, S_OLD.replace(NOSTRICT, "")), (S_NEW, S_NEW.replace(NOSTRICT, ""))], None, ["UNIT"])
py("W05", "workflow", CI, [("        with:\n          fetch-depth: 0\n      - uses: gitleaks", "      - uses: gitleaks"),
                           ("        with:\n          fetch-depth: 0\n      - name: secret-scan", "      - name: secret-scan")], None, ["UNIT"])
py("W06", "workflow", CI, "if: ${{ always() }}", "if: ${{ success() }}", ["UNIT"])
py("W07", "workflow", "docker-compose.yml", "vatpac-stats-collector:${VATPAC_IMAGE_TAG:-latest}\n    restart: unless-stopped", 'vatpac-stats-collector:${VATPAC_IMAGE_TAG:-latest}\n    restart: "no"', ["UNIT"])
py("W16", "workflow", "docker-compose.yml", "vatpac-stats-watchdog:${VATPAC_IMAGE_TAG:-latest}\n    restart: unless-stopped", 'vatpac-stats-watchdog:${VATPAC_IMAGE_TAG:-latest}\n    restart: "no"', ["UNIT"])
py("W08", "scope filter", CS, "        return DOCS\n    return FULL\n", "        return DOCS\n    return DOCS\n", ["UNIT"])
py("W09", "scope filter", CS, "    if not paths:\n        return FULL", "    if not paths:\n        return DOCS", ["UNIT"])
py("W10", "scope filter", CS, 'if p.startswith((".github/", "ops/")) or', 'if p.startswith((".github/",)) or', ["UNIT"])
py("W11", "scope filter", CS, "return max((classify_path(p) for p in paths)", "return min((classify_path(p) for p in paths)", ["UNIT"])
py("W12", "scope filter", CS, "    except ValueError:\n        return FULL", "    except ValueError:\n        return DOCS", ["UNIT"])
py("W13", "backup", "ops/backup/backup.sh", 'MIN_SIZE_PERCENT="${BACKUP_MIN_SIZE_PERCENT:-50}"', 'MIN_SIZE_PERCENT="${BACKUP_MIN_SIZE_PERCENT:-0}"', ["BAK"])
py("W14", "backup", "ops/backup/backup.sh", '[ "$size" -ge "$min" ] ||', '[ "$size" -gt "$min" ] ||', ["BAK"])
py("W15", "deploy", "ops/deploy.sh", '  if [ "$lh" != "$full" ]; then\n    echo "!!!! STALE CHECKOUT: this checkout is at ${lh:0:10} but the commit being marked', '  if false; then\n    echo "!!!! STALE CHECKOUT: this checkout is at ${lh:0:10} but the commit being marked', ["DEPF", "DEP"])
