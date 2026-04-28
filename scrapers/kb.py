"""Schema, slug generation, validation, and KB read/write for neighbors-help.

Single module so all four pieces stay in lockstep — change the schema in one
place and the writer/validator/reader update with it.

Output convention: kb/resources/{filename}.md (flat). Filename encodes type +
state + zip + slug + a short hash for collision safety.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field, asdict
from datetime import date
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
KB_RESOURCES = REPO_ROOT / "kb" / "resources"
KB_GEO = REPO_ROOT / "kb" / "geo"

VALID_TYPES = {
    "food", "medical", "housing", "safety", "care", "economy", "education",
}

VALID_SUBTYPES: dict[str, set[str]] = {
    "food": {
        "soup_kitchen", "food_pantry", "community_fridge", "food_bank",
        "snap_enrollment", "community_garden", "meal_delivery", "wic_clinic",
        "farmers_market",
    },
    "medical": {
        "free_clinic", "fqhc", "mental_health", "harm_reduction",
        "dental", "vision", "pharmacy_assistance", "va_facility",
    },
    "housing": {
        "emergency_shelter", "transitional_housing", "warming_center",
        "cooling_center", "rapid_rehousing", "prevention_assistance",
        "domestic_violence_shelter",
    },
    "safety": {
        "crisis_line", "domestic_violence", "legal_aid", "tenant_rights",
        "community_safety", "youth_crisis",
    },
    "care": {
        "childcare", "elder_care", "disability_support", "respite_care",
        "hospice", "laundry_assistance",
    },
    "economy": {
        "mutual_aid_fund", "buy_nothing", "time_bank", "tool_library",
        "clothing_exchange", "utility_assistance", "emergency_fund",
    },
    "education": {
        "public_library", "adult_literacy", "ged_prep", "esl",
        "digital_access", "tutoring", "job_training",
    },
}

VALID_SOURCES = {
    "usda_food_pantries", "usda_snap_retailers", "hrsa_fqhc", "hud_shelters",
    "hud_hopwa", "va_facilities", "osm", "mutual_aid_hub",
    "211_national", "community_pr", "manual", "maintainer", "scraper",
    "tool_library_alliance", "food_not_bombs", "laundry_love",
}
# 211_{state} sources are also valid — checked dynamically.

# Sources whose underlying datasets don't include contact info. We accept
# these without flagging "missing all contact channels" — that's not a
# data-quality problem, it's a property of the dataset.
NO_CONTACT_SOURCES = {
    "usda_snap_retailers",
    "tool_library_alliance",   # Google My Maps form, mostly empty fields
    "food_not_bombs",          # ditto
}

# Continental US bounding box (rough). AK/HI handled separately if needed.
US_LAT_RANGE = (24.0, 50.0)
US_LNG_RANGE = (-125.0, -66.0)
AK_HI_LAT_RANGE = (18.5, 72.0)  # Hawaii to northern Alaska
AK_HI_LNG_RANGE = (-180.0, -129.0)

# Field order for emitted YAML — readability matters because humans edit these.
FIELD_ORDER = [
    # identity
    "name", "slug",
    # taxonomy
    "type", "subtype",
    # location
    "address", "city", "state", "zip", "county",
    "lat", "lng", "geocoded_by", "geocoded_at", "geocode_confidence",
    "needs_geocode_review",
    # contact
    "phone", "phone_alt", "email", "website",
    # hours
    "hours_raw",
    # services / populations
    "services", "populations", "languages",
    # accessibility
    "wheelchair_accessible", "transit_accessible",
    # provenance
    "source", "source_id", "all_sources",
    "verified", "verified_by", "last_checked",
    # data quality
    "needs_review", "review_notes",
    # accountability (defined but not surfaced in templates yet)
    "accountability",
    # metadata
    "created", "updated",
]

# ---------------------------------------------------------------------------
# Slug + filename
# ---------------------------------------------------------------------------

_LEGAL_SUFFIX_RE = re.compile(
    r"\b(inc|llc|ltd|corp|corporation|foundation|of|the)\b\.?",
    re.IGNORECASE,
)

def slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = s.encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")


def make_slug(name: str, *, max_len: int = 50) -> str:
    """Slug for the org name portion only — no city/zip prefix."""
    cleaned = _LEGAL_SUFFIX_RE.sub("", name or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return slugify(cleaned)[:max_len]


def short_hash(*parts: str) -> str:
    """Stable 4-char hash for filename disambiguation."""
    h = hashlib.sha1()
    for p in parts:
        h.update((p or "").encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:4]


def make_filename(entry: dict) -> str:
    """`{type}-{state}-{zip}-{slug}-{hash}.md`

    The hash disambiguates two orgs with similar names in the same zip.
    Computed from address+name so it's stable across pipeline runs.
    """
    t = (entry.get("type") or "unknown").lower()
    state = (entry.get("state") or "xx").lower()
    zip5 = (entry.get("zip") or "00000").split("-")[0][:5] or "00000"
    name_slug = make_slug(entry.get("name") or "unnamed")
    h = short_hash(entry.get("address", ""), entry.get("name", ""), zip5)
    return f"{t}-{state}-{zip5}-{name_slug}-{h}.md"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _is_us_coord(lat: float | None, lng: float | None) -> bool:
    if lat is None or lng is None:
        return False
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return False
    if US_LAT_RANGE[0] <= lat <= US_LAT_RANGE[1] and US_LNG_RANGE[0] <= lng <= US_LNG_RANGE[1]:
        return True
    if AK_HI_LAT_RANGE[0] <= lat <= AK_HI_LAT_RANGE[1] and AK_HI_LNG_RANGE[0] <= lng <= AK_HI_LNG_RANGE[1]:
        return True
    return False


def validate(entry: dict) -> tuple[bool, list[str], list[str]]:
    """Return (is_valid, errors, warnings).

    is_valid=False -> blocked, do not write.
    Warnings are flagged onto entry as needs_review/review_notes by the caller
    if it chooses to keep the entry.
    """
    errors: list[str] = []
    warnings: list[str] = []

    name = (entry.get("name") or "").strip()
    if not name:
        errors.append("missing name")
    elif len(name) > 200:
        errors.append("name too long")

    t = entry.get("type")
    if t not in VALID_TYPES:
        errors.append(f"invalid type: {t!r}")

    sub = entry.get("subtype")
    if sub and t in VALID_SUBTYPES and sub not in VALID_SUBTYPES[t]:
        warnings.append(f"unknown subtype for {t}: {sub!r}")

    state = (entry.get("state") or "").upper()
    if len(state) != 2 or not state.isalpha():
        errors.append(f"invalid state: {entry.get('state')!r}")

    zip_code = (entry.get("zip") or "").split("-")[0]
    if not (len(zip_code) == 5 and zip_code.isdigit()):
        warnings.append("missing or invalid zip")

    if entry.get("lat") is not None and entry.get("lng") is not None:
        if not _is_us_coord(entry.get("lat"), entry.get("lng")):
            warnings.append("coordinates outside US bounds")

    if entry.get("lat") is None and entry.get("lng") is None and not entry.get("needs_geocode_review"):
        warnings.append("missing coordinates")

    if not entry.get("address"):
        warnings.append("missing address")
    # Some sources structurally have no contact channels (e.g. USDA SNAP
    # retailer dataset has no phone/email/web fields). Don't flag these.
    if not entry.get("phone") and not entry.get("email") and not entry.get("website"):
        if entry.get("source") not in NO_CONTACT_SOURCES:
            warnings.append("missing all contact channels")

    src = entry.get("source")
    if src and src not in VALID_SOURCES and not (isinstance(src, str) and src.startswith("211_")):
        warnings.append(f"unknown source: {src!r}")

    return (not errors), errors, warnings


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)


def read_entry(path: Path) -> dict:
    """Parse a KB markdown file into {**frontmatter, body}."""
    text = path.read_text(encoding="utf-8")
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise ValueError(f"{path}: no frontmatter found")
    fm = yaml.safe_load(m.group(1)) or {}
    fm["body"] = m.group(2).strip()
    return fm


# Force quoting of fields that look numeric-but-aren't — US zip codes
# being the canonical example. Without this, PyYAML emits `zip: 06029`
# (unquoted) and Hugo's YAML parser reads it as the float 6029, losing
# the leading zero and silently changing the dict key.
_FORCE_QUOTED_FIELDS = {"zip", "zip4", "phone", "source_id"}


def _ordered_dump(entry: dict) -> str:
    """Emit YAML with our preferred field order so files stay readable.

    Forces single-quote style on fields where stringly-typed numbers are
    common (zip codes especially) to keep downstream YAML parsers happy.
    """
    body = entry.pop("body", "") or ""
    ordered = {}
    for k in FIELD_ORDER:
        if k in entry:
            ordered[k] = _force_string(entry[k]) if k in _FORCE_QUOTED_FIELDS else entry[k]
    # Tail: any unknown keys, alphabetically.
    for k in sorted(entry):
        if k not in ordered:
            ordered[k] = (
                _force_string(entry[k]) if k in _FORCE_QUOTED_FIELDS else entry[k]
            )

    # Build YAML by hand to get explicit quoting on the must-quote fields.
    lines = []
    for k, v in ordered.items():
        lines.append(_yaml_kv(k, v))
    yaml_text = "\n".join(lines) + "\n"
    return f"---\n{yaml_text}---\n\n{body}".rstrip() + "\n"


def _force_string(v):
    if v is None:
        return None
    return str(v)


def _yaml_kv(k: str, v) -> str:
    """Render one top-level frontmatter line. Lists/dicts use safe_dump."""
    if v is None:
        # Skip None entries entirely
        return ""
    if k in _FORCE_QUOTED_FIELDS and isinstance(v, str):
        # Single-quoted scalar — escape embedded single-quotes per YAML spec
        escaped = v.replace("'", "''")
        return f"{k}: '{escaped}'"
    # Use safe_dump for everything else; strip the trailing newline.
    chunk = yaml.safe_dump(
        {k: v},
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=100,
    )
    return chunk.rstrip("\n")


# Fields the writer NEVER overwrites if they already exist on disk.
# Manual edits and community PRs win for these.
PRESERVE_FROM_DISK = {
    "accountability",
    "verified",
    "verified_by",
    "verified_at",
    "notes",
    "wheelchair_accessible",
    "transit_accessible",
}
# review_notes is NOT preserved — it's a per-run computed field. If the
# data is clean now, the prior warnings should disappear. Manual notes
# go in `notes`, not `review_notes`.


def merge_with_existing(scraped: dict, existing: dict) -> dict:
    """Apply preserve-from-disk policy.

    The scraped entry is the proposal; the existing entry is the disk truth.
    Anything in PRESERVE_FROM_DISK that exists on disk wins.
    """
    merged = dict(scraped)
    for f in PRESERVE_FROM_DISK:
        if existing.get(f) not in (None, "", []):
            merged[f] = existing[f]
    # Track all sources that have ever contributed to this entry.
    sources = set(existing.get("all_sources") or [existing.get("source")] or [])
    sources.discard(None)
    if scraped.get("source"):
        sources.add(scraped["source"])
    if sources:
        merged["all_sources"] = sorted(sources)
    return merged


def write_entry(entry: dict, *, kb_root: Path | None = None, dry_run: bool = False) -> Path:
    """Write entry to KB. Returns the path it was (or would be) written to.

    Applies merge-with-existing if a file already exists at the target path.
    Stamps `updated` to today and `created` to today if missing.
    """
    kb_root = kb_root or KB_RESOURCES
    kb_root.mkdir(parents=True, exist_ok=True)

    # Stamp dates
    today = date.today().isoformat()
    entry.setdefault("created", today)
    entry["updated"] = today

    filename = make_filename(entry)
    path = kb_root / filename

    if path.exists():
        try:
            existing = read_entry(path)
            entry = merge_with_existing(entry, existing)
        except Exception as e:
            # If the existing file is malformed, log but still proceed.
            # Caller should handle the warning via the report.
            entry.setdefault("review_notes", "")
            entry["review_notes"] = (
                entry["review_notes"] + f"; existing file unreadable: {e}"
            ).strip("; ")

    text = _ordered_dump(dict(entry))  # copy because _ordered_dump pops body
    if not dry_run:
        path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------

def generate_coverage(kb_root: Path | None = None, out_path: Path | None = None) -> dict:
    """Build {zip: {type: count}} and write to kb/geo/coverage.json.

    Note: this is an authoring-time artifact, distinct from the Hugo
    build-time coverage.json which has present/missing arrays. Both can
    coexist; the Hugo one is the one the site reads.
    """
    kb_root = kb_root or KB_RESOURCES
    out_path = out_path or (KB_GEO / "coverage.json")

    coverage: dict[str, dict[str, int]] = {}
    for entry_file in kb_root.glob("*.md"):
        if entry_file.name.startswith("_"):
            continue
        try:
            e = read_entry(entry_file)
        except Exception:
            continue
        z = (e.get("zip") or "").split("-")[0][:5]
        t = e.get("type")
        if not z or not t:
            continue
        coverage.setdefault(z, {})
        coverage[z][t] = coverage[z].get(t, 0) + 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(coverage, indent=2, sort_keys=True) + "\n")
    return coverage


# ---------------------------------------------------------------------------
# Convenience: bulk write with reporting
# ---------------------------------------------------------------------------

@dataclass
class WriteReport:
    valid: int = 0
    blocked: int = 0
    needs_review: int = 0
    blocked_examples: list[tuple[str, list[str]]] = field(default_factory=list)
    paths: list[Path] = field(default_factory=list)


def write_many(
    entries: Iterable[dict], *, kb_root: Path | None = None, dry_run: bool = False,
) -> WriteReport:
    """Validate + write a batch. Returns a report."""
    report = WriteReport()
    for e in entries:
        ok, errors, warnings = validate(e)
        if not ok:
            report.blocked += 1
            if len(report.blocked_examples) < 20:
                report.blocked_examples.append((e.get("name") or "<no name>", errors + warnings))
            continue
        if warnings:
            e["needs_review"] = True
            existing_notes = (e.get("review_notes") or "").strip()
            new_notes = "; ".join(warnings)
            e["review_notes"] = "; ".join(p for p in (existing_notes, new_notes) if p)
            report.needs_review += 1
        path = write_entry(e, kb_root=kb_root, dry_run=dry_run)
        report.paths.append(path)
        report.valid += 1
    return report
