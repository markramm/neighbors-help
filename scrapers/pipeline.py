"""Pipeline orchestrator: fetch all sources → geocode → dedup → validate → write → coverage.

Per-source try/except so one source failing doesn't kill the whole run.

Usage:
  python -m scrapers.pipeline                                    # all sources, all states
  python -m scrapers.pipeline --sources hrsa_fqhc usda_snap     # subset
  python -m scrapers.pipeline --states MI OH IN                 # filter geographically
  python -m scrapers.pipeline --dry-run                         # don't write KB
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from typing import Callable, Iterable, Optional

from scrapers import kb
from scrapers.geocode import Geocoder, geocode_entry
from scrapers.normalize.dedup import deduplicate
from scrapers.sources.federal import hrsa_fqhc, usda_snap

log = logging.getLogger(__name__)


# Registry of sources: name -> fetch_fn
# fetch_fn signature: (state: Optional[str], limit: Optional[int]) -> Iterator[dict]
SOURCES: dict[str, Callable] = {
    "hrsa_fqhc":   hrsa_fqhc.fetch,
    "usda_snap":   usda_snap.fetch,
}


def run(
    sources: Optional[list[str]] = None,
    states: Optional[list[str]] = None,
    limit_per_source: Optional[int] = None,
    dry_run: bool = False,
    skip_geocode: bool = False,
) -> dict:
    sources = sources or list(SOURCES.keys())
    state_filter = [s.upper() for s in states] if states else None

    # 1. Fetch each source. State filtering happens at the source layer
    # (cheaper than fetching everything).
    all_entries: list[dict] = []
    per_source_counts: dict[str, int] = {}
    failed_sources: list[tuple[str, str]] = []

    for sname in sources:
        if sname not in SOURCES:
            log.warning("unknown source %r — skipping", sname)
            continue
        log.info("=== fetching %s ===", sname)
        t0 = time.time()
        fetch = SOURCES[sname]
        source_entries: list[dict] = []
        try:
            if state_filter:
                for st in state_filter:
                    source_entries.extend(fetch(state=st, limit=limit_per_source))
            else:
                source_entries.extend(fetch(state=None, limit=limit_per_source))
        except Exception as e:
            log.exception("source %s FAILED: %s", sname, e)
            failed_sources.append((sname, str(e)))
            continue
        per_source_counts[sname] = len(source_entries)
        all_entries.extend(source_entries)
        log.info("=== %s done: %d entries in %.1fs ===", sname, len(source_entries), time.time() - t0)

    log.info("total fetched: %d", len(all_entries))

    # 2. Geocode missing coords. Both v1 sources include coords so this is
    # mostly a no-op, but it lets later (community PR) entries get geocoded.
    if not skip_geocode:
        geocoder = Geocoder()
        geocoder.load_cache()
        try:
            ungeo = sum(1 for e in all_entries if e.get("lat") is None or e.get("lng") is None)
            if ungeo:
                log.info("geocoding %d entries...", ungeo)
                for e in all_entries:
                    geocode_entry(e, geocoder)
                log.info("geocoder stats: %s", geocoder.stats)
            else:
                log.info("all entries already have coords — skipping geocode")
        finally:
            geocoder.save_cache()

    # 3. Dedup (within-type, within-zip)
    log.info("deduplicating...")
    deduped = deduplicate(all_entries)

    # 4. Validate + write
    log.info("validating + writing...")
    report = kb.write_many(deduped, dry_run=dry_run)

    # 5. Coverage (KB-internal authoring artifact)
    if not dry_run:
        log.info("regenerating KB coverage...")
        kb.generate_coverage()

    # 6. Build site data files (per-state + index + GeoJSON)
    if not dry_run:
        log.info("building client-side data files...")
        from scrapers import build_data
        data_summary = build_data.build()
        log.info("data files: %s", data_summary)

    summary = {
        "fetched":          len(all_entries),
        "deduped":          len(deduped),
        "valid":            report.valid,
        "blocked":          report.blocked,
        "needs_review":     report.needs_review,
        "per_source":       per_source_counts,
        "failed_sources":   failed_sources,
    }
    log.info("=== pipeline complete ===")
    for k, v in summary.items():
        log.info("  %-15s %s", k + ":", v)
    return summary


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="neighbors-help scraper pipeline")
    parser.add_argument("--sources", nargs="+",
                        help="Source names to run (default: all). Available: " +
                             ", ".join(SOURCES))
    parser.add_argument("--states", nargs="+",
                        help="2-letter state codes to filter to (default: all states).")
    parser.add_argument("--limit-per-source", type=int,
                        help="Stop each source after N entries (for testing).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Don't write KB files.")
    parser.add_argument("--skip-geocode", action="store_true",
                        help="Skip geocoding step (faster when all entries have coords).")
    parser.add_argument("--quiet", action="store_true", help="Less verbose logging.")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    summary = run(
        sources=args.sources,
        states=args.states,
        limit_per_source=args.limit_per_source,
        dry_run=args.dry_run,
        skip_geocode=args.skip_geocode,
    )

    print()
    print("=" * 60)
    print(f"  Fetched:      {summary['fetched']}")
    print(f"  After dedup:  {summary['deduped']}")
    print(f"  Valid:        {summary['valid']}")
    print(f"  Blocked:      {summary['blocked']}")
    print(f"  Needs review: {summary['needs_review']}")
    if summary["per_source"]:
        print("  Per source:")
        for s, c in summary["per_source"].items():
            print(f"    {s:30s} {c}")
    if summary["failed_sources"]:
        print("  FAILED sources:")
        for s, err in summary["failed_sources"]:
            print(f"    {s}: {err}")
    print("=" * 60)
    return 0 if not summary["failed_sources"] else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
