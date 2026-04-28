"""Food Not Bombs — community-shared meals chapters.

Source: Google My Maps mid=1KVbOaPBP2Xh1zk59DS9nI-BjjYnrwtwD ("2025 FNB Locations")
Docs:   https://foodnotbombs.net

Schema notes:
  - Each placemark has rich ExtendedData (Phone, Email, City, State, Sharing
    Address, Cooking Address, Meeting Location, Notes, social handles).
  - Sharing Address is where they distribute food; Cooking Address is the
    kitchen. We use Sharing Address as primary "where" since that's what
    a hungry neighbor needs.
  - State field comes as either "NM" or "Maryland" — normalize both.
  - Subtype: soup_kitchen (closest schema fit; FNB chapters distribute
    free meals at scheduled times).

Run:
  python -m scrapers.sources.community.food_not_bombs --print --limit 5
  python -m scrapers.sources.community.food_not_bombs --write
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import date
from typing import Iterator, Optional

from scrapers.geocode import Geocoder
from scrapers.sources._http import normalize_phone, normalize_website
from scrapers.sources._kml import (
    fetch_my_maps_kml, parse_placemarks, is_us_coord,
)

log = logging.getLogger(__name__)

MAP_ID = "1KVbOaPBP2Xh1zk59DS9nI-BjjYnrwtwD"

# Lookup for verbose state names → 2-letter codes. The KML mixes formats.
STATE_NAME_TO_CODE = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME",
    "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH",
    "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "puerto rico": "PR",
}


def _strip_html(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"<br\s*/?>", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _normalize_state(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    if len(raw) == 2 and raw.isalpha():
        return raw.upper()
    return STATE_NAME_TO_CODE.get(raw.lower(), "")


def fetch(state: Optional[str] = None, limit: Optional[int] = None) -> Iterator[dict]:
    kml = fetch_my_maps_kml(MAP_ID)
    geocoder = Geocoder()
    geocoder.load_cache()

    yielded = 0
    skipped_outside_us = 0
    skipped_no_state = 0
    skipped_state_filter = 0

    try:
        for pm in parse_placemarks(kml):
            if not is_us_coord(pm.lat, pm.lng):
                skipped_outside_us += 1
                continue

            ext = pm.extended
            # Form-supplied state (could be code or full name)
            form_state = _normalize_state(ext.get("State", ""))
            form_city = (ext.get("City") or "").strip()

            # Reverse-geocode for ground-truth state/zip/city. Prefer reverse
            # over form data — chapters self-report inconsistently.
            rev = geocoder.reverse(pm.lat, pm.lng)
            if rev is None or not rev.state:
                # Last-resort: trust form-state if present
                if not form_state:
                    skipped_no_state += 1
                    continue
                state_code = form_state
                zip_code = ""
                city = form_city
                county = ""
            else:
                state_code = rev.state
                zip_code = rev.zip
                city = rev.city or form_city
                county = rev.county

            if state and state_code != state.upper():
                skipped_state_filter += 1
                continue

            phone = normalize_phone(ext.get("Phone Number", ""))
            email = (ext.get("Email") or "").strip()
            website = normalize_website(ext.get("Website") or "")

            sharing_addr = (ext.get("Sharing Address") or "").strip()
            cooking_addr = (ext.get("Cooking Address") or "").strip()
            meeting_loc = _strip_html(ext.get("Meeting Location", ""))
            sharing_info = _strip_html(ext.get("Sharing Info", ""))

            # Build hours_raw from sharing info if available
            hours = sharing_info or _strip_html(ext.get("Cooking Info", ""))

            notes_parts = []
            if cooking_addr:
                notes_parts.append(f"Cooking: {cooking_addr}")
            if meeting_loc:
                notes_parts.append(f"Meets: {meeting_loc}")
            general_notes = _strip_html(ext.get("Notes", ""))
            if general_notes:
                notes_parts.append(general_notes)
            notes = " · ".join(notes_parts)

            entry = {
                "name": pm.name,
                "type": "food",
                "subtype": "soup_kitchen",
                "address": sharing_addr or None,
                "city": city or None,
                "state": state_code,
                "zip": zip_code or None,
                "county": county or None,
                "lat": round(pm.lat, 6),
                "lng": round(pm.lng, 6),
                "geocoded_by": "source",
                "geocoded_at": date.today().isoformat(),
                "geocode_confidence": "high",
                "phone": phone or None,
                "email": email or None,
                "website": website or None,
                "hours_raw": hours or None,
                "notes": notes or None,
                "source": "food_not_bombs",
                "source_id": f"fnb-{pm.name.lower().replace(' ', '-')[:60]}",
                "verified": False,
                "verified_by": "scraper:food_not_bombs",
                "verified_at": date.today().isoformat(),
                "last_checked": date.today().isoformat(),
                "populations": ["anyone"],
                "services": ["hot_meals"],
            }
            yield {k: v for k, v in entry.items() if v not in (None, "", [])}
            yielded += 1
            if limit and yielded >= limit:
                break
    finally:
        geocoder.save_cache()
        log.info(
            "FNB done: yielded=%d outside_us=%d no_state=%d state_filter=%d",
            yielded, skipped_outside_us, skipped_no_state, skipped_state_filter,
        )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Food Not Bombs scraper")
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
