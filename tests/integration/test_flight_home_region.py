"""Issue #208: flight.touches_home_region asks icao_is_home(), the one home-region rule, instead of keeping its own prefix list.

The oracle is flight_ext (dep_is_home OR arr_is_home), which has always used icao_is_home(), and a plain Python statement of the
rule. Two cases tell the old inline copy from the function: a setting that exists but holds no usable prefix (the copy fell back to
'Y', the function has no prefix), and an airport code stored in a spelling the writer never produces (the copy compared raw text).
"""
from __future__ import annotations

from typing import Any

import pytest
from helpers import CID, add_flight

pytestmark = pytest.mark.integration

BASE = CID + 30000
PAIRS = [("YMML", "KJFK"), ("KJFK", "YSSY"), ("EGLL", "KJFK"), ("NZAA", "EGLL"), ("YMML", "YSSY"), ("VHHH", "ZZZZ")]


def set_prefixes(conn: Any, value: str) -> None:
    conn.execute("INSERT INTO app_setting (key, value) VALUES ('home_icao_prefixes', %s) "
                 "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (value,))


def touches(conn: Any) -> dict[int, bool]:
    return {r["session_id"]: r["touches_home_region"] for r in conn.execute("SELECT session_id, touches_home_region FROM flight").fetchall()}


@pytest.mark.parametrize("setting", ["Y", "Y,NZ", "YM", "\tym\r\n, nz ,, v", "YMML", "Y,"])
def test_touches_home_region_agrees_with_flight_ext_and_the_rule(conn: Any, setting: str) -> None:
    set_prefixes(conn, setting)
    prefixes = [p.strip(" \t\r\n").upper() for p in setting.split(",") if p.strip(" \t\r\n")]
    sids = {add_flight(conn, BASE + i, dep=d, arr=a): (d, a) for i, (d, a) in enumerate(PAIRS)}
    got = touches(conn)
    ext = {r["session_id"]: r["dep_is_home"] or r["arr_is_home"] for r in conn.execute("SELECT * FROM flight_ext").fetchall()}
    for sid, (d, a) in sids.items():
        assert got[sid] == ext[sid] == any(x.startswith(p) for x in (d, a) for p in prefixes), (setting, d, a)


@pytest.mark.parametrize("setting", ["", "   ", " , ,"])
def test_a_setting_with_no_usable_prefix_matches_nothing_in_every_view(conn: Any, setting: str) -> None:
    """Fails if the view is reverted: the old inline copy fell back to 'Y' here, so flight said True and flight_ext said False."""
    set_prefixes(conn, setting)
    sid = add_flight(conn, BASE + 100, dep="YMML", arr="YSSY")
    assert touches(conn)[sid] is False
    ext = conn.execute("SELECT dep_is_home, arr_is_home FROM flight_ext WHERE session_id = %s", (sid,)).fetchone()
    assert (ext["dep_is_home"], ext["arr_is_home"]) == (False, False)


def test_a_missing_setting_falls_back_to_y_in_both_views(conn: Any) -> None:
    conn.execute("DELETE FROM app_setting WHERE key = 'home_icao_prefixes'")
    sid_home = add_flight(conn, BASE + 110, dep="YMML", arr="KJFK")
    sid_other = add_flight(conn, BASE + 111, dep="EGLL", arr="KJFK")
    assert touches(conn) == {sid_home: True, sid_other: False}


@pytest.mark.parametrize("dep", ["ymml", " YMML ", "\tymml\r\n"])
def test_a_code_stored_in_another_spelling_is_still_home(conn: Any, dep: str) -> None:
    """Fails if the view is reverted: the old inline copy compared the raw text (the writer stores the normalised code, so this is
    defence for rows written some other way)."""
    sid = add_flight(conn, BASE + 120, dep="YMML", arr="EGLL")
    conn.execute("UPDATE flight_summary SET departure = %s WHERE session_id = %s", (dep, sid))
    assert touches(conn)[sid] is True


def test_the_flight_view_holds_no_prefix_list_of_its_own(conn: Any) -> None:
    text = conn.execute("SELECT pg_get_viewdef('flight'::regclass) AS v").fetchone()["v"]
    assert "icao_is_home" in text and "app_setting" not in text and "string_to_array" not in text
