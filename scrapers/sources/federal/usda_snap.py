"""USDA SNAP-authorized retailers — Farmers and Markets only.

Source: ArcGIS FeatureServer
  https://services1.arcgis.com/RLQu0rK7h4kbsBq5/arcgis/rest/services/snap_retailer_location_data/FeatureServer/0

We scrape ONLY `Store_Type = 'Farmers and Markets'` (~7,200 records nationally).
Other Store_Type values are commercial retailers (groceries, convenience stores,
dollar stores) — not what a neighborhood directory is for. The spec's original
"Non-Profit / Co-op" filter doesn't exist in this dataset.

Fields:
  Store_Name             → name
  Store_Street_Address   → address
  City / State / Zip_Code → city / state / zip
  County                 → county (uppercase from source — title-case it)
  Latitude / Longitude   → lat / lng (already in WGS84)
  Record_ID              → source_id

Notes:
  - No phone, website, or hours in this dataset
  - Pagination: max 1000 records/query, use resultOffset
  - Subtype: "farmers_market" (food)

Run as script:
  python -m scrapers.sources.federal.usda_snap --state MI --print --limit 5
  python -m scrapers.sources.federal.usda_snap --write
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from typing import Iterator, Optional

from scrapers.sources._http import (
    get_with_retry, normalize_zip, session,
)

log = logging.getLogger(__name__)

LAYER_URL = (
    "https://services1.arcgis.com/RLQu0rK7h4kbsBq5/arcgis/rest/services/"
    "snap_retailer_location_data/FeatureServer/0/query"
)
PAGE_SIZE = 1000
STORE_TYPE_FILTER = "Farmers and Markets"


def _attrs_to_entry(a: dict) -> Optional[dict]:
    name = (a.get("Store_Name") or "").strip()
    if not name:
        return None
    state = (a.get("State") or "").strip().upper()
    if len(state) != 2:
        return None

    lat = a.get("Latitude")
    lng = a.get("Longitude")
    if lat is None or lng is None:
        return None
    try:
        lat = float(lat); lng = float(lng)
    except (TypeError, ValueError):
        return None

    address_parts = [a.get("Store_Street_Address") or "", a.get("Additonal_Address") or ""]
    address = " ".join(p.strip() for p in address_parts if p and p.strip())

    county = (a.get("County") or "").strip()
    if county:
        # Source data is uppercase — make it title case + add "County" if not present
        county = county.title()
        if not county.endswith(" County"):
            county = f"{county} County"

    record_id = a.get("Record_ID")

    entry = {
        "name": name,
        "type": "food",
        "subtype": "farmers_market",
        "address": address or None,
        "city": (a.get("City") or "").strip().title() or None,
        "state": state,
        "zip": normalize_zip(a.get("Zip_Code")) or None,
        "county": county or None,
        "lat": round(lat, 6),
        "lng": round(lng, 6),
        "geocoded_by": "source",
        "geocoded_at": date.today().isoformat(),
        "geocode_confidence": "high",
        "source": "usda_snap_retailers",
        "source_id": f"snap-{record_id}" if record_id else None,
        "verified": True,
        "verified_by": "scraper:usda_snap",
        "verified_at": date.today().isoformat(),
        "last_checked": date.today().isoformat(),
        "populations": ["anyone"],
        "services": ["snap_enrollment"],  # accepts EBT
        # Note: Incentive_Program is sometimes set ("Double Up Food Bucks" etc.)
        # Capture as part of services or a note.
    }
    incentive = (a.get("Incentive_Program") or "").strip()
    if incentive:
        entry["notes"] = f"Incentive program: {incentive}"

    return {k: v for k, v in entry.items() if v not in (None, "", [])}


def fetch(state: Optional[str] = None, limit: Optional[int] = None) -> Iterator[dict]:
    """Stream SNAP Farmers and Markets entries.

    Args:
        state: 2-letter state code, or None for national.
        limit: stop after N entries.
    """
    sess = session()
    where = f"Store_Type='{STORE_TYPE_FILTER}'"
    if state:
        where += f" AND State='{state.upper()}'"

    # Get total count first
    count_resp = get_with_retry(
        LAYER_URL,
        params={"where": where, "returnCountOnly": "true", "f": "json"},
        sess=sess,
    )
    total = int(count_resp.json().get("count", 0))
    log.info("SNAP query %r: total %d records", where, total)

    yielded = 0
    offset = 0
    while offset < total:
        if limit and yielded >= limit:
            break
        page_size = PAGE_SIZE
        if limit:
            page_size = min(page_size, limit - yielded)

        r = get_with_retry(
            LAYER_URL,
            params={
                "where": where,
                "outFields": "*",
                "returnGeometry": "false",
                "resultOffset": offset,
                "resultRecordCount": page_size,
                "orderByFields": "Record_ID",
                "f": "json",
            },
            sess=sess,
        )
        features = r.json().get("features", [])
        if not features:
            log.info("no more features at offset %d", offset)
            break

        for feat in features:
            entry = _attrs_to_entry(feat.get("attributes", {}))
            if entry is None:
                continue
            yield entry
            yielded += 1
            if limit and yielded >= limit:
                break

        offset += len(features)
        log.info("SNAP fetched %d / %d", offset, total)

    log.info("SNAP done: yielded=%d", yielded)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="USDA SNAP Farmers and Markets scraper")
    parser.add_argument("--state", help="2-letter state code. Default: all.")
    parser.add_argument("--limit", type=int, help="Stop after N entries.")
    parser.add_argument("--print", action="store_true",
                        help="Print entries to stdout instead of writing KB.")
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
