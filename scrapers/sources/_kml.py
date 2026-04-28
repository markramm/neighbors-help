"""Shared helper for sources that publish via Google My Maps KML.

Google My Maps exposes the underlying data as KML at:
  https://www.google.com/maps/d/kml?mid={MAP_ID}&forcekml=1

Each placemark in that KML has:
  - <name>          (display name)
  - <description>   (free text — sometimes a contact / hours / address blob)
  - <coordinates>   (lng,lat[,alt])
  - optional <ExtendedData><Data name="...">  (form-field data)

Real-world data quality is variable — many maps are pin-on-map only,
with no address text. For those, callers must reverse-geocode the
coordinates to derive state/zip/city.
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Iterator, Optional

from scrapers.sources._http import get_with_retry

log = logging.getLogger(__name__)

KML_NS = {"kml": "http://www.opengis.net/kml/2.2"}


@dataclass
class KmlPlacemark:
    name: str
    description: str
    lat: float
    lng: float
    extended: dict[str, str]   # ExtendedData fields, name -> value


def fetch_my_maps_kml(map_id: str, timeout: float = 60) -> bytes:
    """Fetch the raw KML for a Google My Maps id."""
    url = f"https://www.google.com/maps/d/kml?mid={map_id}&forcekml=1"
    log.info("fetching Google My Maps KML: %s", url)
    r = get_with_retry(url, timeout=timeout)
    log.info("downloaded %d bytes", len(r.content))
    return r.content


def parse_placemarks(kml_bytes: bytes) -> Iterator[KmlPlacemark]:
    """Yield placemarks from a KML document. Skips placemarks without
    parseable coordinates."""
    root = ET.fromstring(kml_bytes)
    for pm in root.iter("{http://www.opengis.net/kml/2.2}Placemark"):
        name_el = pm.find("kml:name", KML_NS)
        desc_el = pm.find("kml:description", KML_NS)
        coords_el = pm.find(".//kml:coordinates", KML_NS)
        if coords_el is None or not coords_el.text:
            continue
        # KML coords are "lng,lat[,alt]" possibly with whitespace
        first_pt = coords_el.text.strip().split()[0]
        try:
            parts = [p.strip() for p in first_pt.split(",")]
            lng = float(parts[0])
            lat = float(parts[1])
        except (ValueError, IndexError):
            continue

        extended: dict[str, str] = {}
        for d in pm.findall(".//kml:Data", KML_NS):
            name_attr = d.get("name") or ""
            v = d.findtext("kml:value", default="", namespaces=KML_NS) or ""
            extended[name_attr] = v.strip()

        yield KmlPlacemark(
            name=(name_el.text if name_el is not None else "").strip(),
            description=(desc_el.text if desc_el is not None else "").strip(),
            lat=lat, lng=lng,
            extended=extended,
        )


def is_us_coord(lat: float, lng: float) -> bool:
    """Cheap continental-US + AK + HI bounding box check.

    Used to skip non-US placemarks from international datasets without
    calling reverse geocode (which would waste a Census request).
    """
    # Continental US
    if 24.0 <= lat <= 50.0 and -125.0 <= lng <= -66.0:
        return True
    # Alaska
    if 51.0 <= lat <= 72.0 and -180.0 <= lng <= -129.0:
        return True
    # Hawaii
    if 18.5 <= lat <= 22.5 and -161.0 <= lng <= -154.0:
        return True
    # Puerto Rico / USVI
    if 17.5 <= lat <= 18.6 and -67.5 <= lng <= -64.5:
        return True
    return False
