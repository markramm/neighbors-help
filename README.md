# neighbors-help

Static directory site for community resources — food, shelter, medical care, and other neighborhood support. Run by Transparency Cascade Press LLC.

Live at **[neighbors-help.org](https://neighbors-help.org)**.

## What this is

A neighborhood resource finder. Zip-code search, filterable map, color-coded by resource type. Seeded from public data, maintained by community contribution. No accounts, no tracking, no dynamic backend — fully static.

## Repo layout

```
.
├── site/         # Hugo site (layouts, static assets, config)
├── kb/           # Pyrite knowledge base — one markdown file per resource
│   ├── resources/{food,medical,housing,safety,care,economy,education}/
│   ├── geo/      # zip boundaries, coverage data
│   └── _schema.yaml
├── scripts/      # Scrapers, geocoder, validators
└── .github/      # Workflows + issue templates
```

The KB and the site live in the same repo for now. The KB can be split into a separate public repo later if anyone wants to fork just the data.

## Adding a resource

Open an issue with the **"Add a resource"** template. A maintainer will verify and merge.

## Local development

```sh
cd site
hugo server -D
```

## Build

```sh
cd site
hugo --minify
```

Output goes to `site/public/`.

## License

Code: MIT. Data (`kb/`): CC0 — public-domain dedication so anyone can fork, mirror, or redistribute.
