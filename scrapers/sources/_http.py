"""Shared HTTP helpers for source scrapers."""
from __future__ import annotations

import logging
import time
from typing import Optional

import requests

log = logging.getLogger(__name__)

USER_AGENT = "neighbors-help-scraper/0.1 (+https://neighbors-help.org)"
DEFAULT_TIMEOUT = 60


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def get_with_retry(
    url: str,
    *,
    params: Optional[dict] = None,
    sess: Optional[requests.Session] = None,
    retries: int = 3,
    backoff_s: float = 2.0,
    timeout: float = DEFAULT_TIMEOUT,
) -> requests.Response:
    """GET with bounded exponential backoff. Raises on persistent failure."""
    s = sess or session()
    last_err: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            r = s.get(url, params=params, timeout=timeout)
            if r.status_code in (429, 500, 502, 503, 504):
                wait = backoff_s * (2 ** attempt)
                log.warning("status %d on %s, retry %d in %.1fs", r.status_code, url, attempt + 1, wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last_err = e
            wait = backoff_s * (2 ** attempt)
            log.warning("error on %s (%s), retry %d in %.1fs", url, e, attempt + 1, wait)
            time.sleep(wait)
    raise RuntimeError(f"giving up on {url}: {last_err}")


def normalize_phone(raw: str) -> str:
    """Format phone as (xxx) xxx-xxxx if it parses; otherwise return cleaned input."""
    if not raw:
        return ""
    digits = "".join(c for c in str(raw) if c.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return str(raw).strip()


def normalize_website(raw: str) -> str:
    """Ensure scheme; return empty string if obviously invalid."""
    if not raw:
        return ""
    s = str(raw).strip()
    if not s or s.lower() in ("none", "n/a", "na", "null", "-"):
        return ""
    if not s.startswith(("http://", "https://")):
        s = "https://" + s
    return s


def normalize_zip(raw) -> str:
    """5-digit zip; strip +4. Empty string if not parseable."""
    if raw is None:
        return ""
    s = str(raw).strip().split("-")[0].split(".")[0]
    return s if (len(s) == 5 and s.isdigit()) else ""
