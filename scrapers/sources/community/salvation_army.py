"""The Salvation Army — service centers / corps locations.

Discovery path (after probing):
  1. Sitemap at https://www.salvationarmyusa.org/sitemap.xml lists every URL.
  2. Filter to depth-3 paths matching /{state}/{city}/{corps}/ → 3,293 locations.
  3. Each location page has cookieManager.set(...) calls in inline JS that
     populate locationAddress / locationCity / locationState / locationZipcode.
     These are the canonical address fields. Pull them with regex.
  4. Geocode addresses via Census forward geocoder (already cached).

Subtype mapping:
  Salvation Army corps offer many services. We model each location as ONE
  entry under `care` / `community_support` (a meta-subtype that captures
  "various basic-needs services on-site"). The location page's sub-paths
  (/hunger/, /youth-services/, /spiritual-healing/, etc.) tell us what
  services they offer — captured in `services` list.

Notes:
  - Total ~3,293 pages → at 0.5s polite delay, ~30min runtime. Implements
    --state filter so contributors can run subsets.
  - Per-location service detection requires fetching the index page only;
    the sub-pages aren't fetched (those are service detail pages).

Run:
  python -m scrapers.sources.community.salvation_army --state MI --print --limit 5
  python -m scrapers.sources.community.salvation_army --state MI --write
  python -m scrapers.sources.community.salvation_army --write    # all states
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from datetime import date
from typing import Iterator, Optional
from xml.etree import ElementTree as ET

from scrapers.geocode import Geocoder, geocode_entry
from scrapers.sources._http import (
    get_with_retry, normalize_phone, normalize_zip, session,
)

log = logging.getLogger(__name__)

SITEMAP_URL = "https://www.salvationarmyusa.org/sitemap.xml"
SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

STATE_ABBREVS = {
    "al","ak","az","ar","ca","co","ct","de","fl","ga","hi","id","il","in","ia",
    "ks","ky","la","me","md","ma","mi","mn","ms","mo","mt","ne","nv","nh","nj",
    "nm","ny","nc","nd","oh","ok","or","pa","ri","sc","sd","tn","tx","ut","vt",
    "va","wa","wv","wi","wy","dc","pr",
}

# Convert verbose state names emitted in cookieManager calls.
STATE_NAME_TO_CODE = {
    "alabama":"AL","alaska":"AK","arizona":"AZ","arkansas":"AR","california":"CA",
    "colorado":"CO","connecticut":"CT","delaware":"DE","florida":"FL","georgia":"GA",
    "hawaii":"HI","idaho":"ID","illinois":"IL","indiana":"IN","iowa":"IA",
    "kansas":"KS","kentucky":"KY","louisiana":"LA","maine":"ME","maryland":"MD",
    "massachusetts":"MA","michigan":"MI","minnesota":"MN","mississippi":"MS",
    "missouri":"MO","montana":"MT","nebraska":"NE","nevada":"NV","new hampshire":"NH",
    "new jersey":"NJ","new mexico":"NM","new york":"NY","north carolina":"NC",
    "north dakota":"ND","ohio":"OH","oklahoma":"OK","oregon":"OR","pennsylvania":"PA",
    "rhode island":"RI","south carolina":"SC","south dakota":"SD","tennessee":"TN",
    "texas":"TX","utah":"UT","vermont":"VT","virginia":"VA","washington":"WA",
    "west virginia":"WV","wisconsin":"WI","wyoming":"WY",
    "district of columbia":"DC","puerto rico":"PR",
}

# Sub-paths under a location indicate which services are offered.
SUBPATH_TO_SERVICE = {
    "hunger":            "groceries",
    "feeding-others":    "groceries",
    "shelter":           "emergency_shelter",
    "transitional-housing": "transitional_housing",
    "youth-services":    "youth_program",
    "addiction-treatment": "harm_reduction",
    "adult-program-services": "adult_program",
    "veteran-services":  "veteran_program",
    "music-and-the-arts": "arts_program",
    "thrift-store":      "clothing_exchange",
    "spiritual-healing": "spiritual_support",
    "domestic-violence-support": "dv_support",
    "case-management":   "case_management",
    "financial-assistance": "utility_assistance",
}


def list_location_urls(state: Optional[str] = None) -> list[str]:
    """Pull the sitemap and return depth-3 location URLs (state/city/corps)."""
    log.info("fetching sitemap…")
    r = get_with_retry(SITEMAP_URL, timeout=60)
    log.info("sitemap %d bytes", len(r.content))
    locs: list[str] = []
    state_lower = state.lower() if state else None
    for url_el in ET.fromstring(r.content).findall(".//sm:loc", SITEMAP_NS):
        u = (url_el.text or "").strip()
        path = u.replace("https://www.salvationarmyusa.org", "")
        parts = [p for p in path.split("/") if p]
        if len(parts) != 3:
            continue
        if parts[0] not in STATE_ABBREVS:
            continue
        if state_lower and parts[0] != state_lower:
            continue
        locs.append(u)
    log.info("found %d location URLs%s", len(locs),
             f" (state={state})" if state else "")
    return locs


_COOKIE_SET_RE = re.compile(
    r"cookieManager\.set\(cookieKeys\.(\w+),\s*`([^`]*)`\)",
)


def parse_location_page(html: str) -> Optional[dict]:
    """Extract location fields from cookieManager.set(...) calls in inline JS.

    The page emits multiple sets — we want the first complete one that
    has all 4 address fields (locationAddress / locationCity /
    locationState / locationZipcode) populated.
    """
    fields: dict[str, str] = {}
    current: dict[str, str] = {}
    for m in _COOKIE_SET_RE.finditer(html):
        k, v = m.group(1), m.group(2)
        current[k] = v
        # When we hit a complete set with address fields, snapshot it
        if all(k in current and current[k] for k in
               ("locationAddress", "locationCity", "locationState", "locationZipcode")):
            fields = dict(current)
            break

    if not fields:
        return None

    state_raw = (fields.get("locationState") or "").strip()
    state = STATE_NAME_TO_CODE.get(state_raw.lower(),
                                   state_raw.upper() if len(state_raw) == 2 else "")
    if not state:
        return None

    return {
        "name": (fields.get("locationName") or "").strip().title() or None,
        "address": fields.get("locationAddress") or None,
        "city": (fields.get("locationCity") or "").strip().title() or None,
        "state": state,
        "zip": normalize_zip(fields.get("locationZipcode")) or None,
        "zuid": fields.get("locationZUID") or None,
    }


def detect_services(html: str, base_path: str) -> list[str]:
    """Find sub-page links under base_path (e.g. "/pa/corry/west-washington-street-corps/")
    and map known suffixes to canonical services."""
    pat = re.compile(re.escape(base_path) + r"([a-z\-]+)/")
    found = set()
    for m in pat.finditer(html):
        suffix = m.group(1)
        svc = SUBPATH_TO_SERVICE.get(suffix)
        if svc:
            found.add(svc)
    return sorted(found)


def fetch(state: Optional[str] = None, limit: Optional[int] = None,
          rate_limit_s: float = 0.4) -> Iterator[dict]:
    """Stream KB entries for Salvation Army corps locations.

    Args:
        state: 2-letter state filter (case insensitive)
        limit: cap entries (testing)
        rate_limit_s: sleep between fetches to be polite
    """
    sess = session()
    geocoder = Geocoder()
    geocoder.load_cache()

    urls = list_location_urls(state=state)
    if limit:
        urls = urls[:limit]

    yielded = 0
    skipped_parse = 0
    skipped_geocode = 0
    today = date.today().isoformat()

    try:
        for i, url in enumerate(urls):
            if i:
                time.sleep(rate_limit_s)
            try:
                r = get_with_retry(url, sess=sess, timeout=30)
            except Exception as e:
                log.warning("fetch failed for %s: %s", url, e)
                skipped_parse += 1
                continue

            parsed = parse_location_page(r.text)
            if not parsed:
                skipped_parse += 1
                continue

            base_path = url.replace("https://www.salvationarmyusa.org", "")
            services = detect_services(r.text, base_path)

            entry = {
                "name": parsed["name"] or "The Salvation Army",
                "type": "care",
                "subtype": "community_support",
                "address": parsed.get("address"),
                "city": parsed.get("city"),
                "state": parsed["state"],
                "zip": parsed.get("zip"),
                "lat": None,
                "lng": None,
                "geocoded_by": None,
                "needs_geocode_review": False,
                "website": url,
                "source": "salvation_army",
                "source_id": f"sa-{parsed['zuid']}" if parsed.get("zuid") else f"sa-{base_path.strip('/').replace('/', '-')}",
                "verified": True,
                "verified_by": "scraper:salvation_army",
                "verified_at": today,
                "last_checked": today,
                "populations": ["anyone"],
                "services": services,
            }

            # Geocode (Census, cached)
            geocode_entry(entry, geocoder)
            if entry.get("lat") is None or entry.get("lng") is None:
                skipped_geocode += 1
                # Skip entries we can't place — without coords they don't render
                # on the map. They could still be added later with manual coords.
                continue

            yield {k: v for k, v in entry.items() if v not in (None, "", [])}
            yielded += 1

            if i % 50 == 0:
                log.info("SA: progress %d/%d (yielded=%d)", i, len(urls), yielded)
    finally:
        geocoder.save_cache()
        log.info(
            "SA done: yielded=%d urls=%d skipped_parse=%d skipped_geocode=%d",
            yielded, len(urls), skipped_parse, skipped_geocode,
        )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Salvation Army scraper")
    parser.add_argument("--state", help="2-letter state filter (lowercase ok)")
    parser.add_argument("--limit", type=int, help="Stop after N entries")
    parser.add_argument("--rate", type=float, default=0.4, help="Seconds between requests")
    parser.add_argument("--print", action="store_true")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not (args.print or args.write):
        parser.error("specify --print or --write")

    if args.write:
        from scrapers import kb
        report = kb.write_many(fetch(state=args.state, limit=args.limit, rate_limit_s=args.rate))
        print(f"\nValid: {report.valid}  Blocked: {report.blocked}  NeedsReview: {report.needs_review}")
        for name, msgs in report.blocked_examples[:10]:
            print(f"  BLOCKED {name}: {msgs}")
    else:
        import json
        for e in fetch(state=args.state, limit=args.limit, rate_limit_s=args.rate):
            print(json.dumps(e, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
