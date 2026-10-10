"""Issue 82 follow-ups at poll level: a changed feed shape is a poll error, never an empty or plan-less ok snapshot."""
from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from vatpac.feed import FetchResult
from vatpac.filters import load_reference
from vatpac.main import Collector
from vatpac.settings import Settings
from vatpac.store import ApplyResult


class _Feed:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    async def fetch(self) -> FetchResult:
        return FetchResult(self.payload, 200)


def _poll(payload: dict[str, Any]) -> tuple[str, str | None, list[Any]]:
    repo = Path(__file__).resolve().parents[3]
    ref = load_reference(repo / "config" / "australian_airspace_polygon.json",
                         repo / "config" / "controller_callsigns_list.txt")
    col = Collector(Settings(), ref, MagicMock(), _Feed(payload))  # type: ignore[arg-type]
    rows: list[dict[str, Any]] = []
    applied: list[Any] = []
    col._log_poll = lambda status, _ref, **kw: rows.append({"status": status, **kw})  # type: ignore[method-assign]
    col._apply = lambda snap, _ref: applied.append(snap) or ApplyResult()  # type: ignore[method-assign]
    outcome = asyncio.run(col.poll_once())
    return outcome.status, rows[0]["error"], applied


def test_pilots_changing_shape_is_an_error_poll_even_when_controllers_are_fine(fixture_payload: dict) -> None:
    p = copy.deepcopy(fixture_payload)
    p["pilots"] = {"changed": "shape"}
    status, error, applied = _poll(p)
    assert status == "error" and "pilots is not a list" in (error or "") and applied == []


def test_flight_plans_changing_format_for_most_pilots_is_an_error_poll(fixture_payload: dict) -> None:
    p = copy.deepcopy(fixture_payload)
    for pilot in p["pilots"]:
        pilot["flight_plan"] = ["a", "list"]
    status, error, applied = _poll(p)
    assert status == "error" and "flight_plan that is not an object" in (error or "") and applied == []


def test_a_few_bad_flight_plans_are_stored_without_a_plan_and_the_poll_is_ok(fixture_payload: dict) -> None:
    p = copy.deepcopy(fixture_payload)
    p["pilots"][0]["flight_plan"] = "oops"
    status, _error, applied = _poll(p)
    assert status == "ok" and applied[0].bad_plans == 1
