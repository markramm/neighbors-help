"""Laundry Love — free laundry events at participating laundromats.

Source: StoreRocket public API
  https://storerocket.io/api/user/ezpBgQy4vy/locations
  (account ID found in https://laundrylove.org/find-a-location/ widget)

Schema (very clean — addr/city/state/zip + lat/lng + phone + email,
plus a "Day-Time" custom field that holds the hours of operation).

Subtype: laundry_assistance under `care` (basic-needs assistance, not
economic exchange — neighbors who can't afford laundromat costs are
the target population).

Run:
  python -m scrapers.sources.community.laundry_love --print --limit 5
  python -m scrapers.sources.community.laundry_love --write
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from typing import Iterator, Optional

from scrapers.sources._http import (
    get_with_retry, normalize_phone, normalize_website, normalize_zip, session,
)

log = logging.getLogger(__name__)

API_URL = "https://storerocket.io/api/user/ezpBgQy4vy/locations"


def _parse_float(s) -> Optional[float]:
    if s is None or s == "":
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _hours_from_fields(fields: list[dict]) -> str:
    """Pull the Day-Time custom field value if present."""
    for f in fields or []:
        if (f.get("name") or "").strip().lower() in ("day-time", "day time", "schedule", "hours"):
            v = (f.get("pivot_field_value") or "").strip()
            if v:
                return v
    return ""


def _location_to_entry(loc: dict) -> Optional[dict]:
    if not loc.get("visible"):
        return None
    name = (loc.get("name") or "").strip()
    if not name:
        return None
    if (loc.get("country") or "").upper() != "US":
        return None
    state = (loc.get("state") or "").strip().upper()
    if len(state) != 2:
        return None

    lat = _parse_float(loc.get("lat"))
    lng = _parse_float(loc.get("lng"))
    if lat is None or lng is None:
        return None

    address_parts = [loc.get("address_line_1") or "", loc.get("address_line_2") or ""]
    address = " ".join(p.strip() for p in address_parts if p and p.strip())

    hours = _hours_from_fields(loc.get("fields") or [])

    entry = {
        "name": name,
        "type": "care",
        "subtype": "laundry_assistance",
        "address": address or None,
        "city": (loc.get("city") or "").strip() or None,
        "state": state,
        "zip": normalize_zip(loc.get("postcode")) or None,
        "lat": round(lat, 6),
        "lng": round(lng, 6),
        "geocoded_by": "source",
        "geocoded_at": date.today().isoformat(),
        "geocode_confidence": "high",
        "phone": normalize_phone(loc.get("phone") or "") or None,
        "email": (loc.get("email") or "").strip() or None,
        "website": normalize_website(loc.get("url") or "") or None,
        "hours_raw": hours or None,
        "source": "laundry_love",
        "source_id": f"ll-{loc.get('id')}",
        "verified": True,    # locator-published; high confidence
        "verified_by": "scraper:laundry_love",
        "verified_at": date.today().isoformat(),
        "last_checked": date.today().isoformat(),
        "populations": ["anyone"],
        "services": ["laundry"],
    }
    return {k: v for k, v in entry.items() if v not in (None, "", [])}


def fetch(state: Optional[str] = None, limit: Optional[int] = None) -> Iterator[dict]:
    sess = session()
    log.info("fetching Laundry Love: %s", API_URL)
    r = get_with_retry(API_URL, sess=sess, timeout=60)
    data = r.json()
    if not data.get("success"):
        raise RuntimeError(f"Laundry Love API returned non-success: {data}")
    locations = (data.get("results") or {}).get("locations") or []
    log.info("got %d locations", len(locations))

    yielded = 0
    skipped_state = 0
    skipped_invalid = 0

    for loc in locations:
        entry = _location_to_entry(loc)
        if entry is None:
            skipped_invalid += 1
            continue
        if state and entry["state"] != state.upper():
            skipped_state += 1
            continue
        yield entry
        yielded += 1
        if limit and yielded >= limit:
            break

    log.info(
        "Laundry Love done: yielded=%d skipped_invalid=%d skipped_state=%d",
        yielded, skipped_invalid, skipped_state,
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Laundry Love scraper")
    parser.add_argument("--state", help="2-letter state filter")
    parser.add_argument("--limit", type=int, help="Stop after N entries")
    parser.add_argument("--print", action="store_true", help="Print entries to stdout")
    parser.add_argument("--write", action="store_true", help="Write to KB")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not (args.print or args.write):
        parser.error("specify --print or --write")

    if args.write:
        from scrapers import kb
        report = kb.write_many(fetch(state=args.state, limit=args.limit))
        print(f"\nValid: {report.valid}  Blocked: {report.blocked}  NeedsReview: {report.needs_review}")
        for name, msgs in report.blocked_examples[:10]:
            print(f"  BLOCKED {name}: {msgs}")
    else:
        import json
        for e in fetch(state=args.state, limit=args.limit):
            print(json.dumps(e, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
