"""Review fixes 87 (health ages follow the poll interval), 99 (prefixes precomputed), 100 (one read per file)."""
from __future__ import annotations

import hashlib
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from vatpac.filters import ReferenceData, is_near_miss, load_reference
from vatpac.health import Stats


def _stats(poll: float | None, ok_age: float, snap_age: float) -> Stats:
    now = time.time()
    kw = {} if poll is None else {"poll_interval_s": poll}
    return Stats(last_ok_wall=now - ok_age, last_snapshot_ts=datetime.fromtimestamp(now - snap_age, UTC), **kw)


@pytest.mark.parametrize("poll", [None, 30.0, 5.0, 20.0])  # the default and every interval whose floor is 60 / 120
@pytest.mark.parametrize("ok_age,snap_age,healthy", [(59, 119, True), (61, 100, False), (30, 121, False), (0, 0, True)])
def test_health_ages_at_the_default_interval_are_60_and_120(poll, ok_age, snap_age, healthy) -> None:
    assert _stats(poll, ok_age, snap_age).healthy() is healthy


@pytest.mark.parametrize("poll,ok_age,snap_age,healthy", [
    (90.0, 170, 350, True), (90.0, 190, 100, False), (90.0, 100, 370, False),
    (300.0, 590, 1190, True), (300.0, 610, 100, False), (300.0, 100, 1210, False)])
def test_health_ages_scale_with_a_slow_poll_interval(poll, ok_age, snap_age, healthy) -> None:
    assert _stats(poll, ok_age, snap_age).healthy() is healthy


def test_the_collector_gives_stats_the_configured_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    from vatpac.main import Collector
    from vatpac.settings import Settings

    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "90")
    s = Settings()
    ref = ReferenceData.__new__(ReferenceData)
    object.__setattr__(ref, "polygon_sha", "p"), object.__setattr__(ref, "whitelist_sha", "w")
    assert Collector(s, ref, None, None).stats.poll_interval_s == 90.0  # type: ignore[arg-type]


class _NoIterate(frozenset):
    def __iter__(self):  # type: ignore[no-untyped-def]
        raise AssertionError("the whitelist must not be walked when the prefixes are given")


def test_near_miss_uses_the_given_prefixes_without_walking_the_whitelist() -> None:
    wl = _NoIterate({"ML_APP", "AD_TWR"})
    assert is_near_miss("ML_XYZ", wl, frozenset({"ML", "AD"})) is True
    assert is_near_miss("PH_TWR", wl, frozenset({"ML", "AD"})) is False
    assert is_near_miss("ML_APP", wl, frozenset({"ML"})) is False  # listed


def test_reference_prefixes_match_the_default_path(polygon_path: Path, whitelist_path: Path) -> None:
    ref = load_reference(polygon_path, whitelist_path)
    assert ref.prefixes == {c.split("_", 1)[0] for c in ref.whitelist if "_" in c}
    for cs in ("ML_FOO_CTR", "AD_XYZ_TWR", "PH_TWR", "SY_OBS", "UAE3", "ML_APP"):
        assert is_near_miss(cs, ref.whitelist, ref.prefixes) == is_near_miss(cs, ref.whitelist)


def test_load_reference_reads_each_file_once_and_hashes_what_it_parsed(
        polygon_path: Path, whitelist_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reads: list[str] = []
    real = Path.read_bytes

    def counting(self: Path) -> bytes:
        reads.append(self.name)
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", counting)
    ref = load_reference(polygon_path, whitelist_path)
    assert sorted(reads) == sorted([polygon_path.name, whitelist_path.name])
    assert ref.polygon_sha == hashlib.sha256(polygon_path.read_bytes()).hexdigest()
    assert ref.whitelist_sha == hashlib.sha256(whitelist_path.read_bytes()).hexdigest()
