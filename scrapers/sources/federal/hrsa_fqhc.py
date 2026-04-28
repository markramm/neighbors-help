"""HRSA FQHC + Look-Alike service delivery sites.

Source: https://data.hrsa.gov/DataDownload/DD_Files/Health_Center_Service_Delivery_and_LookAlike_Sites.csv

Fields (column → schema):
  Site Name                                          → name
  Site Address / City / State Abbreviation / Postal  → address / city / state / zip
  Site Telephone Number                              → phone
  Site Web Address                                   → website
  Operating Hours per Week                           → hours_raw  (hours/week, e.g. "40")
  Health Center Type Description                     → subtype: fqhc (always)
  Site Status Description                            → filter active only
  Geocoding Artifact Address Primary X Coordinate    → lng
  Geocoding Artifact Address Primary Y Coordinate    → lat
  Complete County Name                               → county
  BPHC Assigned Number                               → source_id

Filter:
  - Skip sites whose `Site Status Description` is anything other than active.
  - Skip sites missing a name or both address+coords.

Run as script:
  python -m scrapers.sources.federal.hrsa_fqhc            # fetch all (national)
  python -m scrapers.sources.federal.hrsa_fqhc --state MI # MI only
  python -m scrapers.sources.federal.hrsa_fqhc --limit 5 --print
"""
from __future__ import annotations

import argparse
import csv
import io
import logging
import sys
from datetime import date
from typing import Iterable, Iterator, Optional

from scrapers.sources._http import (
    get_with_retry, normalize_phone, normalize_website, normalize_zip,
)

log = logging.getLogger(__name__)

CSV_URL = "https://data.hrsa.gov/DataDownload/DD_Files/Health_Center_Service_Delivery_and_LookAlike_Sites.csv"

ACTIVE_STATUS_VALUES = {
    "Active",                       # most rows
    "Active - Operational",
}

# Subtype mapping by HRSA "Health Center Type Description"
# Default to "fqhc" — this dataset is exclusively FQHCs and Look-Alikes.
SUBTYPE_MAP = {
    "Health Center Program Awardee": "fqhc",
    "Health Center Program Look-Alike": "fqhc",
}


def _parse_float(s: str) -> Optional[float]:
    if s is None:
        return None
    s = str(s).strip()
    if not s or s.lower() in ("none", "n/a", "na"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _row_to_entry(row: dict) -> Optional[dict]:
    name = (row.get("Site Name") or "").strip()
    if not name:
        return None

    status = (row.get("Site Status Description") or "").strip()
    # HRSA sometimes prefixes status with " - " variants. Be lenient.
    if status and status not in ACTIVE_STATUS_VALUES and not status.startswith("Active"):
        return None

    state = (row.get("Site State Abbreviation") or "").strip().upper()
    if len(state) != 2:
        return None

    zip5 = normalize_zip(row.get("Site Postal Code"))

    lat = _parse_float(row.get("Geocoding Artifact Address Primary Y Coordinate"))
    lng = _parse_float(row.get("Geocoding Artifact Address Primary X Coordinate"))

    address = (row.get("Site Address") or "").strip()
    city = (row.get("Site City") or "").strip()

    # Skip if we have neither geocoded coords nor an address — nothing to map
    if (lat is None or lng is None) and not address:
        return None

    hctype = (row.get("Health Center Type Description") or "").strip()
    subtype = SUBTYPE_MAP.get(hctype, "fqhc")

    hours_per_week = (row.get("Operating Hours per Week") or "").strip()
    hours_raw = f"{hours_per_week} hours/week" if hours_per_week else ""

    bphc_num = (row.get("BPHC Assigned Number") or "").strip()
    site_loc_id = (row.get("Health Center Location Identification Number") or "").strip()
    source_id = f"hrsa-{bphc_num}-{site_loc_id}" if bphc_num and site_loc_id else None

    entry = {
        "name": name,
        "type": "medical",
        "subtype": subtype,
        "address": address or None,
        "city": city or None,
        "state": state,
        "zip": zip5 or None,
        "county": (row.get("Complete County Name") or "").strip() or None,
        "lat": round(lat, 6) if lat is not None else None,
        "lng": round(lng, 6) if lng is not None else None,
        "geocoded_by": "source" if (lat is not None and lng is not None) else None,
        "geocoded_at": date.today().isoformat() if (lat is not None and lng is not None) else None,
        "geocode_confidence": "high" if (lat is not None and lng is not None) else None,
        "phone": normalize_phone(row.get("Site Telephone Number") or "") or None,
        "website": normalize_website(row.get("Site Web Address") or "") or None,
        "hours_raw": hours_raw or None,
        "source": "hrsa_fqhc",
        "source_id": source_id,
        "verified": True,
        "verified_by": "scraper:hrsa_fqhc",
        "verified_at": date.today().isoformat(),
        "last_checked": date.today().isoformat(),
        "populations": ["anyone"],
    }
    # Drop Nones for cleanliness (writer handles missing fields fine).
    return {k: v for k, v in entry.items() if v not in (None, "", [])}


def fetch(state: Optional[str] = None, limit: Optional[int] = None) -> Iterator[dict]:
    """Stream HRSA CSV rows, yield validated KB entries.

    Args:
        state: 2-letter state code, or None for all states.
        limit: stop after N entries yielded (for testing).
    """
    log.info("fetching HRSA CSV: %s", CSV_URL)
    r = get_with_retry(CSV_URL, timeout=180)
    text = r.content.decode("utf-8-sig", errors="replace")
    log.info("downloaded %d bytes", len(r.content))

    reader = csv.DictReader(io.StringIO(text))
    yielded = 0
    skipped_status = 0
    skipped_state = 0
    skipped_other = 0

    for row in reader:
        if state and (row.get("Site State Abbreviation") or "").strip().upper() != state.upper():
            skipped_state += 1
            continue
        entry = _row_to_entry(row)
        if entry is None:
            # Distinguish status filter vs other reasons
            status = (row.get("Site Status Description") or "").strip()
            if status and not (status in ACTIVE_STATUS_VALUES or status.startswith("Active")):
                skipped_status += 1
            else:
                skipped_other += 1
            continue
        yield entry
        yielded += 1
        if limit and yielded >= limit:
            log.info("hit limit %d", limit)
            break

    log.info(
        "HRSA done: yielded=%d skipped_state=%d skipped_status=%d skipped_other=%d",
        yielded, skipped_state, skipped_status, skipped_other,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="HRSA FQHC scraper")
    parser.add_argument("--state", help="2-letter state code (e.g. MI). Default: all.")
    parser.add_argument("--limit", type=int, help="Stop after N entries.")
    parser.add_argument("--print", action="store_true",
                        help="Print entries to stdout instead of writing KB files.")
    parser.add_argument("--write", action="store_true",
                        help="Write entries to kb/resources/.")
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
