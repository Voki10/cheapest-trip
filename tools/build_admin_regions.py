"""Build cheaptrip/data/admin_regions.json: states / provinces / oblasts with their airports.

Sources (both open data):
  * OurAirports (public domain) — airports.csv gives each airport's ISO 3166-2 region, regions.csv
    the regions' English names;
  * Wikidata — Russian names of the same regions, matched by ISO 3166-2 code (property P300).
Only regions that have at least one airport with an IATA code and scheduled service are kept.

    python tools/build_admin_regions.py
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "cheaptrip" / "data" / "admin_regions.json"
AIRPORTS_CSV = "https://davidmegginson.github.io/ourairports-data/airports.csv"
REGIONS_CSV = "https://davidmegginson.github.io/ourairports-data/regions.csv"
WIKIDATA = "https://query.wikidata.org/sparql"
QUERY = """
SELECT ?code ?ru ?en WHERE {
  ?r wdt:P300 ?code .
  OPTIONAL { ?r rdfs:label ?ru FILTER(LANG(?ru) = "ru") }
  OPTIONAL { ?r rdfs:label ?en FILTER(LANG(?en) = "en") }
}
"""
UA = {"User-Agent": "cheaptrip/1.0 (admin regions build; python-httpx)"}


def main() -> None:
    with httpx.Client(timeout=120, headers=UA, follow_redirects=True) as c:
        airports = list(csv.DictReader(io.StringIO(c.get(AIRPORTS_CSV).text)))
        regions = {r["code"]: r for r in csv.DictReader(io.StringIO(c.get(REGIONS_CSV).text))}
        rows = c.get(WIKIDATA, params={"query": QUERY, "format": "json"}).json()["results"]["bindings"]
    ru_names: dict[str, str] = {}
    for b in rows:
        code = b["code"]["value"]
        if "ru" in b and code not in ru_names:
            ru_names[code] = b["ru"]["value"]

    by_region: dict[str, list[str]] = {}
    for a in airports:
        if a["iata_code"] and a["scheduled_service"] == "yes" and a["iso_region"] in regions:
            by_region.setdefault(a["iso_region"], []).append(a["iata_code"])

    out = {}
    for code, iatas in sorted(by_region.items()):
        r = regions[code]
        if r["local_code"] == "U-A":  # "unassigned" pseudo-regions
            continue
        out[code] = {"en": r["name"], "ru": ru_names.get(code), "country": r["iso_country"],
                     "airports": sorted(set(iatas))}
    OUT.write_text(json.dumps({
        "source": "OurAirports (airports.csv, regions.csv; public domain) + Wikidata P300 Russian labels",
        "regions": out,
    }, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    with_ru = sum(1 for v in out.values() if v["ru"])
    print(f"{len(out)} regions with scheduled IATA airports ({with_ru} with Russian names) → {OUT}")


if __name__ == "__main__":
    main()
