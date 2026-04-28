"""Build all client-side data files from the KB.

Outputs (all written to site/static/data/):

  index.json          — small (~10KB), loaded on every page hit. State list
                        with name, center, totals per petal.
  states/{XX}.json    — per-state, lazy-loaded. Includes by-zip groups with
                        full org details.
  zip-prefix.json     — copy of kb/geo/zip-prefix.json for client lookup.
  state-centers.json  — copy of kb/geo/state-centers.json (for the daisy
                        markers — keeps name + center pinned).
  coverage.json       — per-zip {present, missing} for the gap overlay.
  resources.geojson   — full national GeoJSON for sitemaps / external use.

Why outside of Hugo: emitting 51 state files plus an index from Hugo
requires either a content-section-per-state (lots of scaffolding) or
hugo's `printf` + `os.WriteFile` pattern (fragile). Doing it in Python is
~30 lines and runs in <1s for 25k entries.

Run:
  python -m scrapers.build_data
"""
from __future__ import annotations

import json
import logging
import shutil
import sys
from collections import defaultdict
from pathlib import Path

import yaml

from scrapers import kb

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
KB_RESOURCES = REPO_ROOT / "kb" / "resources"
KB_GEO = REPO_ROOT / "kb" / "geo"
SITE_DATA = REPO_ROOT / "site" / "static" / "data"
SITE_CONTENT_S = REPO_ROOT / "site" / "content" / "s"
SITE_CONTENT_Z = REPO_ROOT / "site" / "content" / "z"


def load_entries() -> list[dict]:
    """Read every KB resource into memory. ~25k entries, ~50MB resident."""
    entries = []
    skipped = 0
    for path in KB_RESOURCES.iterdir():
        if path.suffix != ".md" or path.name.startswith("_"):
            continue
        try:
            entries.append(kb.read_entry(path))
        except Exception as e:
            log.warning("skip %s: %s", path.name, e)
            skipped += 1
    log.info("loaded %d entries (%d skipped)", len(entries), skipped)
    return entries


def _zip_centers() -> dict:
    """Authoritative zip → centroid lookup, for known zips."""
    p = KB_GEO / "zip-centers.json"
    if not p.exists():
        return {}
    data = json.loads(p.read_text())
    return {k: v for k, v in data.items() if not k.startswith("_")}


def _state_centers() -> dict:
    p = KB_GEO / "state-centers.json"
    data = json.loads(p.read_text())
    return {k: v for k, v in data.items() if not k.startswith("_")}


def _to_org_record(e: dict) -> dict | None:
    """Compact public-facing org record. Drop fields users don't need;
    omit None values to shrink the payload."""
    if e.get("lat") is None or e.get("lng") is None:
        return None
    if not e.get("type") or not e.get("zip"):
        return None
    rec = {
        "id":       e.get("slug") or _slug_from_name_zip(e),
        "url":      f"/r/{_filename_stem(e)}/",
        "name":     e.get("name"),
        "type":     e.get("type"),
        "subtype":  e.get("subtype"),
        "lat":      round(float(e["lat"]), 6),
        "lng":      round(float(e["lng"]), 6),
        "address":  e.get("address"),
        "city":     e.get("city"),
        "state":    e.get("state"),
        "zip":      e.get("zip"),
        "hours":    e.get("hours_raw"),
        "phone":    e.get("phone"),
        "website":  e.get("website"),
        "verified": bool(e.get("verified")),
    }
    return {k: v for k, v in rec.items() if v not in (None, "", False)} | (
        {"verified": True} if e.get("verified") else {}
    )


def _slug_from_name_zip(e: dict) -> str:
    return f"{e.get('type','x')}-{e.get('state','xx')}-{e.get('zip','00000')}-{kb.make_slug(e.get('name',''))}"


def _filename_stem(e: dict) -> str:
    """Reproduce kb.make_filename without .md suffix."""
    return kb.make_filename(e)[:-3]  # strip ".md"


def build(entries: list[dict] | None = None) -> dict:
    """Emit all data files. Returns a small summary dict."""
    if entries is None:
        entries = load_entries()

    SITE_DATA.mkdir(parents=True, exist_ok=True)
    states_dir = SITE_DATA / "states"
    states_dir.mkdir(exist_ok=True)
    # Wipe stale state files (a state with zero entries should disappear)
    for f in states_dir.glob("*.json"):
        f.unlink()

    zip_centers = _zip_centers()
    state_centers = _state_centers()

    # Group entries by state and by (state, zip)
    by_state: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        st = (e.get("state") or "").upper()
        if not st or st not in state_centers:
            continue
        rec = _to_org_record(e)
        if rec is None:
            continue
        by_state[st].append(rec)

    # ----- per-state files -----
    state_summaries = []
    total_orgs = 0
    total_zips = 0
    for st, recs in sorted(by_state.items()):
        # Group by zip
        by_zip: dict[str, dict] = {}
        for r in recs:
            z = (r.get("zip") or "").split("-")[0][:5]
            if not z:
                continue
            grp = by_zip.setdefault(z, {"orgs": [], "center": None})
            grp["orgs"].append(r)
        # Compute centers: known centroid > average of org coords in the zip
        for z, grp in by_zip.items():
            if z in zip_centers:
                grp["center"] = zip_centers[z]
            else:
                lat = sum(o["lat"] for o in grp["orgs"]) / len(grp["orgs"])
                lng = sum(o["lng"] for o in grp["orgs"]) / len(grp["orgs"])
                # Use the city of the first org as the display name
                grp["center"] = {
                    "name": grp["orgs"][0].get("city") or "",
                    "lat":  round(lat, 6),
                    "lng":  round(lng, 6),
                }

        # Group by city (for the low-zoom city-tier daisies). City is just
        # the org's `city` field, lower-cased + stripped to dedupe trivial
        # casing differences.
        by_city: dict[str, dict] = {}
        for r in recs:
            cname = (r.get("city") or "").strip()
            if not cname:
                continue
            key = cname.lower()
            grp = by_city.setdefault(key, {"orgs": [], "name": cname, "center": None})
            grp["orgs"].append(r)
        for key, grp in by_city.items():
            lat = sum(o["lat"] for o in grp["orgs"]) / len(grp["orgs"])
            lng = sum(o["lng"] for o in grp["orgs"]) / len(grp["orgs"])
            grp["center"] = {"name": grp["name"], "lat": round(lat, 6), "lng": round(lng, 6)}
            # Drop the per-org list from city tier — only need the count + petal types
            # to keep payload size down. The full data lives in `zips`.
            grp["count"] = len(grp["orgs"])
            grp["petals"] = sorted({o["type"] for o in grp["orgs"]})
            del grp["orgs"]
            del grp["name"]

        # Per-state petal counts
        petal_counts: dict[str, int] = defaultdict(int)
        for r in recs:
            petal_counts[r["type"]] += 1

        # Write state file
        state_meta = state_centers[st]
        state_payload = {
            "code": st,
            "name": state_meta["name"],
            "center": {"lat": state_meta["lat"], "lng": state_meta["lng"]},
            "counts": dict(petal_counts),
            "total":  len(recs),
            "zip_count": len(by_zip),
            "city_count": len(by_city),
            "cities": by_city,
            "zips": by_zip,
        }
        state_path = states_dir / f"{st}.json"
        state_path.write_text(
            json.dumps(state_payload, separators=(",", ":"), ensure_ascii=False) + "\n",
        )
        state_summaries.append({
            "code":      st,
            "name":      state_meta["name"],
            "lat":       state_meta["lat"],
            "lng":       state_meta["lng"],
            "counts":    dict(petal_counts),
            "total":     len(recs),
            "zip_count": len(by_zip),
        })
        total_orgs += len(recs)
        total_zips += len(by_zip)

    # ----- index.json -----
    index = {
        "states":       state_summaries,
        "petals":       _petals(),
        "total_orgs":   total_orgs,
        "total_zips":   total_zips,
        "total_states": len(state_summaries),
    }
    (SITE_DATA / "index.json").write_text(
        json.dumps(index, separators=(",", ":"), ensure_ascii=False) + "\n",
    )

    # ----- zip-prefix.json -----
    zip_prefix_src = KB_GEO / "zip-prefix.json"
    if zip_prefix_src.exists():
        zp = json.loads(zip_prefix_src.read_text())
        zp = {k: v for k, v in zp.items() if not k.startswith("_")}
        (SITE_DATA / "zip-prefix.json").write_text(
            json.dumps(zp, separators=(",", ":")) + "\n",
        )

    # ----- coverage.json -----
    petals = [p["id"] for p in _petals()]
    cov_full: dict[str, dict] = defaultdict(lambda: {"present": [], "missing": []})
    cov_seen: dict[str, set[str]] = defaultdict(set)
    for st_recs in by_state.values():
        for r in st_recs:
            z = (r.get("zip") or "").split("-")[0][:5]
            if z and r.get("type"):
                cov_seen[z].add(r["type"])
    for z, present in cov_seen.items():
        cov_full[z]["present"] = sorted(present)
        cov_full[z]["missing"] = [p for p in petals if p not in present]
    (SITE_DATA / "coverage.json").write_text(
        json.dumps(dict(cov_full), separators=(",", ":")) + "\n",
    )

    # ----- national GeoJSON (for sitemap / external consumers) -----
    features = []
    for st_recs in by_state.values():
        for r in st_recs:
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [r["lng"], r["lat"]]},
                "properties": {
                    k: v for k, v in r.items() if k not in ("lat", "lng")
                },
            })
    (SITE_DATA / "resources.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": features},
                   separators=(",", ":")) + "\n",
    )

    # ----- Hugo content stubs for /s/{XX}/ and /z/{12345}/ pages -----
    # These give us per-state and per-zip URLs with proper OG meta tags
    # for social sharing. They render with the layout in
    # site/layouts/s/single.html and z/single.html respectively.
    _build_state_stubs(state_summaries)
    _build_zip_stubs(by_state)

    summary = {
        "states":           len(state_summaries),
        "orgs":             total_orgs,
        "zips":             total_zips,
        "index_kb":         (SITE_DATA / "index.json").stat().st_size // 1024,
        "biggest_state_kb": max(
            (states_dir / f"{s['code']}.json").stat().st_size // 1024
            for s in state_summaries
        ) if state_summaries else 0,
        "state_stubs":      len(state_summaries),
    }
    return summary


def _build_state_stubs(state_summaries: list[dict]) -> None:
    """Write site/content/s/{XX}.md per state. Hugo turns these into
    /s/{XX}/ pages with custom layout for OG tags + map embed."""
    SITE_CONTENT_S.mkdir(parents=True, exist_ok=True)
    # Wipe stale stubs first
    for f in SITE_CONTENT_S.glob("*.md"):
        if f.name != "_index.md":
            f.unlink()

    # Section index
    (SITE_CONTENT_S / "_index.md").write_text(
        "---\ntitle: States\nlayout: prose\n---\n",
    )

    for s in state_summaries:
        code = s["code"]
        name = s["name"]
        total = s["total"]
        zip_count = s["zip_count"]
        # Build a brief description for OG
        desc = (
            f"{total:,} community resources in {name} — food, medical, "
            f"and other neighborhood support. Free, public, no tracking."
        )
        front = {
            "title": f"{name}",
            "url": f"/s/{code}/",
            "layout": "state",
            "state_code": code,
            "state_name": name,
            "state_total": total,
            "state_zip_count": zip_count,
            "state_lat": s["lat"],
            "state_lng": s["lng"],
            "description": desc,
        }
        body = ""
        text = "---\n" + yaml.safe_dump(front, sort_keys=False) + "---\n\n" + body
        (SITE_CONTENT_S / f"{code}.md").write_text(text)


def _build_zip_stubs(by_state: dict[str, list[dict]]) -> None:
    """Write site/content/z/{12345}.md per zip. ~10k files. Hugo renders
    each as /z/{12345}/ with OG tags + a map zoomed into that zip."""
    SITE_CONTENT_Z.mkdir(parents=True, exist_ok=True)
    for f in SITE_CONTENT_Z.glob("*.md"):
        if f.name != "_index.md":
            f.unlink()

    (SITE_CONTENT_Z / "_index.md").write_text(
        "---\ntitle: Zip codes\nlayout: prose\n---\n",
    )

    # Aggregate orgs per zip across all states (one zip = one state via prefix
    # rule, but iterate all to be safe in case dedup left straddlers)
    zip_to_orgs: dict[str, list[dict]] = defaultdict(list)
    for st_recs in by_state.values():
        for r in st_recs:
            z = (r.get("zip") or "").split("-")[0][:5]
            if z:
                zip_to_orgs[z].append(r)

    for z, orgs in zip_to_orgs.items():
        if not orgs:
            continue
        # State + city for the title
        first = orgs[0]
        state = first.get("state", "")
        city = first.get("city", "")
        # Center: average of org coords (rough — fine for OG/page header)
        lat = sum(o["lat"] for o in orgs) / len(orgs)
        lng = sum(o["lng"] for o in orgs) / len(orgs)
        # Petal counts
        petal_counts: dict[str, int] = defaultdict(int)
        for o in orgs:
            petal_counts[o["type"]] += 1

        title = f"{city or 'Zip'} {z}" if city else f"Zip {z}"
        desc = (
            f"{len(orgs)} community resources in {city or 'zip ' + z}, {state} — "
            "food, medical, and other neighborhood support."
        )
        front = {
            "title": title,
            "url": f"/z/{z}/",
            "layout": "zip",
            "zip_code": z,
            "zip_state": state,
            "zip_city": city,
            "zip_lat": round(lat, 6),
            "zip_lng": round(lng, 6),
            "zip_total": len(orgs),
            "zip_counts": dict(petal_counts),
            "description": desc,
        }
        text = "---\n" + yaml.safe_dump(front, sort_keys=False) + "---\n"
        (SITE_CONTENT_Z / f"{z}.md").write_text(text)


def _petals() -> list[dict]:
    p = REPO_ROOT / "site" / "data" / "petals.yaml"
    return yaml.safe_load(p.read_text())


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    summary = build()
    print(f"\nBuilt data files in {SITE_DATA}")
    for k, v in summary.items():
        print(f"  {k:20s} {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
