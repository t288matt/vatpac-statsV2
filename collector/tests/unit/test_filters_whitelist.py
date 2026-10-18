"""SPEC s4.2 / s7: load_whitelist."""
from __future__ import annotations

from pathlib import Path

import pytest

from vatpac.errors import ReferenceDataError
from vatpac.filters import load_whitelist


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
