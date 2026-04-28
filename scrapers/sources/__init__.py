"""Source scrapers — one module per data source.

Each source module exposes a `fetch()` function that returns an iterable of
KB-shape dicts (matching scrapers.kb.FIELD_ORDER). Sources should:

1. Be runnable as a script: `python -m scrapers.sources.federal.X`
2. Log progress to stdout/stderr.
3. Raise on hard failures so the orchestrator can catch and continue.
4. Set `geocoded_by` to "source" if lat/lng comes from the source itself.
5. Set `source` to a value in scrapers.kb.VALID_SOURCES.
"""
