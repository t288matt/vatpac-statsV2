"""SPEC s4.2 / s7: load_whitelist and is_unlisted_callsign."""
from __future__ import annotations

from pathlib import Path

import pytest

from vatpac.errors import ReferenceDataError
from vatpac.filters import is_unlisted_callsign, load_whitelist


def write(tmp_path: Path, text: str, name: str = "wl.txt") -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


# ------------------------------------------------------------------ load_whitelist
def test_both_line_formats(tmp_path: Path) -> None:
    wl = load_whitelist(write(tmp_path, "AD_APP, 118.200\nML_TWR,120.500\nBN_GND\n"))
    assert wl == frozenset({"AD_APP", "ML_TWR", "BN_GND"})
    assert isinstance(wl, frozenset)


def test_comments_and_blank_lines_skipped(tmp_path: Path) -> None:
    text = "# header comment\n\nAD_APP, 118.200\n   \n# AD_TWR, 120.500\n\tML_TWR\n"
    assert load_whitelist(write(tmp_path, text)) == frozenset({"AD_APP", "ML_TWR"})


def test_whitespace_around_callsign_is_trimmed(tmp_path: Path) -> None:
    assert load_whitelist(write(tmp_path, "  AD_APP  ,  118.200  \n")) == frozenset({"AD_APP"})


def test_crlf_line_endings(tmp_path: Path) -> None:
    p = tmp_path / "crlf.txt"
    p.write_bytes(b"AD_APP, 118.200\r\nML_TWR\r\n")
    assert load_whitelist(p) == frozenset({"AD_APP", "ML_TWR"})


def test_duplicates_collapse(tmp_path: Path) -> None:
    wl = load_whitelist(write(tmp_path, "AD_APP, 118.200\nAD_APP, 118.200\nAD_APP\n"))
    assert wl == frozenset({"AD_APP"})


def test_same_callsign_different_frequency_collapses(tmp_path: Path) -> None:
    assert load_whitelist(write(tmp_path, "AD_APP, 118.200\nAD_APP, 119.000\n")) == frozenset({"AD_APP"})


def test_case_sensitive(tmp_path: Path) -> None:
    wl = load_whitelist(write(tmp_path, "AD_APP\nad_twr\n"))
    assert "AD_APP" in wl and "ad_app" not in wl
    assert "ad_twr" in wl and "AD_TWR" not in wl  # stored exactly as written


def test_accepts_str_path(tmp_path: Path) -> None:
    assert load_whitelist(str(write(tmp_path, "AD_APP\n"))) == frozenset({"AD_APP"})


def test_missing_file_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ReferenceDataError):
        load_whitelist(tmp_path / "missing.txt")


@pytest.mark.parametrize(
    "text",
    ["", "\n\n\n", "   \n\t\n", "# only a comment\n# another\n"],
    ids=["zero-bytes", "newlines", "whitespace", "comments-only"],
)
def test_empty_yields_error(tmp_path: Path, text: str) -> None:
    with pytest.raises(ReferenceDataError):
        load_whitelist(write(tmp_path, text))


def test_unreadable_binary_content_is_error(tmp_path: Path) -> None:
    p = tmp_path / "bin.txt"
    p.write_bytes(b"\xff\xfe\x00\x81\x8d\x90 garbage")
    with pytest.raises(ReferenceDataError):
        load_whitelist(p)


def test_directory_instead_of_file_is_error(tmp_path: Path) -> None:
    with pytest.raises(ReferenceDataError):
        load_whitelist(tmp_path)


@pytest.mark.parametrize("line", [", 118.200", " ,"], ids=["empty-callsign", "blank-callsign"])
def test_file_with_only_malformed_lines_fails_closed(tmp_path: Path, line: str) -> None:
    """SPEC s7: zero usable callsigns -> ReferenceDataError."""
    with pytest.raises(ReferenceDataError):
        load_whitelist(write(tmp_path, f"{line}\n"))


@pytest.mark.parametrize("line", [", 118.200", " ,"], ids=["empty-callsign", "blank-callsign"])
def test_malformed_line_never_becomes_a_callsign(tmp_path: Path, line: str) -> None:
    """SPEC s4 says the file is validated; s7 lists only missing/unreadable/empty as raising, so a
    bad line among good ones may be skipped or rejected, but must never yield an empty callsign."""
    p = write(tmp_path, f"AD_APP, 118.200\n{line}\n")
    try:
        wl = load_whitelist(p)
    except ReferenceDataError:
        return
    assert wl == frozenset({"AD_APP"})


def test_real_whitelist_loads(whitelist_path: Path) -> None:
    wl = load_whitelist(whitelist_path)
    assert len(wl) >= 200  # file has 254 data lines; duplicates would only reduce this slightly
    for cs in ("PH_TWR", "SY_TWR", "ML_APP", "BN_GND", "AD_APP"):
        assert cs in wl
    assert not any(cs.endswith("_OBS") for cs in wl)
    assert not any("ATIS" in cs for cs in wl)
    assert "ML_XYZ_CTR" not in wl
    assert all(cs == cs.strip() and cs and "," not in cs and " " not in cs for cs in wl)


# ------------------------------------------------------------------ is_unlisted_callsign
WL = frozenset({"ML_TWR", "ML-ASP_CTR", "AD_APP", "SY_TWR"})


@pytest.mark.parametrize("cs", ["ML_XYZ_CTR", "AD_XYZ_TWR", "SY_FOO", "ML-ASP_XYZ"])
def test_unlisted_callsign_true(cs: str) -> None:
    assert is_unlisted_callsign(cs, WL) is True


@pytest.mark.parametrize("cs", ["ML_TWR", "ML-ASP_CTR", "AD_APP"])
def test_listed_is_not_unlisted_callsign(cs: str) -> None:
    assert is_unlisted_callsign(cs, WL) is False


@pytest.mark.parametrize("cs", ["ML_OBS", "AD_OBS", "ML_XYZ_OBS", "ZZ_OBS"])
def test_obs_never_unlisted_callsign(cs: str) -> None:
    assert is_unlisted_callsign(cs, WL) is False


@pytest.mark.parametrize("cs", ["ML_SUP", "AD_SUP", "ML_XYZ_SUP", "AS_SUP", "EN_SUP"])
def test_sup_never_unlisted_callsign(cs: str) -> None:
    """Issue 39 (audit F8): supervisors are not new positions, so they must not raise an unknown-callsign alert."""
    assert is_unlisted_callsign(cs, WL) is False


@pytest.mark.parametrize("cs", ["ZZ_XYZ_CTR", "YSSY_TWR", "ML-FOO_CTR", "UAE123", "", "NZ_TWR"])
def test_unrelated_prefix_is_not_unlisted_callsign(cs: str) -> None:
    assert is_unlisted_callsign(cs, WL) is False


@pytest.mark.parametrize("cs", ["ml_xyz_ctr", "Ml_XYZ_CTR"])
def test_unlisted_callsign_is_case_sensitive(cs: str) -> None:
    assert is_unlisted_callsign(cs, WL) is False


def test_prefix_must_equal_not_startwith() -> None:
    """MLX_FOO starts with ML but its prefix MLX is not a whitelist prefix."""
    assert is_unlisted_callsign("MLX_FOO", WL) is False
    assert is_unlisted_callsign("M_FOO", WL) is False


def test_unlisted_callsign_with_real_whitelist(whitelist_path: Path) -> None:
    wl = load_whitelist(whitelist_path)
    assert is_unlisted_callsign("ML_XYZ_CTR", wl) is True
    assert is_unlisted_callsign("AD_XYZ_TWR", wl) is True
    assert is_unlisted_callsign("PH_TWR", wl) is False
    assert is_unlisted_callsign("SY_OBS", wl) is False
    assert is_unlisted_callsign("UAE3", wl) is False
