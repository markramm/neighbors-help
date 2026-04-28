"""Cross-source deduplication.

When multiple scrapers find the same organization, merge their records.

Rules of the game (per spec, with refinements from probe day):

  1. Match candidates within the same (type, zip5). We never merge across
     petal types — an org that provides BOTH food and shelter legitimately
     appears twice (it's two different services from a user's perspective).

  2. Use rapidfuzz token_sort_ratio on name and address. Both >=85 ⇒ match.
     Name >=95 + same phone ⇒ match. Same phone + same zip + addr >=70 ⇒ match.

  3. Higher-priority source wins on field conflicts (HRSA > VA > HUD > USDA
     > 211 > Mutual Aid Hub > OSM > community PR).

  4. Field-level merge: missing fields in canonical are filled from the
     duplicate. Services lists are unioned. `all_sources` records every
     source that contributed.

  5. Subtypes do not need to match. A "fqhc" entry from HRSA dedupes against
     a "free_clinic" entry from OSM if name+address match — keep the canonical
     subtype.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Iterable

import re

from rapidfuzz import fuzz

log = logging.getLogger(__name__)

# Address suffix normalization. "123 Main St" and "123 Main Street" should
# match — they're the same address with different abbreviation choices.
_SUFFIX_NORM = {
    r"\bst\b": "street",
    r"\bave\b": "avenue",
    r"\brd\b": "road",
    r"\bblvd\b": "boulevard",
    r"\bdr\b": "drive",
    r"\bln\b": "lane",
    r"\bct\b": "court",
    r"\bpl\b": "place",
    r"\bpkwy\b": "parkway",
    r"\bcir\b": "circle",
    r"\bter\b": "terrace",
    r"\bhwy\b": "highway",
    r"\bn\b": "north",
    r"\bs\b": "south",
    r"\be\b": "east",
    r"\bw\b": "west",
    r"\bne\b": "northeast",
    r"\bnw\b": "northwest",
    r"\bse\b": "southeast",
    r"\bsw\b": "southwest",
    r"[.,#]": " ",
}


def _norm_addr(s: str) -> str:
    s = (s or "").lower().strip()
    for pat, repl in _SUFFIX_NORM.items():
        s = re.sub(pat, repl, s)
    return re.sub(r"\s+", " ", s).strip()

SOURCE_PRIORITY = [
    "hrsa_fqhc",            # federal, maintained, high quality
    "va_facilities",
    "hud_shelters",
    "hud_hopwa",
    "usda_food_pantries",
    "usda_snap_retailers",
    "211_national",
    "mutual_aid_hub",
    "osm",
    "community_pr",
    "manual",
    "maintainer",
]


def source_rank(entry: dict) -> int:
    src = entry.get("source") or "osm"
    if src.startswith("211_"):
        # All 211 state sources rank where 211_national ranks
        try:
            return SOURCE_PRIORITY.index("211_national")
        except ValueError:
            return len(SOURCE_PRIORITY)
    try:
        return SOURCE_PRIORITY.index(src)
    except ValueError:
        return len(SOURCE_PRIORITY)


def _zip5(entry: dict) -> str:
    z = entry.get("zip") or ""
    return str(z).split("-")[0][:5]


_TRAILING_NUM_RE = re.compile(r"\s+(?:#?\d+|[ivx]+|unit|center|site)$", re.IGNORECASE)


def _names_differ_only_in_trailing_id(a: str, b: str) -> bool:
    """`Mobile Dental 1` vs `Mobile Dental 2` — same prefix, different number.

    These are distinct units sharing a base name, NOT duplicates.
    """
    a, b = a.strip().lower(), b.strip().lower()
    if a == b:
        return False
    a_stem = _TRAILING_NUM_RE.sub("", a).strip()
    b_stem = _TRAILING_NUM_RE.sub("", b).strip()
    if a_stem != a and b_stem != b and a_stem == b_stem:
        return True
    # Also catch "X 1" vs "X 2" without word boundary
    a_no_tail = re.sub(r"\s*\d+\s*$", "", a).strip()
    b_no_tail = re.sub(r"\s*\d+\s*$", "", b).strip()
    if a_no_tail and a_no_tail == b_no_tail and a != b:
        return True
    return False


def _is_match(a: dict, b: dict) -> bool:
    """Decide whether two entries describe the same organization.

    The danger is over-merging: distinct clinics in the same medical plaza
    (same address, different names) must NOT merge. Mobile units 1/2/3 at
    a single address are distinct services. Two food vendors at one
    farmers' market are distinct.

    Match conditions (any one):
      - name >=95 + addr >=70  (typos / case / minor variation)
      - name >=88 + addr >=95  (abbreviation variants only)
      - same phone + name >=85 (different sources spell the same org slightly
        differently but share the public-facing phone number)

    Reject conditions (all of these veto a match regardless of scores):
      - Names differ only in trailing digits / unit numbers (Mobile 1 vs 2)
    """
    name_a = (a.get("name") or "").lower().strip()
    name_b = (b.get("name") or "").lower().strip()
    if not name_a or not name_b:
        return False

    if _names_differ_only_in_trailing_id(name_a, name_b):
        return False

    name_score = fuzz.token_sort_ratio(name_a, name_b)
    addr_a = _norm_addr(a.get("address"))
    addr_b = _norm_addr(b.get("address"))
    addr_score = fuzz.token_set_ratio(addr_a, addr_b) if (addr_a and addr_b) else 0

    phone_a = _digits(a.get("phone"))
    phone_b = _digits(b.get("phone"))
    same_phone = bool(phone_a) and phone_a == phone_b

    if name_score >= 95 and addr_score >= 70:
        return True
    if name_score >= 88 and addr_score >= 95:
        return True
    # Same phone is suggestive but not sufficient — districts and hospital
    # systems share switchboard numbers across distinct sites. Require
    # very-strong name agreement on top of same phone.
    if same_phone and name_score >= 95:
        return True
    return False


def _digits(s) -> str:
    if not s:
        return ""
    return "".join(c for c in str(s) if c.isdigit())


def _merge_into(canonical: dict, dup: dict) -> dict:
    """Merge `dup` into `canonical`. Canonical wins on direct conflicts;
    missing canonical fields are filled from `dup`. `all_sources` accumulates.

    Mutates and returns `canonical`.
    """
    # Track every source ID that contributed to this entry
    sources = set(canonical.get("all_sources") or ([canonical.get("source")] if canonical.get("source") else []))
    if dup.get("source"):
        sources.add(dup["source"])
    sources.discard(None)
    if sources:
        canonical["all_sources"] = sorted(sources)

    # Track all source IDs too (useful for re-fetches)
    src_ids = set(canonical.get("all_source_ids") or ([canonical.get("source_id")] if canonical.get("source_id") else []))
    if dup.get("source_id"):
        src_ids.add(dup["source_id"])
    src_ids.discard(None)
    if src_ids:
        canonical["all_source_ids"] = sorted(src_ids)

    # Fill in missing scalar fields from the duplicate
    fill_if_missing = [
        "phone", "phone_alt", "email", "website",
        "hours_raw", "address", "county",
        "wheelchair_accessible", "transit_accessible",
        "subtype",
    ]
    for f in fill_if_missing:
        if not canonical.get(f) and dup.get(f) is not None:
            canonical[f] = dup[f]

    # Union list-valued fields
    for f in ["services", "populations", "languages"]:
        u = set(canonical.get(f) or [])
        u.update(dup.get(f) or [])
        if u:
            canonical[f] = sorted(u)

    # Coordinates: prefer canonical, but use dup if canonical lacks them
    if (canonical.get("lat") is None or canonical.get("lng") is None) and dup.get("lat") is not None:
        canonical["lat"] = dup["lat"]
        canonical["lng"] = dup["lng"]
        canonical["geocoded_by"] = dup.get("geocoded_by")
        canonical["geocoded_at"] = dup.get("geocoded_at")
        canonical["geocode_confidence"] = dup.get("geocode_confidence")

    return canonical


def deduplicate(entries: Iterable[dict]) -> list[dict]:
    """Group by (type, zip5), find matches via fuzzy name+address.

    Returns a deduped list. Order: items grouped together, with the
    highest-priority source as the canonical entry.
    """
    # Group by (type, zip5)
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    no_zip: list[dict] = []
    for e in entries:
        t = e.get("type")
        z = _zip5(e)
        if not t or not z:
            no_zip.append(e)
            continue
        groups[(t, z)].append(e)

    out: list[dict] = []
    merged_count = 0

    for (t, z), bucket in groups.items():
        # Sort by source priority — best sources first
        bucket.sort(key=source_rank)
        canonical_list: list[dict] = []
        for entry in bucket:
            matched = None
            for cand in canonical_list:
                if _is_match(entry, cand):
                    matched = cand
                    break
            if matched is not None:
                _merge_into(matched, entry)
                merged_count += 1
            else:
                canonical_list.append(dict(entry))  # copy to avoid mutating input
        out.extend(canonical_list)

    out.extend(no_zip)
    log.info(
        "dedup: %d in -> %d out (merged %d), %d unzipped",
        sum(len(b) for b in groups.values()) + len(no_zip), len(out),
        merged_count, len(no_zip),
    )
    return out
