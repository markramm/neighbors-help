"""Tests for the geocoder. Uses unittest.mock to avoid live HTTP."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

from scrapers.geocode.geocoder import (
    Geocoder, GeocodeResult, _normalize_address_key, _build_oneline,
    geocode_entry,
)


def t(label: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        raise SystemExit(1)


def test_helpers() -> None:
    print("helpers")
    k1 = _normalize_address_key("123 Main St", "Ypsilanti", "MI", "48197")
    k2 = _normalize_address_key("  123 MAIN ST ", "ypsilanti", "mi", "48197-1234")
    t("key normalizes",  k1 == k2, f"\n    {k1!r}\n    {k2!r}")
    t("oneline OK",      _build_oneline("1 X", "Y", "MI", "48197") == "1 X, Y, MI, 48197")


def _mock_census_response(matches: list[dict]) -> MagicMock:
    m = MagicMock()
    m.raise_for_status = MagicMock()
    m.json.return_value = {"result": {"addressMatches": matches}}
    return m


def _mock_google_response(status: str, results: list[dict]) -> MagicMock:
    m = MagicMock()
    m.raise_for_status = MagicMock()
    m.json.return_value = {"status": status, "results": results}
    return m


def test_census_hit() -> None:
    print("census hit")
    g = Geocoder(cache_path=Path(tempfile.mkdtemp()) / "c.json")
    g.cache = {}
    fake = _mock_census_response([
        {"coordinates": {"x": -83.61, "y": 42.24}, "matchedAddress": "123 Main St, Ypsilanti, MI"}
    ])
    with patch("scrapers.geocode.geocoder.requests.get", return_value=fake):
        r = g.geocode("123 Main St", "Ypsilanti", "MI", "48197")
    t("got result",     r is not None)
    t("lat right",      abs(r.lat - 42.24) < 1e-6)
    t("lng right",      abs(r.lng + 83.61) < 1e-6)
    t("source census",  r.source == "census")
    t("conf high",      r.confidence == "high")
    t("stat census_ok", g.stats["census_ok"] == 1)


def test_census_miss_then_google() -> None:
    print("census miss → google")
    g = Geocoder(
        cache_path=Path(tempfile.mkdtemp()) / "c.json",
        google_api_key="FAKE-KEY",
    )
    g.cache = {}
    census_miss = _mock_census_response([])
    google_hit = _mock_google_response("OK", [{
        "geometry": {"location": {"lat": 42.0, "lng": -83.0}, "location_type": "ROOFTOP"},
        "formatted_address": "Whatever, MI",
    }])
    with patch("scrapers.geocode.geocoder.requests.get", side_effect=[census_miss, google_hit]):
        r = g.geocode("PO Box 1", "Yp", "MI", "48197")
    t("google used",   r is not None and r.source == "google")
    t("conf high",     r.confidence == "high")
    t("budget ticked", g.google_calls == 1 and g.google_spent_usd == 0.005)
    t("stat census_miss", g.stats["census_miss"] == 1)
    t("stat google_ok",  g.stats["google_ok"] == 1)


def test_google_skipped_no_key() -> None:
    print("google skipped (no key)")
    g = Geocoder(cache_path=Path(tempfile.mkdtemp()) / "c.json", google_api_key="")
    g.cache = {}
    census_miss = _mock_census_response([])
    with patch("scrapers.geocode.geocoder.requests.get", return_value=census_miss):
        r = g.geocode("PO Box 1", "Yp", "MI", "48197")
    t("returned None", r is None)
    t("no google call", g.google_calls == 0)
    t("skipped reason logged", g.stats["google_skipped_no_key"] == 1)


def test_google_budget_cap() -> None:
    print("google budget cap")
    g = Geocoder(
        cache_path=Path(tempfile.mkdtemp()) / "c.json",
        google_api_key="FAKE",
        budget_usd=0.0,   # cap of zero
    )
    g.cache = {}
    census_miss = _mock_census_response([])
    with patch("scrapers.geocode.geocoder.requests.get", return_value=census_miss):
        r = g.geocode("PO Box 1", "Yp", "MI", "48197")
    t("returned None",   r is None)
    t("no google call",  g.google_calls == 0)
    t("skipped budget",  g.stats["google_skipped_budget"] == 1)


def test_cache() -> None:
    print("cache")
    tmp = Path(tempfile.mkdtemp())
    try:
        g = Geocoder(cache_path=tmp / "c.json")
        g.cache = {}
        fake = _mock_census_response([{
            "coordinates": {"x": -83.61, "y": 42.24}, "matchedAddress": "X"
        }])
        with patch("scrapers.geocode.geocoder.requests.get", return_value=fake) as mock_get:
            g.geocode("123 Main", "Y", "MI", "48197")
            t("called once", mock_get.call_count == 1)
            g.geocode("123 Main", "Y", "MI", "48197")
            t("not called second time", mock_get.call_count == 1)
        t("cache hit stat",  g.stats["cache_hit"] == 1)

        g.save_cache()
        t("cache file exists", (tmp / "c.json").exists())

        g2 = Geocoder(cache_path=tmp / "c.json")
        g2.load_cache()
        t("cache loaded",  len(g2.cache) == 1)
        with patch("scrapers.geocode.geocoder.requests.get") as mock_get:
            r = g2.geocode("123 Main", "Y", "MI", "48197")
            t("no http on reload", mock_get.call_count == 0)
            t("source = cache",   r.source == "cache")
    finally:
        shutil.rmtree(tmp)


def test_negative_cache() -> None:
    print("negative cache")
    g = Geocoder(cache_path=Path(tempfile.mkdtemp()) / "c.json", google_api_key="")
    g.cache = {}
    census_miss = _mock_census_response([])
    with patch("scrapers.geocode.geocoder.requests.get", return_value=census_miss) as mock_get:
        r1 = g.geocode("Bad", "Bad", "ZZ", "00000")
        r2 = g.geocode("Bad", "Bad", "ZZ", "00000")
        t("both None",   r1 is None and r2 is None)
        t("called once", mock_get.call_count == 1, f"called {mock_get.call_count}x")


def test_geocode_entry_skips_existing() -> None:
    print("geocode_entry skip")
    g = Geocoder(cache_path=Path(tempfile.mkdtemp()) / "c.json")
    e = {"address": "x", "city": "y", "state": "MI", "zip": "48197",
         "lat": 42.24, "lng": -83.61, "geocoded_by": "manual"}
    with patch("scrapers.geocode.geocoder.requests.get") as mock_get:
        geocode_entry(e, g)
        t("no http call", mock_get.call_count == 0)
    t("lat preserved", e["lat"] == 42.24)


def test_geocode_entry_geocodes() -> None:
    print("geocode_entry geocodes")
    g = Geocoder(cache_path=Path(tempfile.mkdtemp()) / "c.json")
    g.cache = {}
    e = {"address": "1 Main", "city": "Y", "state": "MI", "zip": "48197"}
    fake = _mock_census_response([{
        "coordinates": {"x": -83.61, "y": 42.24}, "matchedAddress": "X"
    }])
    with patch("scrapers.geocode.geocoder.requests.get", return_value=fake):
        geocode_entry(e, g)
    t("lat applied",      e["lat"] == 42.24)
    t("lng applied",      e["lng"] == -83.61)
    t("by = census",      e["geocoded_by"] == "census")
    t("conf set",         e["geocode_confidence"] == "high")
    t("flag false",       e["needs_geocode_review"] is False)


def test_geocode_entry_failure_flags() -> None:
    print("geocode_entry failure")
    g = Geocoder(cache_path=Path(tempfile.mkdtemp()) / "c.json", google_api_key="")
    g.cache = {}
    e = {"address": "PO Box 1", "city": "Y", "state": "MI", "zip": "48197"}
    census_miss = _mock_census_response([])
    with patch("scrapers.geocode.geocoder.requests.get", return_value=census_miss):
        geocode_entry(e, g)
    t("no coords",     e.get("lat") is None and e.get("lng") is None)
    t("flag set",      e["needs_geocode_review"] is True)


if __name__ == "__main__":
    test_helpers()
    test_census_hit()
    test_census_miss_then_google()
    test_google_skipped_no_key()
    test_google_budget_cap()
    test_cache()
    test_negative_cache()
    test_geocode_entry_skips_existing()
    test_geocode_entry_geocodes()
    test_geocode_entry_failure_flags()
    print("\nAll tests passed.")
