# Runbook: add or change a dashboard

Last reviewed: 11-10-2026. Dashboards are code: JSON files in git, loaded by Grafana from a read-only mount. Edits made in the Grafana web interface are not saved (`allowUiUpdates: false`, viewers cannot edit) and are lost on restart; every change goes through the files.

## Where things are

- `grafana/dashboards/<folder>/<name>.json`: one file per dashboard. The directory name becomes the Grafana folder (`pilots`, `atc`, `events`, `demographics`, `military`, `monitoring`).
- `grafana/provisioning/dashboards/dashboards.yml`: loads every file under that directory and rescans every 30 seconds. `grafana/provisioning/datasources/postgres.yml`: the single data source `vatpac` (uid `vatpac`), connecting as the read-only role `grafana_ro`.
- `grafana/validate_dashboards.py` and `grafana/check_columns.py`: the static checks below.
- `docs/CHART_DEFINITIONS.md`: one row per chart saying what it shows, where it comes from and how to test it.

## The rules a dashboard must follow

`python grafana/validate_dashboards.py` enforces these (it fails CI):

- Valid JSON with a unique dashboard `uid` (convention `vatpac-<name>`), a title and `"timezone": "utc"`.
- A text panel containing `${go_live}` ("Data since ..."), fed by the hidden `go_live` variable (`SELECT ... FROM data_since`). There is no backfill, so every chart starts empty at go-live.
- Unique panel ids; every non-text panel uses the data source with uid `vatpac` and has a non-empty `rawSql`; give every panel a description (a warning otherwise). Say which v1 card it reproduces.
- Queries read only allow-listed relations: the views created in `db/migrations/` and the six lookup tables (`controller_rating`, `facility`, `pilot_rating`, `airline`, `aircraft_type`, `region_map`). Base tables such as `pilot_session` or `poll_log` are refused, and `grafana_ro` has no access to them anyway. The list of views is in [../DATA_DICTIONARY.md](../DATA_DICTIONARY.md).
- No hard-coded dates, no `DISTINCT ON`, and every query must parse as PostgreSQL (needs `pip install sqlglot`; without it the SQL is not parsed and the script says so).
- Use UTC explicitly, and Grafana's time macros (`$__timeFrom()`, `$__timeTo()`, `$__timeFilter(col)`) or `now()` so the chart follows the time range.

## Add or change a panel

1. **Write the query against the views** in `psql` first. On the VM: `docker compose exec db psql -U vatpac -d vatpac`. The query must also work as `grafana_ro`, which sees views only. Mind cost: Grafana's statement timeout is 30 seconds, and the panels run every 30 to 60 seconds. Avoid reading `pilot_position` (it is not exposed); use `flight`, `flight_ext` and the other views.
2. **If a view is missing**, add a new numbered migration in `db/migrations/` (file name `<timestamp>_description.sql` in the same 14-digit style, later than the latest, with `-- migrate:up` and a `-- migrate:down` stub that says it is forward-only). Never edit an applied migration. Every view in `public` is granted to `grafana_ro` the next time the `migrate` job runs `db/roles.sql`, so no grant line is needed. Document the view in [../DATA_DICTIONARY.md](../DATA_DICTIONARY.md).
3. **Edit the dashboard JSON**: copy a similar panel in the same file, give it the next unused `id` and a free `gridPos`, then set `title`, `description`, `fieldConfig` and `targets[0].rawSql` (`"format": "table"` for tables and stats, `"time_series"` for time series, with a column named `time`). Keep the JSON valid (the validator reports the file and panel on error).
4. **Update the documentation in the same commit:** the row in [../CHART_DEFINITIONS.md](../CHART_DEFINITIONS.md) (v1 card, definition, source view, panel file and title, difference from v1, status, test) and the summary counts if a card moved between KEPT, CHANGED and DROPPED; [../DASHBOARDS_SIMPLIFIED.md](../DASHBOARDS_SIMPLIFIED.md) if a chart is dropped; the tracker.
5. **Test it** (next section), commit, and deploy ([deploy-to-vm.md](deploy-to-vm.md)). A JSON-only change needs no rebuild or restart; Grafana picks it up within 30 seconds.

## Test a dashboard change

Without a database (anywhere, fast):

```sh
python grafana/validate_dashboards.py     # expect RESULT: OK (0 errors); one known warning is printed for the controller activity trends panel
python grafana/check_columns.py           # resolves every column against the view definitions; expect "bad 0"
```

Against synthetic data, on a throwaway database named `*_test` (see [run-tests.md](run-tests.md)): the integration tests load the dashboard JSON, take the panel's `rawSql`, replace `$__timeFrom()` and `$__timeTo()`, insert synthetic flights or sessions and assert on the result. To cover a new or changed panel, add a test in `tests/integration/` beside the existing ones (for example `tests/integration/test_card_90.py`, which reuses `run_panel` and `add_flight` from `tests/integration/test_pilots_t14.py`). The marker is `@pytest.mark.integration`.

Against real data, read-only, on the VM: take the panel's `rawSql`, replace `$__timeFrom()` with `(now() - interval '200 days')` and `$__timeTo()` with `(now())` (keep the brackets), wrap it as `SELECT count(*) FROM (...) x`, and pipe it to `psql` in the db container. Check the result against a manual count.

Through Grafana, after a deploy: `python3 ops/check_panels.py` (from the stack folder, on the VM) runs every panel query via Grafana's API and reports OK, EMPTY (no rows yet, fine while data is young) or ERROR. Then look at the dashboard in a browser: a query that runs is not the same as a query that answers the question correctly.

Names and CIDs are shown and are public VATSIM data. Public, read-only sharing is done with Grafana public dashboards and is described in [public-access.md](public-access.md) (ADR-029); anonymous access stays off. Public dashboards do not fill text-box variables, so a dashboard that is shared needs a fixed value in place of a text box (the Departure board is fixed to YSSY). Each shared dashboard also needs a short name in `SHORT_NAMES` in `ops/grafana_public.py` (a test requires it); see the short-address steps in [public-access.md](public-access.md).
