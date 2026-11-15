"""ops/backfill_rating_history.py (issue 379) against a fake VATSIM API and a fake database. The real API is never called."""
from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("backfill_rating_history", REPO / "ops" / "backfill_rating_history.py")
bf = importlib.util.module_from_spec(spec)
sys.modules["backfill_rating_history"] = bf
spec.loader.exec_module(bf)


def item(rating: int, start: str, minutes: float, end: str | None = "") -> dict[str, Any]:
    s = datetime.fromisoformat(start).replace(tzinfo=UTC)
    e = (s.timestamp() + minutes * 60)
    end_iso = datetime.fromtimestamp(e, UTC).isoformat() if end == "" else end
    return {"connection_id": {"vatsim_id": 1, "rating": rating, "start": s.isoformat(), "end": end_iso}}


class FakeApi:
    """url -> pages; counts the calls. histories maps cid to its list of items."""

    def __init__(self, histories: dict[int, list[dict[str, Any]]], fail_from: int | None = None) -> None:
        self.histories, self.fail_from, self.urls = histories, fail_from, []

    def __call__(self, url: str) -> Any:
        self.urls.append(url)
        if self.fail_from is not None and len(self.urls) >= self.fail_from:
            raise bf.ApiStop("HTTP 429")
        cid = int(url.split("/members/")[1].split("/")[0])
        if cid not in self.histories:
            raise bf.ApiNotFound(url)
        off = int(url.split("offset=")[1])
        return {"items": self.histories[cid][off:off + bf.PAGE]}


class FakeDb:
    def __init__(self, pending: list[int]) -> None:
        self.pending, self.writes = pending, []

    def __call__(self, sql: str) -> str:
        if sql.startswith("SELECT cid"):
            limit = int(sql.rstrip(";\n").rsplit("LIMIT ", 1)[1])
            return "\n".join(str(c) for c in self.pending[:limit])
        self.writes.append(sql)
        return ""


def run(argv: list[str], api: FakeApi, db: FakeDb) -> int:
    return bf.main(["--sleep", "0", *argv], fetch=api, run=db, sleep=lambda s: None)


def test_twenty_minute_rule_and_earliest_per_rating() -> None:
    got = bf.earliest_per_rating([
        item(2, "2026-01-05T10:00:00", 19),        # too short
        item(2, "2026-01-09T10:00:00", 21),        # first long S1
        item(2, "2026-01-07T10:00:00", 60),        # earlier long S1 listed later in the page
        item(2, "2026-01-02T10:00:00", 20),        # exactly 20 minutes is not longer than 20
        item(3, "2026-02-01T10:00:00", 30),
        item(4, "2026-03-01T10:00:00", 30, end=None),  # still running: ignored
    ])
    assert got == {2: datetime(2026, 1, 7, 10, tzinfo=UTC), 3: datetime(2026, 2, 1, 10, tzinfo=UTC)}


def test_paging_reads_every_page_until_a_short_one() -> None:
    items = [item(2, "2026-01-01T10:00:00", 30) for _ in range(bf.PAGE)] + [item(3, "2026-02-01T10:00:00", 30)]
    api = FakeApi({7: items})
    got = bf.fetch_history(7, api, lambda s: None, 0)
    assert sorted(got) == [2, 3]
    assert [u.split("offset=")[1] for u in api.urls] == ["0", str(bf.PAGE)]
    assert all("limit=500" in u for u in api.urls)


def test_writes_rows_and_marker_in_one_script_and_prints_a_summary(capsys: pytest.CaptureFixture[str]) -> None:
    api = FakeApi({11: [item(2, "2026-01-07T10:00:00", 60), item(3, "2026-02-01T10:00:00", 30)]})
    db = FakeDb([11])
    assert run([], api, db) == 0
    assert len(db.writes) == 1
    sql = db.writes[0]
    assert "controller_rating_first_seen" in sql and "(11, 2, '2026-01-07T10:00:00+00:00')" in sql and "(11, 3," in sql
    assert sql.rstrip().endswith("DO UPDATE SET backfilled_at = now();") and sql.count("controller_rating_backfill") == 1
    assert "loaded 1 of 1 controllers" in capsys.readouterr().out


def test_limit_caps_the_controllers_and_the_sleep_is_used() -> None:
    api = FakeApi({c: [item(2, "2026-01-07T10:00:00", 60)] for c in (1, 2, 3)})
    db = FakeDb([1, 2, 3])
    slept: list[float] = []
    assert bf.main(["--sleep", "2", "--limit", "2"], fetch=api, run=db, sleep=slept.append) == 0
    assert len(api.urls) == 2 and len(db.writes) == 2 and slept == [2.0, 2.0]


def test_dry_run_writes_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    api = FakeApi({11: [item(2, "2026-01-07T10:00:00", 60)]})
    db = FakeDb([11])
    assert run(["--dry-run"], api, db) == 0
    assert db.writes == []
    out = capsys.readouterr().out
    assert "dry run" in out and "would load 1 of 1" in out


def test_429_stops_the_run_but_keeps_finished_controllers(capsys: pytest.CaptureFixture[str]) -> None:
    api = FakeApi({c: [item(2, "2026-01-07T10:00:00", 60)] for c in (1, 2, 3)}, fail_from=2)
    db = FakeDb([1, 2, 3])
    assert run([], api, db) == 3
    assert len(db.writes) == 1 and "controller_rating_first_seen" in db.writes[0] and "(1, 2," in db.writes[0]
    assert len(api.urls) == 2                                   # it did not carry on to controller 3
    assert "STOPPED early: HTTP 429" in capsys.readouterr().out


def test_resumable_skips_controllers_that_already_have_a_marker() -> None:
    api = FakeApi({1: [item(2, "2026-01-07T10:00:00", 60)], 2: [item(2, "2026-01-07T10:00:00", 60)]})
    db = FakeDb([2])                                            # 1 has a marker, so the database no longer lists it
    assert run([], api, db) == 0
    assert len(api.urls) == 1 and "/members/2/" in api.urls[0]


def test_unknown_member_gets_a_marker_with_no_ratings() -> None:
    db = FakeDb([99])
    assert run([], FakeApi({}), db) == 0
    assert len(db.writes) == 1 and "controller_rating_first_seen" not in db.writes[0] and "(99)" in db.writes[0]


def test_bad_options_are_refused() -> None:
    assert bf.main(["--limit", "0"], fetch=FakeApi({}), run=FakeDb([])) == 2
    assert bf.main(["--sleep", "-1"], fetch=FakeApi({}), run=FakeDb([])) == 2


def test_cid_option_loads_that_controller_only() -> None:
    api = FakeApi({5: [item(2, "2026-01-07T10:00:00", 60)]})
    db = FakeDb([1, 2, 3])
    assert run(["--cid", "5"], api, db) == 0
    assert len(api.urls) == 1 and "/members/5/" in api.urls[0]
