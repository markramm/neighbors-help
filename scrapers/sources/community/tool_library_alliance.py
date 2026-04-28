"""Tool Library Alliance — community-maintained tool library directory.

Source: Google My Maps mid=1pvBotUQAOaYIQ1GAUVwVk2JuvQP9U30
Docs:   https://www.toollibraryalliance.org/map (form-submitted, lightly curated)

Schema notes:
  - Placemarks have name + lat/lng but rarely have address text — the form
    fields are mostly empty. So we always reverse-geocode to get state / zip.
  - Worldwide map (~97 placemarks); we filter to US only.
  - Subtype: tool_library (under economy).

Run:
  python -m scrapers.sources.community.tool_library_alliance --print --limit 5
  python -m scrapers.sources.community.tool_library_alliance --write
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import date
from typing import Iterator, Optional

from scrapers.geocode import Geocoder
from scrapers.sources._kml import (
    fetch_my_maps_kml, parse_placemarks, is_us_coord,
)

log = logging.getLogger(__name__)

MAP_ID = "1pvBotUQAOaYIQ1GAUVwVk2JuvQP9U30"


def _strip_html(s: str) -> str:
    """Crude HTML-strip for KML descriptions. Just removes tags; doesn't
    decode entities — fine for the freeform notes field."""
    if not s:
        return ""
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", "", s)
    return s.strip()


def fetch(state: Optional[str] = None, limit: Optional[int] = None) -> Iterator[dict]:
    """Stream KB-shape entries for US tool libraries.

    Args:
        state: optional 2-letter state filter.
        limit: stop after N yielded entries.
    """
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

            rev = geocoder.reverse(pm.lat, pm.lng)
            if rev is None or not rev.state:
                skipped_no_state += 1
                continue
            if state and rev.state != state.upper():
                skipped_state_filter += 1
                continue

            # The form's "Library address" / "Other notes" fields are usually
            # empty, but if filled, they provide free-text address/notes.
            ext_addr = pm.extended.get("Library address", "").strip()
            ext_notes = _strip_html(pm.extended.get("Other notes", "")).strip()

            entry = {
                "name": pm.name,
                "type": "economy",
                "subtype": "tool_library",
                "address": ext_addr or None,
                "city": rev.city or None,
                "state": rev.state,
                "zip": rev.zip or None,
                "county": rev.county or None,
                "lat": round(pm.lat, 6),
                "lng": round(pm.lng, 6),
                "geocoded_by": "source",
                "geocoded_at": date.today().isoformat(),
                "geocode_confidence": "high",
                "source": "tool_library_alliance",
                "source_id": f"tla-{pm.name.lower().replace(' ', '-')[:60]}",
                "notes": ext_notes or None,
                "verified": False,    # community-maintained, may be stale
                "verified_by": "scraper:tool_library_alliance",
                "verified_at": date.today().isoformat(),
                "last_checked": date.today().isoformat(),
                "populations": ["anyone"],
            }
            yield {k: v for k, v in entry.items() if v not in (None, "", [])}
            yielded += 1
            if limit and yielded >= limit:
                break
    finally:
        geocoder.save_cache()
        log.info(
            "TLA done: yielded=%d outside_us=%d no_state=%d state_filter=%d",
            yielded, skipped_outside_us, skipped_no_state, skipped_state_filter,
        )


# Register source name with kb validator. (kb.VALID_SOURCES is the gate;
# add it there too — done in a separate edit.)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Tool Library Alliance scraper")
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
