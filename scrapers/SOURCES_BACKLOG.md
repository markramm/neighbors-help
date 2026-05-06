# Source backlog

Candidate data sources for future scrapers, with notes on access and feasibility.
Add to this file as new sources turn up — edit anywhere, anyone can PR. The
top of the file lists shipped sources; everything below is candidate work.

## Shipped

| Source | Coverage | Records (US) | Type | Subtype | Access |
|---|---|---|---|---|---|
| HRSA FQHC | National | ~18,800 | medical | fqhc | Public CSV bulk download |
| USDA SNAP — Farmers and Markets | National | ~7,200 | food | farmers_market | ArcGIS REST FeatureServer |
| Laundry Love | National | ~116 visible US | care | laundry_assistance | StoreRocket public JSON API |
| Food Not Bombs | National | ~25 US chapters (2025 list) | food | soup_kitchen | Google My Maps KML; reverse-geocode for state/zip |
| Tool Library Alliance | Worldwide → US | ~48 US | economy | tool_library | Google My Maps KML; reverse-geocode for state/zip |
| Salvation Army | National | ~3,200 corps locations | care | community_support | Sitemap walk + per-page address extraction from inline cookieManager.set() calls; Census forward geocode |
| National Diaper Bank Network | National | ~240 cities | care | diaper_bank | Single HTML page parse + Nominatim city-centroid geocoding |

## Candidates — reasonable-to-build

| Source | Petal | Likely access | Risk / notes |
|---|---|---|---|
| **Mutual Aid Hub** (mutualaidhub.org) | economy / food / care | Webflow/Mapbox-rendered map. No documented API; need to inspect what the map JS fetches. Spec'd in original plan. | Many entries are COVID-era inactive — will need a `last_active` filter or community vetting before publishing. |

## Candidates — needs investigation before committing

| Source | Petal | Why it's promising | Why I'm cautious |
|---|---|---|---|
| **Organize Directory** (organize.directory/location) | depends on org type | National org directory by location, well-indexed. | "Organizing" framing may pull in electoral/political groups that are out-of-scope per spec ("nothing requiring explanation to a hostile reader"). Need to filter carefully. |
| **Find a Protest** (findaprotest.info/directory) | safety / community | Lists grassroots orgs and "ways to get involved." | Same risk as Organize Directory — political adjacency. Read carefully before pulling in. Returned 403 to my probe. |
| **The County Office** (thecountyoffice.com/charity-non-profit) | mixed | Aggregator of all US nonprofits/charities. Massive. | Aggregator data quality unknown; could include defunct or fraudulent entities. Better to source from IRS Form 990 directly if we want the nonprofit universe (then filter). |

## Started but blocked / deferred

- **Meals on Wheels America** — probed 2026-05-06. Locator at
  mealsonwheelsamerica.org/find-meals-and-services/ takes a `pc=ZIP` GET
  param and returns server-rendered HTML with results. No public API,
  no sitemap-of-providers, no "show all" listing. Per-zip iteration
  would require ~3,000 queries at ~1s each = 50min/run with heavy
  cross-zip duplication that needs careful dedup.

  **Decision:** Skip for v1.5 in favor of cleaner federal sources.
  Revisit when we have time to do per-state zip-iteration carefully.
  The schema already has `food/meal_delivery` subtype and one seed
  entry (Ypsilanti Meals on Wheels), so it's not a blocker.

- **WIC clinic locator** — probed 2026-05-06. signupwic.com is a Craft
  CMS site using HTMX/Sprig — every clinic-list render requires an
  encoded `sprig:config` token per session. No bulk export. Federal
  WIC defers locator data to state WIC programs; each state runs its
  own website (Michigan, Texas, etc.) with no consistent data format.
  Building real coverage = 50 state-specific scrapers.

  **Decision:** Defer. Could potentially bootstrap from the
  `food/wic_clinic` subtype using a single state at a time as
  contributors become available. Federal-level scraping is not viable.

- **SAMHSA Behavioral Health Treatment Locator (findtreatment.gov)** —
  probed 2026-05-06. The locator listing API at /locator/listing is a
  POST endpoint that requires a Google reCAPTCHA token per request
  (intentional gate against automated scraping). The underlying
  N-SUMHSS dataset is published by SAMHSA but access requires a manual
  data-request workflow, not a public download.

  **Decision:** Defer; consider negotiating dataset access via official
  SAMHSA channels if mental-health coverage becomes a priority.

## Pattern observation (2026-05-06)

Three "high-priority" tier-1 sources we tried in this round (Meals on
Wheels, WIC, SAMHSA) all turned out to be unbuildable as automated
scrapers — either captcha-gated, federated to state programs without
unified APIs, or behind manual data-request workflows. The earlier
seven sources we shipped were anomalies: federal datasets that *did*
publish bulk-downloadable data because they were designed for transit
to other systems (HRSA federal funding programs, USDA SNAP retailer
locator).

For new high-quality sources we should look for:
- Datasets explicitly published "open data" by their owner (data.gov,
  agency open-data portals)
- Standardized exchange formats (HL7 FHIR for healthcare, OpenReferral
  HSDS for human services)
- 211 state programs that publish openly (Texas, CT, WA do — most
  others don't)
- Member-of-network directories from federated nonprofits (Catholic
  Charities, YMCA) where the parent maintains a member list

## Known issues to address later

- **NDBN entries have `00000` zip in filenames.** Nominatim returns lat/lng but no ZCTA, so the writer falls back to `00000`. Cosmetic; entries still render correctly on the map (they have valid coords). Fix: reverse-geocode Nominatim coords through Census to derive ZCTA, then update the entries.
- **Salvation Army `community_support` is a catch-all subtype.** Each corps offers a varying mix of services (food/shelter/youth/etc.) — the `services` list captures these but the petal/subtype model collapses them all under `care`. Could split into multiple petals per location later if needed.
- **TLA + FNB entries have empty addresses** (form-submitted maps where contributors only dropped a pin). Reverse-geocoded to city/state/zip but no street. Fine for the directory; community PRs can add real addresses over time.

## Out of scope (for now)

- **211 / iCarol** — licensing per-state, complicated, deferred from spec
- **Feeding America Food Bank Finder** — Phase 2 in original spec, not yet probed
- **OpenStreetMap Overpass** — Phase 2; deferred until we need housing/social-services coverage that current sources don't provide
- **HUD shelters** — confirmed not buildable from public data (see `probes/FINDINGS.md`)
- **VA facilities** — needs API key; deferred to v1.5

## Conventions for adding to this file

When you add a candidate:
1. Note the petal(s) it'd populate.
2. Capture the access path (API / scrape / manual / form-only).
3. Flag any defensibility concern up front — we don't want to discover
   that "X charity directory" is half-political-PAC after we've integrated.
4. Estimated record count if you can guess.

When a candidate ships, move it to the top table with a row.
