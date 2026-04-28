"""Smoke tests for kb.py. Run: .venv/bin/python -m scrapers.test_kb"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from scrapers import kb


def t(label: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        raise SystemExit(1)


def test_slug() -> None:
    print("slug")
    t("basic",      kb.slugify("Community Kitchen at First UMC") == "community-kitchen-at-first-umc")
    t("unicode",    kb.slugify("Niños & Familias") == "ninos-familias")
    t("punct",      kb.slugify("AA / BB ___ CC") == "aa-bb-cc")
    t("name_strip", kb.make_slug("Hope Foundation, Inc.") == "hope")
    t("max_len",    len(kb.make_slug("a" * 200)) == 50)


def test_filename() -> None:
    print("filename")
    e1 = {"type": "food", "state": "MI", "zip": "48197",
          "name": "Hope Pantry", "address": "123 Main St"}
    e2 = {"type": "food", "state": "MI", "zip": "48197",
          "name": "Hope Pantry", "address": "456 Elm St"}
    f1, f2 = kb.make_filename(e1), kb.make_filename(e2)
    t("name encoded",    "hope-pantry" in f1)
    t("type prefix",     f1.startswith("food-mi-48197-"))
    t("ends md",         f1.endswith(".md"))
    t("hash disambig",   f1 != f2, f"both {f1}")
    t("stable",          kb.make_filename(e1) == f1)


def test_validate() -> None:
    print("validate")
    good = {"name": "X", "type": "food", "state": "MI", "zip": "48197",
            "lat": 42.24, "lng": -83.61, "address": "1 Main", "phone": "555"}
    ok, err, warn = kb.validate(good)
    t("good passes",     ok and not err and not warn)

    bad = {"name": "", "type": "nope", "state": "Michigan"}
    ok, err, warn = kb.validate(bad)
    t("blocked on bad",  not ok)
    t("3 errors",        len(err) >= 3, f"got {err}")

    no_coords = dict(good); no_coords.pop("lat"); no_coords.pop("lng")
    ok, err, warn = kb.validate(no_coords)
    t("warns no coords", ok and "missing coordinates" in warn)

    bad_coords = dict(good); bad_coords["lat"] = 90.0; bad_coords["lng"] = 0.0
    ok, err, warn = kb.validate(bad_coords)
    t("warns oob coords", ok and any("outside" in w for w in warn))

    ak = dict(good); ak["lat"] = 61.2; ak["lng"] = -149.9; ak["state"] = "AK"
    ok, err, warn = kb.validate(ak)
    t("AK valid",        ok and not any("outside" in w for w in warn))

    bad_subtype = dict(good); bad_subtype["subtype"] = "not_a_real_subtype"
    ok, err, warn = kb.validate(bad_subtype)
    t("warns bad subtype", ok and any("subtype" in w for w in warn))


def test_leading_zero_zip_quoted() -> None:
    print("leading-zero zip stays a string")
    tmp = Path(tempfile.mkdtemp())
    try:
        # Connecticut zips, Puerto Rico zips, Massachusetts zips — all start
        # with 0. Without forced quoting these get parsed as floats and lose
        # the leading zero downstream.
        for zip_in in ["06029", "00601", "01001", "08037"]:
            e = {
                "name": "X", "type": "food", "state": "CT",
                "zip": zip_in, "lat": 41.5, "lng": -72.5,
                "address": "1 Main", "phone": "555", "source": "manual",
            }
            path = kb.write_entry(e, kb_root=tmp)
            text = path.read_text()
            t(f"{zip_in} quoted",
              f"zip: '{zip_in}'" in text,
              f"got: {[l for l in text.splitlines() if l.startswith('zip:')]}")
            # Round-trip via PyYAML should give back the string
            back = kb.read_entry(path)
            t(f"{zip_in} round-trips as string",
              back["zip"] == zip_in,
              f"got {back['zip']!r}")
    finally:
        shutil.rmtree(tmp)


def test_roundtrip() -> None:
    print("roundtrip")
    tmp = Path(tempfile.mkdtemp())
    try:
        e = {
            "name": "Hope Pantry", "type": "food", "subtype": "food_pantry",
            "address": "123 Main St", "city": "Ypsilanti", "state": "MI",
            "zip": "48197", "lat": 42.24, "lng": -83.61, "phone": "555-0100",
            "source": "manual", "verified": False,
        }
        path = kb.write_entry(e, kb_root=tmp)
        t("file exists",      path.exists())
        text = path.read_text()
        t("has frontmatter",  text.startswith("---\n"))
        t("ends newline",     text.endswith("\n"))
        # Field order check: name should come before type in YAML output
        name_idx, type_idx = text.find("name:"), text.find("type:")
        t("name before type", 0 < name_idx < type_idx)

        back = kb.read_entry(path)
        t("name preserved",   back["name"] == e["name"])
        t("zip preserved",    back["zip"] == e["zip"])
        t("created stamped",  "created" in back)
        t("updated stamped",  "updated" in back)
    finally:
        shutil.rmtree(tmp)


def test_merge() -> None:
    print("merge")
    tmp = Path(tempfile.mkdtemp())
    try:
        # Write existing file with manual edits
        existing = {
            "name": "Hope Pantry", "type": "food", "address": "123 Main",
            "city": "Y", "state": "MI", "zip": "48197",
            "verified": True, "verified_by": "maintainer",
            "accountability": ["Reported issue, 2026-04-15"],
            "notes": "Manual note that should survive scrapers.",
            "source": "manual",
        }
        path = kb.write_entry(existing, kb_root=tmp)

        # Now scraper proposes an update with different verified state
        scraped = {
            "name": "Hope Pantry", "type": "food", "address": "123 Main",
            "city": "Y", "state": "MI", "zip": "48197",
            "verified": False, "verified_by": "scraper",
            "phone": "(555) 555-1234",       # new info from scraper
            "source": "hrsa_fqhc",
        }
        kb.write_entry(scraped, kb_root=tmp)
        result = kb.read_entry(path)
        t("manual verified survives",  result["verified"] is True)
        t("manual verified_by survives", result["verified_by"] == "maintainer")
        t("accountability survives",   result.get("accountability") == ["Reported issue, 2026-04-15"])
        t("notes survives",            "Manual note" in (result.get("notes") or ""))
        t("scraper phone applied",     result["phone"] == "(555) 555-1234")
        t("all_sources merged",        set(result.get("all_sources", [])) >= {"manual", "hrsa_fqhc"})
    finally:
        shutil.rmtree(tmp)


def test_write_many() -> None:
    print("write_many")
    tmp = Path(tempfile.mkdtemp())
    try:
        entries = [
            {"name": "Good", "type": "food", "state": "MI", "zip": "48197",
             "lat": 42.24, "lng": -83.61, "address": "1 X", "phone": "555"},
            {"name": "", "type": "food", "state": "MI"},     # blocked: no name
            {"name": "Warn", "type": "food", "state": "MI"}, # warns: missing coords + zip + addr
        ]
        report = kb.write_many(entries, kb_root=tmp)
        t("2 valid",        report.valid == 2)
        t("1 blocked",      report.blocked == 1)
        t("1 needs review", report.needs_review == 1)
    finally:
        shutil.rmtree(tmp)


def test_coverage() -> None:
    print("coverage")
    tmp = Path(tempfile.mkdtemp())
    try:
        for i, t_ in enumerate(["food", "medical", "food"]):
            kb.write_entry({"name": f"Org {i}", "type": t_, "state": "MI",
                            "zip": "48197", "lat": 42.24, "lng": -83.61,
                            "address": f"{i} Main", "source": "manual"},
                           kb_root=tmp)
        out = tmp / "coverage.json"
        cov = kb.generate_coverage(kb_root=tmp, out_path=out)
        t("48197 in cov",   "48197" in cov)
        t("food count",     cov["48197"].get("food") == 2)
        t("medical count",  cov["48197"].get("medical") == 1)
    finally:
        shutil.rmtree(tmp)


if __name__ == "__main__":
    test_slug()
    test_filename()
    test_validate()
    test_leading_zero_zip_quoted()
    test_roundtrip()
    test_merge()
    test_write_many()
    test_coverage()
    print("\nAll tests passed.")
