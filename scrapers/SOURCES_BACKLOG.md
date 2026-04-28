# Source backlog

Candidate data sources for future scrapers, with notes on access and feasibility.
Add to this file as new sources turn up — edit anywhere, anyone can PR. The
top of the file lists shipped sources; everything below is candidate work.

## Shipped

| Source | Coverage | Records (US) | Type | Subtype | Access |
|---|---|---|---|---|---|
| HRSA FQHC | National | ~18,800 | medical | fqhc | Public CSV bulk download |
| USDA SNAP — Farmers and Markets | National | ~7,200 | food | farmers_market | ArcGIS REST FeatureServer |
| Laundry Love | National | ~300 (116 visible US) | care | laundry_assistance | StoreRocket public JSON API |
| Food Not Bombs | National | ~25 US chapters (2025 list) | food | soup_kitchen | Google My Maps KML; reverse-geocode for state/zip |
| Tool Library Alliance | Worldwide → US | ~48 US | economy | tool_library | Google My Maps KML; reverse-geocode for state/zip |

## Candidates — reasonable-to-build

| Source | Petal | Likely access | Risk / notes |
|---|---|---|---|
| **Mutual Aid Hub** (mutualaidhub.org) | economy / food / care | Webflow/Mapbox-rendered map. No documented API; need to inspect what the map JS fetches. Spec'd in original plan. | Many entries are COVID-era inactive — will need a `last_active` filter or community vetting before publishing. |
| **National Diaper Bank Network** (nationaldiaperbanknetwork.org/member-directory) | care | Member directory page; need to inspect for embedded data or AJAX. | Population: families with infants. Strong fit. |
| **Salvation Army** (salvationarmyusa.org/location-finder) | food / housing / care / economy | JS-rendered store locator. Needs DevTools inspection to find the API. ~7,000 US service centers + thrift stores. | Big dataset, services per location vary widely. Probably worth the effort. |

## Candidates — needs investigation before committing

| Source | Petal | Why it's promising | Why I'm cautious |
|---|---|---|---|
| **Organize Directory** (organize.directory/location) | depends on org type | National org directory by location, well-indexed. | "Organizing" framing may pull in electoral/political groups that are out-of-scope per spec ("nothing requiring explanation to a hostile reader"). Need to filter carefully. |
| **Find a Protest** (findaprotest.info/directory) | safety / community | Lists grassroots orgs and "ways to get involved." | Same risk as Organize Directory — political adjacency. Read carefully before pulling in. Returned 403 to my probe. |
| **The County Office** (thecountyoffice.com/charity-non-profit) | mixed | Aggregator of all US nonprofits/charities. Massive. | Aggregator data quality unknown; could include defunct or fraudulent entities. Better to source from IRS Form 990 directly if we want the nonprofit universe (then filter). |

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
