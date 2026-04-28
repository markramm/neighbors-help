"""Geocoder: Census primary, Google fallback (gated on env + budget cap).

Usage:
    g = Geocoder()
    g.load_cache()
    result = g.geocode("123 Main St", "Ypsilanti", "MI", "48197")
    g.save_cache()

Or for batch use directly on entries:
    geocode_entry(entry, geocoder=g)  # mutates entry in place

Environment variables:
    GOOGLE_GEOCODING_API_KEY    if unset, Google fallback is disabled
    NH_GEOCODE_BUDGET_USD       hard cap on Google spend (default 50.0)
                                Google charges $5 per 1000 calls.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal, Optional

import requests

log = logging.getLogger(__name__)

CACHE_PATH = Path(__file__).parent / "cache.json"

CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
CENSUS_RATE_LIMIT_S = 0.1   # be polite — Census is free
CENSUS_TIMEOUT_S = 10

GOOGLE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
GOOGLE_TIMEOUT_S = 10
GOOGLE_COST_PER_CALL_USD = 0.005   # $5 / 1000

DEFAULT_BUDGET_USD = float(os.environ.get("NH_GEOCODE_BUDGET_USD", "50.0"))


Confidence = Literal["high", "medium", "low"]


class BudgetExceeded(Exception):
    pass


@dataclass
class GeocodeResult:
    lat: float
    lng: float
    source: Literal["census", "google", "cache"]
    confidence: Confidence
    matched_address: Optional[str] = None


def _normalize_address_key(address: str, city: str, state: str, zip_code: str) -> str:
    """Cache key. Lowercase + collapsed whitespace + state/zip normalized."""
    parts = [
        (address or "").lower().strip(),
        (city or "").lower().strip(),
        (state or "").upper().strip()[:2],
        (zip_code or "").split("-")[0][:5],
    ]
    return "|".join(parts)


def _build_oneline(address: str, city: str, state: str, zip_code: str) -> str:
    parts = []
    if address: parts.append(address.strip())
    if city: parts.append(city.strip())
    if state: parts.append(state.strip())
    if zip_code: parts.append(zip_code.strip())
    return ", ".join(parts)


class Geocoder:
    def __init__(
        self,
        cache_path: Path | None = None,
        budget_usd: float = DEFAULT_BUDGET_USD,
        google_api_key: Optional[str] = None,
    ):
        self.cache_path = cache_path or CACHE_PATH
        self.cache: dict[str, dict] = {}
        self.budget_usd = budget_usd
        self.google_api_key = google_api_key or os.environ.get("GOOGLE_GEOCODING_API_KEY")
        self.google_calls = 0
        self.google_spent_usd = 0.0
        self._last_census_call = 0.0
        self.stats = {
            "cache_hit": 0,
            "census_ok": 0,
            "census_miss": 0,
            "google_ok": 0,
            "google_miss": 0,
            "google_skipped_no_key": 0,
            "google_skipped_budget": 0,
            "errors": 0,
        }

    # -- cache --------------------------------------------------------------

    def load_cache(self) -> None:
        if self.cache_path.exists():
            try:
                self.cache = json.loads(self.cache_path.read_text())
            except json.JSONDecodeError:
                log.warning("cache file corrupt, starting fresh: %s", self.cache_path)
                self.cache = {}
        else:
            self.cache = {}

    def save_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        # Sort keys for stable diffs in git
        text = json.dumps(self.cache, indent=2, sort_keys=True) + "\n"
        self.cache_path.write_text(text)

    # -- main entry point --------------------------------------------------

    def geocode(
        self, address: str, city: str, state: str, zip_code: str,
    ) -> Optional[GeocodeResult]:
        key = _normalize_address_key(address, city, state, zip_code)
        if not any([address, city, zip_code]):
            return None

        # 1. Cache
        if key in self.cache:
            cached = self.cache[key]
            self.stats["cache_hit"] += 1
            if cached.get("lat") is None:
                return None
            return GeocodeResult(
                lat=cached["lat"], lng=cached["lng"],
                source="cache",
                confidence=cached.get("confidence", "low"),
                matched_address=cached.get("matched_address"),
            )

        # 2. Census
        result = self._geocode_census(address, city, state, zip_code)

        # 3. Google fallback
        if result is None or result.confidence == "low":
            google_result = self._geocode_google(address, city, state, zip_code)
            if google_result is not None:
                result = google_result

        # 4. Cache outcome (positive or negative)
        self.cache[key] = (
            {"lat": result.lat, "lng": result.lng,
             "source": result.source, "confidence": result.confidence,
             "matched_address": result.matched_address,
             "geocoded_at": date.today().isoformat()}
            if result is not None
            else {"lat": None, "lng": None,
                  "source": "miss",
                  "geocoded_at": date.today().isoformat()}
        )
        return result

    # -- providers ---------------------------------------------------------

    def _geocode_census(
        self, address: str, city: str, state: str, zip_code: str,
    ) -> Optional[GeocodeResult]:
        oneline = _build_oneline(address, city, state, zip_code)
        if not oneline:
            return None

        # Polite rate limit
        elapsed = time.monotonic() - self._last_census_call
        if elapsed < CENSUS_RATE_LIMIT_S:
            time.sleep(CENSUS_RATE_LIMIT_S - elapsed)

        try:
            r = requests.get(
                CENSUS_URL,
                params={"address": oneline, "benchmark": "Public_AR_Current", "format": "json"},
                timeout=CENSUS_TIMEOUT_S,
            )
            self._last_census_call = time.monotonic()
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            log.debug("census error for %r: %s", oneline, e)
            self.stats["errors"] += 1
            return None

        matches = (data.get("result") or {}).get("addressMatches") or []
        if not matches:
            self.stats["census_miss"] += 1
            return None

        m = matches[0]
        coords = m.get("coordinates") or {}
        lat, lng = coords.get("y"), coords.get("x")
        if lat is None or lng is None:
            self.stats["census_miss"] += 1
            return None

        # Census doesn't expose a confidence score directly. Heuristic:
        # if it returned a single match with a tiger-line type "Rooftop" or
        # similar precision we'd say high; multiple matches imply ambiguity.
        confidence: Confidence = "high" if len(matches) == 1 else "medium"
        self.stats["census_ok"] += 1
        return GeocodeResult(
            lat=float(lat), lng=float(lng),
            source="census", confidence=confidence,
            matched_address=m.get("matchedAddress"),
        )

    def _geocode_google(
        self, address: str, city: str, state: str, zip_code: str,
    ) -> Optional[GeocodeResult]:
        if not self.google_api_key:
            self.stats["google_skipped_no_key"] += 1
            return None

        if self.google_spent_usd + GOOGLE_COST_PER_CALL_USD > self.budget_usd:
            self.stats["google_skipped_budget"] += 1
            log.warning(
                "google budget exceeded (spent $%.2f / cap $%.2f) — skipping",
                self.google_spent_usd, self.budget_usd,
            )
            return None

        oneline = _build_oneline(address, city, state, zip_code)
        try:
            r = requests.get(
                GOOGLE_URL,
                params={"address": oneline, "key": self.google_api_key},
                timeout=GOOGLE_TIMEOUT_S,
            )
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            log.debug("google error for %r: %s", oneline, e)
            self.stats["errors"] += 1
            return None
        finally:
            # Count attempts toward budget regardless of success — Google
            # bills on attempt, not result.
            self.google_calls += 1
            self.google_spent_usd += GOOGLE_COST_PER_CALL_USD

        if data.get("status") != "OK" or not data.get("results"):
            self.stats["google_miss"] += 1
            return None

        result = data["results"][0]
        loc = result.get("geometry", {}).get("location", {})
        lat, lng = loc.get("lat"), loc.get("lng")
        if lat is None or lng is None:
            self.stats["google_miss"] += 1
            return None

        # Google location_type: ROOFTOP > RANGE_INTERPOLATED > GEOMETRIC_CENTER > APPROXIMATE
        loc_type = result.get("geometry", {}).get("location_type", "APPROXIMATE")
        confidence: Confidence = {
            "ROOFTOP": "high",
            "RANGE_INTERPOLATED": "high",
            "GEOMETRIC_CENTER": "medium",
            "APPROXIMATE": "low",
        }.get(loc_type, "low")

        self.stats["google_ok"] += 1
        return GeocodeResult(
            lat=float(lat), lng=float(lng),
            source="google", confidence=confidence,
            matched_address=result.get("formatted_address"),
        )


# -- entry-level convenience -------------------------------------------------

def geocode_entry(entry: dict, geocoder: Geocoder) -> dict:
    """Geocode a KB-shape entry in place. Idempotent: skips entries that
    already have lat/lng with a non-`google_fallback_pending` source.

    Mutates and returns the entry.
    """
    if entry.get("lat") is not None and entry.get("lng") is not None and entry.get("geocoded_by") not in (None, "", "google_fallback_pending"):
        return entry

    result = geocoder.geocode(
        address=entry.get("address") or "",
        city=entry.get("city") or "",
        state=entry.get("state") or "",
        zip_code=entry.get("zip") or "",
    )
    if result is None:
        entry["needs_geocode_review"] = True
        entry["geocoded_by"] = None
        return entry

    entry["lat"] = round(result.lat, 6)
    entry["lng"] = round(result.lng, 6)
    entry["geocoded_by"] = result.source
    entry["geocoded_at"] = date.today().isoformat()
    entry["geocode_confidence"] = result.confidence
    entry["needs_geocode_review"] = False
    return entry
