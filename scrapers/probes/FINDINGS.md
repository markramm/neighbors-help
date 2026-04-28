# Probe day findings — 2026-04-27

Reality vs. spec for the four federal sources we planned to launch with.

## ✅ HRSA FQHC — viable, **revised path**

**Spec said:** `https://data.hrsa.gov/DataDownload/DD_Files/BCD_HCCN.zip`

**Real:** `https://data.hrsa.gov/DataDownload/DD_Files/Health_Center_Service_Delivery_and_LookAlike_Sites.csv`

- Public CSV, no key, ~unknown total record count (Content-Length not returned, but >50KB partial returned)
- Includes lat/lng (`Geocoding Artifact Address Primary X/Y Coordinate`) so geocoding can be skipped
- Includes hours (`Operating Hours per Week`), phone, website, county, full address
- Distinguishes site type (FQHC vs Look-Alike) and operating status

**Field mapping needed (different from spec):**
- `Site Name` → name
- `Site Address`/`Site City`/`Site State Abbreviation`/`Site Postal Code` → address/city/state/zip
- `Site Telephone Number` → phone
- `Site Web Address` → website
- `Operating Hours per Week` → hours_raw (numeric, e.g. "40" — not a schedule string)
- `Geocoding Artifact Address Primary Y Coordinate` → lat
- `Geocoding Artifact Address Primary X Coordinate` → lng
- `Site Status Description` → filter to active sites only
- `Health Center Type Description` → subtype (FQHC vs Look-Alike)

**The spec's `Operates_Dental` / `Operates_Mental_Health` columns don't appear in this dataset.** Service-level detail may need a second HRSA dataset (UDS) or be omitted from v1.

---

## ⚠️ VA Facilities — needs free API key registration

**Spec said:** Public bulk CSV at `https://www.data.va.gov/api/views/fxfw-k5jc/rows.csv` (404)

**Real:** VA Lighthouse API at `https://api.va.gov/services/va_facilities/v1/facilities` requires an API key from `https://developer.va.gov`. Registration is free but manual.

- v0 endpoints (no auth) are 404 — fully retired
- v1 returns `401 No API key found in request` without one
- No alternative bulk download discovered

**Decision needed:**
- (a) **Register for VA API key** under TCP LLC — ~5 min, free, blocks pipeline until key in place
- (b) **Skip VA for v1** — defer to v1.5
- (c) **Manual maintenance** for VA facilities in priority regions

I'd recommend **(a)** — it's the lowest cost and unblocks the spec's intent. The key goes in GitHub Actions secrets as `VA_LIGHTHOUSE_API_KEY`. **Action item for you, Mark.**

---

## ✅ USDA SNAP retailers — viable, **smaller scope than spec assumed**

**Spec said:** `https://www.fns.usda.gov/sites/default/files/snap/stores.csv` (404), filter to "Non-Profit", "Co-op", "Farmers' Market"

**Real:** ArcGIS Hub at `https://services1.arcgis.com/RLQu0rK7h4kbsBq5/arcgis/rest/services/snap_retailer_location_data/FeatureServer/0`. Paginated, max 1000/query.

**Discovered Store_Type values (8 total, all there is):**
- Convenience Store (113,834) — not relevant
- Grocery Store (22,441) — not relevant
- Farmers and Markets (**7,207**) — RELEVANT
- Specialty Store (5,598) — mixed, mostly not relevant
- Other (58,329) — mostly Dollar General etc., not relevant
- Restaurant Meals Program — not relevant
- Super Store / Supermarket — not relevant

**Spec was wrong about the categories.** USDA does not categorize SNAP retailers as "Non-Profit" or "Co-op." The only relevant filter is `Farmers and Markets` — these are EBT-accepting markets, often EBT-doubled. ~7k records.

**Subtype:** `farmers_market` (need to add to schema vocabulary — currently we only have `wic_clinic`/`snap_enrollment` adjacent). Recommend adding `farmers_market` as a new subtype under `food`.

---

## ❌ HUD shelters — **not buildable from public data as spec assumed**

**Spec said:** Bulk shelter / emergency housing data via HUD Exchange or HUD User API.

**Real:** HUD does NOT publish a point-level emergency shelter list. The actual shelter-level data lives in HMIS (Homeless Management Information System) which is privacy-restricted at the address level. HUD publishes:

- **CoC grantee polygons** (administrative areas, not facilities)
- **HOPWA grantee polygons** (admin areas)
- **Public Housing Developments** (long-term subsidized residences, not emergency shelters)
- **Multifamily Properties - Assisted** (Section 8/202/811 housing, not shelters)
- **HUD Field Office Locations** (admin offices)

**None of these are emergency shelters or what a neighbor in crisis would walk into.**

**Real options for shelter data:**
1. **OSM `social_facility=shelter`** — free, community-maintained, variable coverage by region. Already in the spec's Phase 2.
2. **Manual curation** by region — what champions/maintainers add as they cover their areas.
3. **State 211 partnerships** — varies, some states publish, some are paywalled.
4. **National Coalition for the Homeless directory** — community-maintained.

**Recommended pivot:** Drop the HUD shelter scraper from v1. Replace with:
- Promote OSM `social_facility=shelter` from Phase 2 to v1 (it was deferred but it's our only national point-level shelter source)
- Manual seed shelters in launch regions

This is a real spec correction, not a delay. The spec's HUD plan is not buildable.

---

## Summary — recommended v1 federal source set

Given findings, the realistic v1 federal scrapers are:

| Source | Status | Records (est) | Coverage |
|---|---|---|---|
| **HRSA FQHC** | ✅ Ready to build | ~15,000 | Medical (national) |
| **USDA SNAP — Farmers and Markets only** | ✅ Ready to build | ~7,000 | Food (national) |
| **VA Facilities** | ⏸ Needs API key | ~1,200 | Medical (national, veteran-focused) |
| **HUD shelters** | ❌ Not buildable as spec'd | — | — |
| **OSM (promoted from Phase 2)** | ⏳ Spec change | ~varies | Housing/safety/care/etc (variable) |

**My recommendation:** Build HRSA + USDA-Farmers as v1, register for VA API key in parallel, drop HUD shelters and add OSM as the housing/social-services source instead. This delivers food + medical national coverage on day one and acknowledges that shelter data is a fundamentally harder problem.

**Decisions needed from you (Mark):**

1. Register for VA Lighthouse API key under TCP LLC, or defer VA to v1.5?
2. Drop HUD shelters from v1 and promote OSM to v1, or keep HUD off v1 with no shelter data?
3. Add `farmers_market` to the `food` subtype vocabulary?

Once you answer, I'll build HRSA + SNAP-Farmers immediately (both have everything they need) and queue the VA scraper to flip on once the key arrives.
