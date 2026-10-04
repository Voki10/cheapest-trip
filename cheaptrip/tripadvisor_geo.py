"""City (Travelpayouts IATA code) → TripAdvisor location id, needed for Xotelo hotel lists.

Candidate ids come from, in order:
  1. local cache of already verified ids;
  2. seed file data/tripadvisor_geo_seed.json (ids for popular destinations);
  3. Wikidata: the city served by the city's airports, then administrative units near its centre;
  4. Xotelo /search via RapidAPI, when RAPIDAPI_KEY is set.
A candidate is accepted only after verification against Xotelo itself: its hotel list must exist
and its hotels must lie near the city's coordinates. Nothing is assumed from a name alone.
"""

from __future__ import annotations

import json
import math
import statistics
import time
from pathlib import Path

from .config import CACHE_DIR, DATA_DIR
from .geo import Geo
from .http import HttpClient, HttpError

WIKIDATA_URL = "https://query.wikidata.org/sparql"
XOTELO_LIST = "https://data.xotelo.com/api/list"
RAPID_SEARCH = "https://xotelo-hotel-prices.p.rapidapi.com/api/search"
RAPID_HOST = "xotelo-hotel-prices.p.rapidapi.com"
MAX_MEDIAN_KM = 50.0
MIN_HOTELS = 5
NEGATIVE_TTL = 7 * 24 * 3600
LIST_CACHE_SECONDS = 24 * 3600

CACHE_FILE = CACHE_DIR / "tripadvisor_geo.json"
SEED_FILE = DATA_DIR / "tripadvisor_geo_seed.json"


def km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


class TripAdvisorLocator:
    def __init__(self, http: HttpClient, geo: Geo, rapidapi_key: str = ""):
        self.http = http
        self.geo = geo
        self.rapidapi_key = rapidapi_key
        self.cache = _load(CACHE_FILE)
        self.seed = _load(SEED_FILE).get("cities", {})

    def _save(self) -> None:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(self.cache, ensure_ascii=False, indent=1), encoding="utf-8")

    def cached(self, city: str) -> str | None:
        entry = self.cache.get(city) or {}
        return entry.get("geo")

    async def location_for(self, city: str) -> tuple[str | None, str]:
        """Returns (TripAdvisor geo id or None, how it was found / why not)."""
        entry = self.cache.get(city)
        if entry and entry.get("geo"):
            return entry["geo"], entry.get("source", "cache")
        if entry and time.time() - entry.get("checked_at", 0) < NEGATIVE_TTL:
            return None, entry.get("reason_key", "not_found")
        c = self.geo.cities.get(city)
        if c is None or c.lat is None or c.lon is None:
            return None, "no_coords"
        tried: list[str] = []
        sources = [("справочник", self._from_seed), ("Wikidata: аэропорты", self._from_airports),
                   ("Wikidata: рядом с центром", self._from_nearby)]
        if self.rapidapi_key:
            sources.append(("Xotelo search (RapidAPI)", self._from_rapidapi))
        for source, fn in sources:
            try:
                candidates = await fn(city)
            except HttpError:
                continue
            for cand in candidates:
                if cand in tried:
                    continue
                tried.append(cand)
                if await self._verify(cand, c.lat, c.lon):
                    self.cache[city] = {"geo": cand, "source": source, "checked_at": time.time()}
                    self._save()
                    return cand, source
        self.cache[city] = {"geo": None, "reason_key": "not_found", "checked_at": time.time()}
        self._save()
        return None, "not_found"

    # ── candidate sources ────────────────────────────────────────────────────
    async def _from_seed(self, city: str) -> list[str]:
        value = self.seed.get(city)
        return [str(value)] if value else []

    async def _sparql(self, query: str) -> list[dict]:
        fetched = await self.http.get_json(WIKIDATA_URL, {"query": query, "format": "json"},
                                           cache_seconds=30 * 24 * 3600)
        return ((fetched.data or {}).get("results") or {}).get("bindings") or []

    async def _from_airports(self, city: str) -> list[str]:
        airports = self.geo.cities[city].airports or [city]
        values = " ".join(f'"{a}"' for a in airports)
        rows = await self._sparql(
            f"SELECT DISTINCT ?ta WHERE {{ VALUES ?code {{ {values} }} "
            f"?airport wdt:P238 ?code ; wdt:P931 ?place . ?place wdt:P3134 ?ta . "
            f'FILTER(REGEX(STR(?ta), "^[0-9]+$")) }}')
        return [r["ta"]["value"] for r in rows]

    async def _from_nearby(self, city: str) -> list[str]:
        c = self.geo.cities[city]
        rows = await self._sparql(
            "SELECT DISTINCT ?ta ?pop ?dist WHERE { "
            "SERVICE wikibase:around { ?item wdt:P625 ?loc . "
            f'bd:serviceParam wikibase:center "Point({c.lon} {c.lat})"^^geo:wktLiteral ; '
            'wikibase:radius "25" ; wikibase:distance ?dist . } '
            '?item wdt:P3134 ?ta . FILTER(REGEX(STR(?ta), "^[0-9]+$")) '
            "?item wdt:P31 ?type . ?type wdt:P279* wd:Q56061 . "
            "OPTIONAL { ?item wdt:P1082 ?pop } } ORDER BY DESC(?pop) ?dist LIMIT 5")
        return [r["ta"]["value"] for r in rows]

    async def _from_rapidapi(self, city: str) -> list[str]:
        c = self.geo.cities[city]
        fetched = await self.http.get_json(
            RAPID_SEARCH, {"query": c.name_en, "location_type": "geo"},
            headers={"x-rapidapi-key": self.rapidapi_key, "x-rapidapi-host": RAPID_HOST},
            cache_seconds=30 * 24 * 3600)
        items = ((fetched.data or {}).get("result") or {}).get("list") or []
        out = []
        for it in items:
            key = str(it.get("location_key") or it.get("hotel_key") or "")
            if key.startswith("g"):
                out.append(key[1:].split("-")[0])
        return out

    # ── verification ─────────────────────────────────────────────────────────
    async def _verify(self, geo_id: str, lat: float, lon: float) -> bool:
        try:
            fetched = await self.http.get_json(
                XOTELO_LIST, {"location_key": f"g{geo_id}", "limit": 30, "offset": 0, "sort": "best_value"},
                cache_seconds=LIST_CACHE_SECONDS)
        except HttpError:
            return False
        hotels = ((fetched.data or {}).get("result") or {}).get("list") or []
        dists = [km(lat, lon, h["geo"]["latitude"], h["geo"]["longitude"])
                 for h in hotels if (h.get("geo") or {}).get("latitude") is not None]
        return len(dists) >= MIN_HOTELS and statistics.median(dists) <= MAX_MEDIAN_KM
