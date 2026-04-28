"""National Diaper Bank Network — member directory.

Source: https://nationaldiaperbanknetwork.org/member-directory/

The directory is a single HTML page: each US state is an <h4>, and members
are <li>City – <a href="URL">Name</a></li> entries beneath. No street
addresses on this index page (some member sites have them, but we don't
crawl those — too unstructured). For lat/lng we forward-geocode the
"City, State" pair.

Subtype: diaper_bank under `care`.
Population: families (specifically families with infants/toddlers).

Run:
  python -m scrapers.sources.community.national_diaper_bank_network --print
  python -m scrapers.sources.community.national_diaper_bank_network --write
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import date
from html.parser import HTMLParser
from typing import Iterator, Optional

from scrapers.geocode import Geocoder, geocode_entry
from scrapers.sources._http import (
    get_with_retry, normalize_website, session,
)

log = logging.getLogger(__name__)

DIRECTORY_URL = "https://nationaldiaperbanknetwork.org/member-directory/"

US_STATE_NAMES = {
    "Alabama","Alaska","Arizona","Arkansas","California","Colorado","Connecticut",
    "Delaware","District of Columbia","Florida","Georgia","Hawaii","Idaho",
    "Illinois","Indiana","Iowa","Kansas","Kentucky","Louisiana","Maine",
    "Maryland","Massachusetts","Michigan","Minnesota","Mississippi","Missouri",
    "Montana","Nebraska","Nevada","New Hampshire","New Jersey","New Mexico",
    "New York","North Carolina","North Dakota","Ohio","Oklahoma","Oregon",
    "Pennsylvania","Rhode Island","South Carolina","South Dakota","Tennessee",
    "Texas","Utah","Vermont","Virginia","Washington","West Virginia",
    "Wisconsin","Wyoming","Puerto Rico",
}

STATE_NAME_TO_CODE = {
    "Alabama":"AL","Alaska":"AK","Arizona":"AZ","Arkansas":"AR","California":"CA",
    "Colorado":"CO","Connecticut":"CT","Delaware":"DE","District of Columbia":"DC",
    "Florida":"FL","Georgia":"GA","Hawaii":"HI","Idaho":"ID","Illinois":"IL",
    "Indiana":"IN","Iowa":"IA","Kansas":"KS","Kentucky":"KY","Louisiana":"LA",
    "Maine":"ME","Maryland":"MD","Massachusetts":"MA","Michigan":"MI",
    "Minnesota":"MN","Mississippi":"MS","Missouri":"MO","Montana":"MT",
    "Nebraska":"NE","Nevada":"NV","New Hampshire":"NH","New Jersey":"NJ",
    "New Mexico":"NM","New York":"NY","North Carolina":"NC","North Dakota":"ND",
    "Ohio":"OH","Oklahoma":"OK","Oregon":"OR","Pennsylvania":"PA",
    "Rhode Island":"RI","South Carolina":"SC","South Dakota":"SD",
    "Tennessee":"TN","Texas":"TX","Utah":"UT","Vermont":"VT","Virginia":"VA",
    "Washington":"WA","West Virginia":"WV","Wisconsin":"WI","Wyoming":"WY",
    "Puerto Rico":"PR",
}

# Patterns for the city–name separator. NDBN's HTML uses several variations
# of dashes (–, &#8211;, -, etc.) and surrounding whitespace.
_DASH_SPLIT_RE = re.compile(r"\s*[–\-—]\s*")


class _DirectoryParser(HTMLParser):
    """Streaming parser for the NDBN directory page.

    Tracks the most-recent <h4> as the current state. Within each <li>,
    captures the leading text (city) before any <a>, the <a href> URL,
    and the <a> text (member name).
    """
    def __init__(self):
        super().__init__()
        self.current_state: Optional[str] = None
        self.in_h4 = False
        self.h4_text: list[str] = []
        self.in_li = False
        self.li_text_parts: list[str] = []
        self.li_link_href: Optional[str] = None
        self.li_link_text: list[str] = []
        self.in_li_link = False
        self.entries: list[dict] = []

    def handle_starttag(self, tag, attrs):
        if tag == "h4":
            self.in_h4 = True
            self.h4_text = []
        elif tag == "li":
            # Reset li state. Some pages have nested <ul><li>, which is fine —
            # we just want the leaf entries with text/link content.
            self.in_li = True
            self.li_text_parts = []
            self.li_link_href = None
            self.li_link_text = []
        elif tag == "a" and self.in_li:
            href = dict(attrs).get("href") or ""
            self.li_link_href = href
            self.in_li_link = True

    def handle_endtag(self, tag):
        if tag == "h4" and self.in_h4:
            full = "".join(self.h4_text).strip()
            if full in US_STATE_NAMES:
                self.current_state = full
            else:
                self.current_state = None
            self.in_h4 = False
        elif tag == "a" and self.in_li_link:
            self.in_li_link = False
        elif tag == "li" and self.in_li:
            self._finalize_li()
            self.in_li = False

    def handle_data(self, data):
        if self.in_h4:
            self.h4_text.append(data)
        elif self.in_li_link:
            self.li_link_text.append(data)
        elif self.in_li:
            self.li_text_parts.append(data)

    def _finalize_li(self):
        if not self.current_state:
            return
        # The <li> looks like:  "City – " <a>Name</a>
        # so li_text_parts ≈ ["City – ", " "], li_link_text = ["Name"]
        leading = "".join(self.li_text_parts).strip()
        # Drop trailing dashes and whitespace
        leading = re.sub(r"[–\-—\s]+$", "", leading).strip()
        name_from_link = "".join(self.li_link_text).strip()
        if not leading and not name_from_link:
            return

        if name_from_link:
            city = leading
            name = name_from_link
            url = self.li_link_href or ""
        else:
            # Sometimes there's no link, just text like "City – Name"
            parts = _DASH_SPLIT_RE.split(leading, maxsplit=1)
            if len(parts) == 2:
                city, name = parts[0].strip(), parts[1].strip()
            else:
                city, name = "", leading
            url = ""

        if not name:
            return
        # Some entries prefix the city with "Multiple cities" or use commas
        # for multi-city. Take the first city for geocoding.
        primary_city = re.split(r"[,&/]| and ", city, maxsplit=1)[0].strip() if city else ""

        self.entries.append({
            "state_name": self.current_state,
            "city": primary_city,
            "name": name,
            "url": url,
        })


def parse_directory(html: str) -> list[dict]:
    p = _DirectoryParser()
    p.feed(html)
    return p.entries


def fetch(state: Optional[str] = None, limit: Optional[int] = None) -> Iterator[dict]:
    """Stream KB entries for diaper bank network members."""
    sess = session()
    log.info("fetching NDBN directory")
    r = get_with_retry(DIRECTORY_URL, sess=sess, timeout=60)
    raw = parse_directory(r.text)
    log.info("parsed %d entries from directory", len(raw))

    geocoder = Geocoder()
    geocoder.load_cache()
    today = date.today().isoformat()

    yielded = 0
    skipped_state_filter = 0
    skipped_geocode = 0

    state_filter = state.upper() if state else None

    try:
        for raw_entry in raw:
            state_code = STATE_NAME_TO_CODE.get(raw_entry["state_name"])
            if not state_code:
                continue
            if state_filter and state_code != state_filter:
                skipped_state_filter += 1
                continue

            entry = {
                "name": raw_entry["name"],
                "type": "care",
                "subtype": "diaper_bank",
                "city": raw_entry["city"] or None,
                "state": state_code,
                "lat": None,
                "lng": None,
                "geocoded_by": None,
                "needs_geocode_review": False,
                "website": normalize_website(raw_entry.get("url") or "") or None,
                "source": "national_diaper_bank_network",
                "source_id": f"ndbn-{state_code.lower()}-{raw_entry['name'].lower().replace(' ', '-')[:60]}",
                "verified": True,
                "verified_by": "scraper:national_diaper_bank_network",
                "verified_at": today,
                "last_checked": today,
                "populations": ["families", "infants"],
                "services": ["diapers"],
            }

            # Forward-geocode "City, State". No street address available.
            if entry["city"]:
                geocode_entry(entry, geocoder)
            if entry.get("lat") is None or entry.get("lng") is None:
                # Without coords we can't place it on the map. Skip — a manual
                # PR can later add a real address + coords.
                skipped_geocode += 1
                continue

            yield {k: v for k, v in entry.items() if v not in (None, "", [])}
            yielded += 1
            if limit and yielded >= limit:
                break
    finally:
        geocoder.save_cache()
        log.info(
            "NDBN done: yielded=%d state_filter=%d skipped_geocode=%d",
            yielded, skipped_state_filter, skipped_geocode,
        )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="National Diaper Bank Network scraper")
    parser.add_argument("--state", help="2-letter state filter")
    parser.add_argument("--limit", type=int, help="Stop after N entries")
    parser.add_argument("--print", action="store_true")
    parser.add_argument("--write", action="store_true")
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
