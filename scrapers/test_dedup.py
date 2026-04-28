"""Tests for the deduplicator."""
from __future__ import annotations

from scrapers.normalize.dedup import (
    deduplicate, source_rank, _is_match, _merge_into,
)


def t(label: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        raise SystemExit(1)


def test_priority() -> None:
    print("priority")
    t("hrsa < osm",   source_rank({"source": "hrsa_fqhc"}) < source_rank({"source": "osm"}))
    t("usda < osm",   source_rank({"source": "usda_food_pantries"}) < source_rank({"source": "osm"}))
    t("211 mid",      source_rank({"source": "211_michigan"}) < source_rank({"source": "osm"}))
    t("unknown last", source_rank({"source": "made_up"}) >= len([
        "hrsa_fqhc","va_facilities","hud_shelters","hud_hopwa","usda_food_pantries",
        "usda_snap_retailers","211_national","mutual_aid_hub","osm","community_pr","manual","maintainer"]))


def test_match_basic() -> None:
    print("match basic")
    a = {"name": "Hope Pantry", "address": "123 Main St", "phone": "555-1234"}
    b = {"name": "Hope Pantry", "address": "123 Main Street", "phone": "555-1234"}
    t("similar match", _is_match(a, b))

    c = {"name": "Hope Pantry", "address": "123 Main", "phone": "555-1234"}
    d = {"name": "Different Org", "address": "999 Elsewhere"}
    t("no match different name", not _is_match(c, d))


def test_match_phone_strong() -> None:
    print("match by phone")
    a = {"name": "Hope Pantry", "address": "1 X St", "phone": "(555) 555-1234"}
    b = {"name": "Hope Pantry", "address": "1 X Street", "phone": "5555551234"}
    t("phone+name=match", _is_match(a, b))

    # Same phone but distinct facilities — district switchboard etc.
    c = {"name": "Pontiac High School Health Center",
         "address": "1051 Arlene Ave", "phone": "(248) 724-7600"}
    d = {"name": "Pontiac Middle School Health Center",
         "address": "1275 N Perry St", "phone": "(248) 724-7600"}
    t("shared switchboard != merge", not _is_match(c, d))


def test_dedup_groups_within_type() -> None:
    print("dedup groups within type")
    entries = [
        {"type": "food", "zip": "48197", "name": "Hope Pantry",
         "address": "123 Main", "source": "osm"},
        {"type": "food", "zip": "48197", "name": "Hope Pantry",
         "address": "123 Main St", "source": "hrsa_fqhc",
         "phone": "(555) 555-1234"},
    ]
    out = deduplicate(entries)
    t("merged to 1",   len(out) == 1)
    # HRSA wins because higher priority — should keep its source
    t("hrsa canonical", out[0]["source"] == "hrsa_fqhc")
    t("phone preserved", out[0]["phone"] == "(555) 555-1234")
    t("all_sources",   set(out[0].get("all_sources", [])) == {"hrsa_fqhc", "osm"})


def test_dedup_keeps_different_types() -> None:
    print("different types stay separate")
    # Same physical org, two different categories — should keep BOTH
    entries = [
        {"type": "housing", "zip": "48103", "name": "Shelter Association",
         "address": "312 W Huron", "source": "manual"},
        {"type": "medical", "zip": "48103", "name": "Shelter Association",
         "address": "312 W Huron", "source": "hrsa_fqhc"},
    ]
    out = deduplicate(entries)
    t("kept both",     len(out) == 2)
    types = {e["type"] for e in out}
    t("one of each",   types == {"housing", "medical"})


def test_dedup_priority_wins() -> None:
    print("priority wins on conflicts")
    # 3 sources, same org. HRSA (highest) should be canonical.
    entries = [
        {"type": "food", "zip": "48197", "name": "Org X", "address": "1 A St",
         "source": "osm",                "phone": ""},
        {"type": "food", "zip": "48197", "name": "Org X", "address": "1 A St",
         "source": "usda_food_pantries", "phone": "(555) 100-0000"},
        {"type": "food", "zip": "48197", "name": "Org X", "address": "1 A Street",
         "source": "hrsa_fqhc",          "phone": "(555) 999-9999"},
    ]
    out = deduplicate(entries)
    t("merged to 1",      len(out) == 1)
    t("hrsa wins phone",  out[0]["phone"] == "(555) 999-9999")
    t("3 sources tracked",
        set(out[0].get("all_sources", [])) == {"hrsa_fqhc", "usda_food_pantries", "osm"})


def test_dedup_fills_gaps() -> None:
    print("dedup fills missing fields")
    entries = [
        # HRSA has name+address but no website
        {"type": "medical", "zip": "48197", "name": "Clinic Y", "address": "1 X St",
         "source": "hrsa_fqhc"},
        # OSM has same org plus a website
        {"type": "medical", "zip": "48197", "name": "Clinic Y", "address": "1 X St",
         "source": "osm", "website": "https://example.org"},
    ]
    out = deduplicate(entries)
    t("merged",            len(out) == 1)
    t("hrsa is canonical", out[0]["source"] == "hrsa_fqhc")
    t("osm website filled in", out[0].get("website") == "https://example.org")


def test_dedup_unions_services() -> None:
    print("union services")
    entries = [
        {"type": "medical", "zip": "48197", "name": "Z", "address": "1 X",
         "source": "hrsa_fqhc", "services": ["dental", "vision"]},
        {"type": "medical", "zip": "48197", "name": "Z", "address": "1 X St",
         "source": "osm", "services": ["mental_health"]},
    ]
    out = deduplicate(entries)
    t("merged", len(out) == 1)
    t("services unioned", set(out[0].get("services", [])) == {"dental", "vision", "mental_health"})


def test_no_merge_trailing_id() -> None:
    print("don't merge units differing only in trailing id")
    # Same address, names differ only in trailing digit — NOT duplicates.
    pairs = [
        ("Mobile Dental 1",        "Mobile Dental 2"),
        ("Health Center Mobile 3", "Health Center Mobile 4"),
        ("FQHC Site #1",           "FQHC Site #2"),
        ("Pediatrics Unit 1",      "Pediatrics Unit 2"),
    ]
    for n1, n2 in pairs:
        a = {"name": n1, "address": "501 Lapeer Ave"}
        b = {"name": n2, "address": "501 Lapeer Ave"}
        t(f"!match {n1!r} vs {n2!r}", not _is_match(a, b))


def test_no_merge_distinct_clinics_same_address() -> None:
    print("don't merge distinct clinics at the same address")
    # "Care Free Medical" / "Care Free Dental" / "Care Free Optometry"
    # all at 1100 W Saginaw — three distinct services.
    a = {"name": "Care Free Medical",   "address": "1100 W Saginaw St"}
    b = {"name": "Care Free Dental",    "address": "1100 W Saginaw St"}
    c = {"name": "Care Free Optometry", "address": "1100 W Saginaw St"}
    t("med != dental", not _is_match(a, b))
    t("med != opt",    not _is_match(a, c))
    t("dental != opt", not _is_match(b, c))


def test_dedup_no_zip() -> None:
    print("no-zip entries pass through")
    entries = [
        {"type": "food", "name": "X", "source": "osm"},  # no zip
        {"type": "food", "zip": "48197", "name": "Y", "source": "osm"},
    ]
    out = deduplicate(entries)
    t("both kept",  len(out) == 2)


if __name__ == "__main__":
    test_priority()
    test_match_basic()
    test_match_phone_strong()
    test_dedup_groups_within_type()
    test_dedup_keeps_different_types()
    test_dedup_priority_wins()
    test_dedup_fills_gaps()
    test_dedup_unions_services()
    test_no_merge_trailing_id()
    test_no_merge_distinct_clinics_same_address()
    test_dedup_no_zip()
    print("\nAll tests passed.")
