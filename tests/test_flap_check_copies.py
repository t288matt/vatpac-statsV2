"""Issue 201: the identity-flap rule has ONE definition (ops/audit/identity_flap.sql). The two rows of the same rule in
ops/audit/invariants.sql cannot include that file (the pack is one UNION query), so this fails if their text drifts."""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SHARED = (REPO / "ops" / "audit" / "identity_flap.sql").read_text(encoding="utf-8")
PACK = (REPO / "ops" / "audit" / "invariants.sql").read_text(encoding="utf-8")

NAMES = ("pilot session pairs, same cid+callsign, logon_time within 2 s (identity flap)",
         "controller session pairs, same cid+callsign, logon_time within 2 s (identity flap)")
JOIN = re.compile(r"ON a\.cid = b\.cid AND a\.callsign = b\.callsign AND a\.id < b\.id AND abs\(extract\(epoch FROM a\.logon_time - b\.logon_time\)\) < 2")


def test_both_files_name_the_two_checks_identically() -> None:
    for name in NAMES:
        assert f"'{name}'" in SHARED and f"'{name}'" in PACK, name


def test_both_files_use_the_same_pair_rule_for_both_tables() -> None:
    assert len(JOIN.findall(SHARED)) == 2 and len(JOIN.findall(PACK)) == 2
    for text in (SHARED, PACK):
        assert "FROM pilot_session a JOIN pilot_session b" in text and "FROM controller_session a JOIN controller_session b" in text
        assert text.count("min(a.first_seen)") == 2  # the earlier session of the earliest pair, in both files


def test_the_shared_file_is_a_single_read_only_statement_with_an_optional_window() -> None:
    body = re.sub(r"--[^\n]*", "", SHARED)
    assert body.count(";") == 1 and not re.search(r"(?i)\b(insert|update|delete|drop|alter|create|truncate)\b", body)
    assert "coalesce(nullif(current_setting('vatpac.flap_window_hours', true), ''), '0')" in body  # unset or empty means all history
    assert body.count("w.hours = 0 OR b.last_seen > now() - make_interval(hours => w.hours)") == 2
