"""Places: cities, airports, countries and regions from Travelpayouts reference data + UN M49 regions.

Nothing here is a list of "allowed destinations". Regions are the UN M49 standard; cities and
airports come from the provider's reference data, and origin cities are ranked by how many real
routes leave their airports (routes.json), not by a hand-picked popularity list.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import httpx

from .config import CACHE_DIR, DATA_DIR
from .i18n import tr

log = logging.getLogger(__name__)

REF_URL = "https://api.travelpayouts.com/data/{name}"
REF_FILES = {
    "cities": "ru/cities.json",
    "airports": "ru/airports.json",
    "countries": "ru/countries.json",
    "routes": "routes.json",
}
REF_MAX_AGE = 7 * 24 * 3600


def normalize(text: str) -> str:
    text = text.lower().replace("ё", "е").strip()
    text = re.sub(r"[\"'«»().,!?:;]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def flag(country_code: str) -> str:
    if len(country_code) != 2 or not country_code.isalpha():
        return "🏳️"
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in country_code.upper())


# ── Regions (UN M49) ─────────────────────────────────────────────────────────
@dataclass(frozen=True)
class RegionDef:
    key: str
    label: str
    label_en: str
    field: str  # "region" | "subregion" | "intermediate"
    values: tuple[str, ...]
    aliases: tuple[str, ...]


REGIONS: tuple[RegionDef, ...] = (
    RegionDef("europe", "Европа", "Europe", "region", ("Europe",),
              ("europe", "европа", "европы", "европе", "европу", "европой", "евросоюз")),
    RegionDef("asia", "Азия", "Asia", "region", ("Asia",),
              ("asia", "азия", "азии", "азию", "азией")),
    RegionDef("africa", "Африка", "Africa", "region", ("Africa",),
              ("africa", "африка", "африки", "африке", "африку", "африкой")),
    RegionDef("oceania", "Океания", "Oceania", "region", ("Oceania",),
              ("oceania", "океания", "океании", "океанию", "австралия и океания")),
    RegionDef("americas", "Америка", "Americas", "region", ("Americas",),
              ("americas", "america", "америка", "америки", "америке", "америку")),
    RegionDef("north_america", "Северная Америка", "North America", "subregion+intermediate",
              ("Northern America", "Central America", "Caribbean"),
              ("north america", "северная америка", "северной америки", "северную америку",
               "северной америке")),
    RegionDef("south_america", "Южная Америка", "South America", "intermediate", ("South America",),
              ("south america", "южная америка", "южной америки", "южную америку", "южной америке")),
    RegionDef("latin_america", "Латинская Америка", "Latin America", "subregion", ("Latin America and the Caribbean",),
              ("latin america", "латинская америка", "латинской америки", "латинскую америку",
               "латинской америке")),
    RegionDef("caribbean", "Карибы", "Caribbean", "intermediate", ("Caribbean",),
              ("caribbean", "карибы", "карибов", "карибах", "карибские острова", "карибское море")),
    RegionDef("se_asia", "Юго-Восточная Азия", "Southeast Asia", "subregion", ("South-eastern Asia",),
              ("southeast asia", "south-east asia", "юго-восточная азия", "юго-восточной азии",
               "юго-восточную азию", "юва")),
    RegionDef("east_asia", "Восточная Азия", "East Asia", "subregion", ("Eastern Asia",),
              ("east asia", "восточная азия", "восточной азии", "восточную азию")),
    RegionDef("south_asia", "Южная Азия", "South Asia", "subregion", ("Southern Asia",),
              ("south asia", "южная азия", "южной азии", "южную азию")),
    RegionDef("central_asia", "Центральная Азия", "Central Asia", "subregion", ("Central Asia",),
              ("central asia", "центральная азия", "центральной азии", "центральную азию",
               "средняя азия", "средней азии", "среднюю азию")),
    RegionDef("western_asia", "Западная Азия (Ближний Восток и Закавказье)", "Western Asia (Middle East and South Caucasus)", "subregion", ("Western Asia",),
              ("western asia", "middle east", "западная азия", "западной азии", "западную азию",
               "ближний восток", "ближнего востока", "ближнем востоке")),
    RegionDef("western_europe", "Западная Европа", "Western Europe", "subregion", ("Western Europe",),
              ("western europe", "западная европа", "западной европы", "западную европу",
               "западной европе")),
    RegionDef("eastern_europe", "Восточная Европа", "Eastern Europe", "subregion", ("Eastern Europe",),
              ("eastern europe", "восточная европа", "восточной европы", "восточную европу",
               "восточной европе")),
    RegionDef("southern_europe", "Южная Европа", "Southern Europe", "subregion", ("Southern Europe",),
              ("southern europe", "южная европа", "южной европы", "южную европу", "южной европе")),
    RegionDef("northern_europe", "Северная Европа", "Northern Europe", "subregion", ("Northern Europe",),
              ("northern europe", "северная европа", "северной европы", "северную европу",
               "северной европе", "скандинавия", "скандинавии", "скандинавию")),
)

@dataclass(frozen=True)
class SubRegionDef:
    """Part of one country, cut by longitude (the only geography the reference data carries)."""
    key: str
    label: str
    label_en: str
    note: str
    note_en: str
    country: str
    lon_min: float | None
    lon_max: float | None
    aliases: tuple[str, ...]


# The Urals run close to 60° E: Perm, Ufa, Orenburg lie west of it, Yekaterinburg, Chelyabinsk east.
SUBREGIONS: tuple[SubRegionDef, ...] = (
    SubRegionDef("ru_europe", "Европейская часть России", "European Russia",
                 "западнее Урала — города с долготой < 60° в. д.", "west of the Urals — cities below 60° E",
                 "RU", None, 60.0,
                 ("european russia", "европейская часть россии", "европейской части россии",
                  "европейскую часть россии", "европейская часть рф", "европейской части рф",
                  "европейскую часть рф", "европейская часть", "европейской части", "европейскую часть",
                  "европейская россия", "европейской россии", "европейскую россию")),
    SubRegionDef("ru_asia", "Азиатская часть России", "Asian Russia",
                 "восточнее Урала — города с долготой ≥ 60° в. д.", "east of the Urals — cities at 60° E and beyond",
                 "RU", 60.0, None,
                 ("asian russia", "азиатская часть россии", "азиатской части россии", "азиатскую часть россии",
                  "азиатская часть рф", "азиатской части рф", "азиатскую часть рф", "азиатская часть",
                  "азиатской части", "азиатскую часть", "за уралом", "из-за урала", "зауралье", "зауралья")),
)

COUNTRY_ALIASES = {"рф": "RU", "россиюшка": "RU", "сша": "US", "штаты": "US", "штатов": "US", "оаэ": "AE",
                   "эмираты": "AE", "эмиратов": "AE", "эмиратах": "AE",
                   "тай": "TH", "тайланд": "TH", "тайланда": "TH", "тайланде": "TH", "тайланду": "TH",
                   "доминикана": "DO", "доминикану": "DO", "доминикане": "DO", "доминиканы": "DO",
                   "мальдивы": "MV", "мальдивах": "MV", "сейшелы": "SC", "сейшелах": "SC",
                   "вьетнаме": "VN", "турции": "TR", "египте": "EG"}

# Colloquial names and islands that the reference data does not carry as city names.
COLLOQUIAL = {
    "питер": "LED", "питера": "LED", "питере": "LED", "питеру": "LED", "питером": "LED",
    "спб": "LED", "мск": "MOW", "екб": "SVX", "екат": "SVX", "екатеринбурга": "SVX",
    "нск": "OVB", "новосиб": "OVB", "новосиба": "OVB",
    "бали": "DPS", "пхукете": "HKT", "самуи": "USM", "тенерифе": "TCI", "майорка": "PMI",
    "майорку": "PMI", "майорке": "PMI", "мальорка": "PMI",
    "дубаи": "DXB", "дубае": "DXB", "анталия": "AYT", "анталию": "AYT", "анталии": "AYT", "анталией": "AYT",
    "шарм": "SSH", "шарм-эль-шейх": "SSH", "шарм эль шейх": "SSH", "хургаду": "HRG", "паттайю": "UTP",
    "пхукет": "HKT", "пукет": "HKT", "нячанге": "NHA", "фукуок": "PQC", "фукуоке": "PQC",
}

ANYWHERE_ALIASES = {
    "", "anywhere", "any", "everywhere", "*", "куда угодно", "куда-нибудь", "куда нибудь",
    "куда-то", "везде", "любое", "любой", "любую", "весь мир", "мир", "откуда угодно",
    "откуда-нибудь", "где угодно", "неважно", "не важно",
}


# ── Records ──────────────────────────────────────────────────────────────────
@dataclass
class City:
    code: str
    name: str
    name_en: str
    country_code: str
    time_zone: str | None
    lat: float | None
    lon: float | None
    flightable: bool
    routes: int = 0
    airports: list[str] = field(default_factory=list)


@dataclass
class Airport:
    code: str
    name: str
    name_en: str
    city_code: str
    country_code: str
    time_zone: str | None


@dataclass
class Country:
    code: str
    name: str
    name_en: str
    region: str | None
    subregion: str | None
    intermediate: str | None


@dataclass
class Place:
    kind: str  # anywhere | region | country | city | airport | multi
    label: str
    code: str | None = None
    country_codes: frozenset[str] | None = None
    city_codes: tuple[str, ...] = ()
    airport: str | None = None
    lon_min: float | None = None  # sub-national regions (European / Asian Russia)
    lon_max: float | None = None
    note: str = ""
    members: tuple["Place", ...] = ()  # kind == "multi": any of these places
    label_en: str = ""
    note_en: str = ""

    def name(self, lang: str = "ru") -> str:
        return self.label_en if lang == "en" and self.label_en else self.label

    @property
    def is_broad(self) -> bool:
        return self.kind in ("anywhere", "region", "country")

    @property
    def region_like(self) -> bool:
        """A whole region or "anywhere" (not an explicit list of countries/cities)."""
        if self.kind == "multi":
            return any(m.region_like for m in self.members)
        return self.kind in ("anywhere", "region")

    @property
    def city_level(self) -> bool:
        if self.kind == "multi":
            return all(m.city_level for m in self.members)
        return self.kind in ("city", "airport")

    def form_value(self, lang: str = "ru") -> str:
        """What to put into the form field so that resolve() gives this place back."""
        if self.kind == "multi":
            return ", ".join(m.form_value(lang) for m in self.members)
        if self.kind == "anywhere":
            return "Anywhere" if lang == "en" else "Куда угодно"
        if self.kind in ("region", "country", "city", "state"):
            return self.name(lang)
        return self.code or self.label

    def describe(self, lang: str = "ru") -> str:
        if self.kind == "multi":
            return tr("place_any_of", lang, places=", ".join(m.describe(lang) for m in self.members))
        if self.kind == "anywhere":
            return tr("anywhere", lang)
        text = tr(f"place_{self.kind}", lang, label=self.name(lang))
        note = self.note_en if lang == "en" and self.note_en else self.note
        return f"{text} ({note})" if note else text


# ── Russian stemming for region names ("в Калифорнию", "из Флориды", "в Краснодарском крае") ──
_ENDINGS = sorted(["ией", "ии", "ию", "ия", "ой", "ою", "ей", "ую", "юю", "ая", "яя", "ое", "ее", "ые", "ие",
                   "ый", "ий", "ого", "его", "ому", "ему", "ом", "ем", "ам", "ям", "ах", "ях",
                   "у", "ю", "а", "я", "е", "ы", "и", "о", "й"], key=len, reverse=True)


def edit_distance(a: str, b: str, limit: int) -> int:
    """Optimal-string-alignment distance (typo, missing/extra letter, swapped pair); > limit = too far."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev2: list[int] = []
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] != b[j - 1]))
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                v = min(v, prev2[j - 2] + 1)
            cur[j] = v
        if min(cur) > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[-1]


def stem_phrase(text: str) -> str:
    words = []
    for w in normalize(text).split():
        for end in _ENDINGS:
            if w.endswith(end) and len(w) - len(end) >= 3:
                w = w[: -len(end)]
                break
        words.append(w)
    return " ".join(words)


# ── Reference data loading ───────────────────────────────────────────────────
def _ref_path(name: str) -> Path:
    return CACHE_DIR / "ref" / REF_FILES[name].replace("/", "_")


def _load_ref(name: str) -> list[dict]:
    path = _ref_path(name)
    fresh = path.exists() and time.time() - path.stat().st_mtime < REF_MAX_AGE
    if not fresh:
        try:
            resp = httpx.get(REF_URL.format(name=REF_FILES[name]), timeout=60,
                             headers={"Accept-Encoding": "gzip, deflate"})
            resp.raise_for_status()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(resp.content)
        except httpx.HTTPError as exc:
            if not path.exists():
                raise RuntimeError(f"cannot download reference data {name}: {exc}") from exc
            log.warning("using stale reference data %s: %s", name, exc)
    return json.loads(path.read_text(encoding="utf-8"))


class Geo:
    def __init__(self) -> None:
        regions = json.loads((DATA_DIR / "un_regions.json").read_text(encoding="utf-8"))["countries"]
        self.countries: dict[str, Country] = {}
        for c in _load_ref("countries"):
            un = regions.get(c["code"], {})
            self.countries[c["code"]] = Country(
                c["code"], c.get("name") or c["code"],
                (c.get("name_translations") or {}).get("en") or c.get("name") or c["code"],
                un.get("region"), un.get("subregion"), un.get("intermediate"),
            )
        self.cities: dict[str, City] = {}
        city_cases: dict[str, dict] = {}
        for c in _load_ref("cities"):
            coords = c.get("coordinates") or {}
            self.cities[c["code"]] = City(
                c["code"], c.get("name") or c["code"],
                (c.get("name_translations") or {}).get("en") or c.get("name") or c["code"],
                c.get("country_code") or "", c.get("time_zone"),
                coords.get("lat"), coords.get("lon"), bool(c.get("has_flightable_airport")),
            )
            city_cases[c["code"]] = c.get("cases") or {}
        self.airports: dict[str, Airport] = {}
        for a in _load_ref("airports"):
            if a.get("iata_type") != "airport" or not a.get("flightable"):
                continue
            self.airports[a["code"]] = Airport(
                a["code"], a.get("name") or a["code"],
                (a.get("name_translations") or {}).get("en") or a.get("name") or a["code"],
                a.get("city_code") or a["code"], a.get("country_code") or "", a.get("time_zone"),
            )
            city = self.cities.get(a.get("city_code") or "")
            if city is not None:
                city.airports.append(a["code"])
        self._rank_cities_by_routes(_load_ref("routes"))
        self._load_states()
        self._build_index(city_cases)

    # ── states / provinces / oblasts (OurAirports ISO 3166-2 + Wikidata names) ──
    def _load_states(self) -> None:
        self.states: dict[str, dict] = {}
        path = DATA_DIR / "admin_regions.json"
        if not path.exists():
            return
        for code, r in json.loads(path.read_text(encoding="utf-8"))["regions"].items():
            cities = {self.airports[a].city_code for a in r["airports"] if a in self.airports}
            cities = {c for c in cities if c in self.cities and self.cities[c].flightable}
            if cities:
                self.states[code] = {"ru": r.get("ru") or r["en"], "en": r["en"], "country": r["country"],
                                     "cities": tuple(sorted(cities))}

    # ── ranking ──────────────────────────────────────────────────────────────
    def _rank_cities_by_routes(self, routes: list[dict]) -> None:
        dests: dict[str, set[str]] = {}
        for r in routes:
            if r.get("codeshare"):
                continue
            dep = self.airports.get(r.get("departure_airport_iata") or "")
            if dep is None:
                continue
            dests.setdefault(dep.city_code, set()).add(r.get("arrival_airport_iata") or "")
        for code, arrivals in dests.items():
            if code in self.cities:
                self.cities[code].routes = len(arrivals)

    # ── name index ───────────────────────────────────────────────────────────
    def _build_index(self, city_cases: dict[str, dict]) -> None:
        self.index: dict[str, list[tuple[str, str]]] = {}

        def add(key: str, kind: str, code: str) -> None:
            key = normalize(key)
            for prep in ("в ", "во ", "на "):  # accusative case forms come with their preposition
                if key.startswith(prep):
                    key = key[len(prep):]
                    break
            if key and (kind, code) not in self.index.setdefault(key, []):
                self.index[key].append((kind, code))

        for r in REGIONS:
            for alias in (r.label, r.label_en, *r.aliases):
                add(alias, "region", r.key)
        for s in SUBREGIONS:
            for alias in (s.label, s.label_en, *s.aliases):
                add(alias, "subregion", s.key)
        for alias, code in COUNTRY_ALIASES.items():
            if code in self.countries:
                add(alias, "country", code)
        country_data = _load_ref("countries")
        for c in country_data:
            for name in (c.get("name"), (c.get("name_translations") or {}).get("en"),
                         *(c.get("cases") or {}).values()):
                if name:
                    add(name, "country", c["code"])
        for code, city in self.cities.items():
            if not city.flightable:
                continue
            for name in (city.name, city.name_en, *city_cases.get(code, {}).values()):
                if name:
                    add(name, "city", code)
        for code, ap in self.airports.items():
            for name in (ap.name, ap.name_en):
                if name:
                    add(name, "airport", code)
        for alias, code in COLLOQUIAL.items():
            if code in self.cities:  # a city beats a same-named airport, so this is safe to add
                add(alias, "city", code)
        # States have no case forms in the data: they are matched by stem as well
        # ("Калифорнию" → "калифорн" = "Калифорния").
        self.state_stems: dict[str, list[str]] = {}
        for code, s in self.states.items():
            for name in {s["ru"], s["en"]}:
                add(name, "state", code)
                stem = stem_phrase(name)
                if len(stem.replace(" ", "")) >= 5 and code not in self.state_stems.setdefault(stem, []):
                    self.state_stems[stem].append(code)
        self.max_phrase_words = max(len(k.split()) for k in self.index)
        # Typo candidates: only names worth travelling to, bucketed by first letter.
        self._typo_buckets: dict[str, list[tuple[str, list]]] = {}
        for key, hits in self.index.items():
            worthy = [h for h in hits if self._typo_worthy(*h)]
            if worthy and len(key.replace(" ", "")) >= 5 and len(key.split()) <= 2:
                self._typo_buckets.setdefault(key[0], []).append((key, worthy))

    def _typo_worthy(self, kind: str, code: str) -> bool:
        if kind in ("region", "subregion", "country", "state"):
            return True
        return kind == "city" and self.cities[code].routes >= 3

    def lookup_typo(self, phrase: str) -> tuple[Place, str] | None:
        """Closest known place name within 1 typo (2 for names of 8+ letters): «Пхует» → Пхукет.
        Returns (place, the name it was matched to) or None."""
        p = normalize(phrase)
        n = len(p.replace(" ", ""))
        if n < 5:
            return None
        limit = 1 if n < 8 else 2
        best = None
        for key, hits in self._typo_buckets.get(p[0], ()):
            d = edit_distance(p, key, limit)
            if d > limit:
                continue
            place = self._place_from_hits(hits)
            weight = self.cities[place.code].routes if place.kind == "city" else 10_000
            if best is None or (d, -weight) < best[0]:
                best = ((d, -weight), place, key)
        return (best[1], best[2]) if best else None

    def lookup(self, phrase: str, fuzzy: bool = False) -> Place | None:
        """Name lookup: exact (normalized) first; with fuzzy=True also states by Russian stem."""
        hits = self.index.get(normalize(phrase))
        if hits:
            return self._place_from_hits(hits)
        if fuzzy:
            codes = self.state_stems.get(stem_phrase(phrase))
            if codes:
                return self._place_from_hits([("state", c) for c in codes])
        return None

    # A big city beats a same-named state ("Москва", "Вашингтон", "Нью-Йорк"); a small one does not.
    BIG_CITY_ROUTES = 20

    def _place_from_hits(self, hits: list[tuple[str, str]]) -> Place:
        # Broader wins on exact ties (country "Сингапур" contains city "Сингапур").
        order = {"region": 0, "subregion": 0, "country": 1, "state": 2, "city": 3, "airport": 4}
        kinds = sorted({k for k, _ in hits}, key=order.__getitem__)
        kind = kinds[0]
        if kind == "state" and "city" in kinds:
            if max(self.cities[c].routes for k, c in hits if k == "city") >= self.BIG_CITY_ROUTES:
                kind = "city"
        codes = [c for k, c in hits if k == kind]
        if kind == "city":
            codes.sort(key=lambda c: -self.cities[c].routes)
        if kind == "state":  # same name in several countries: the one with more connected airports
            codes.sort(key=lambda c: -sum(self.cities[x].routes for x in self.states[c]["cities"]))
        return self._make(kind, codes[0])

    def _make(self, kind: str, code: str) -> Place:
        if kind == "region":
            r = next(r for r in REGIONS if r.key == code)
            return Place("region", r.label, r.key, frozenset(self._region_countries(r)), label_en=r.label_en)
        if kind == "subregion":
            s = next(s for s in SUBREGIONS if s.key == code)
            return Place("region", s.label, s.key, frozenset({s.country}), lon_min=s.lon_min,
                         lon_max=s.lon_max, note=s.note, label_en=s.label_en, note_en=s.note_en)
        if kind == "country":
            c = self.countries[code]
            return Place("country", c.name, code, frozenset({code}), label_en=c.name_en)
        if kind == "state":
            s = self.states[code]
            country = self.countries.get(s["country"])
            return Place("state", s["ru"], code, city_codes=s["cities"], label_en=s["en"],
                         note=country.name if country else s["country"],
                         note_en=country.name_en if country else s["country"])
        if kind == "city":
            c = self.cities[code]
            return Place("city", c.name, code, city_codes=(code,), label_en=c.name_en)
        ap = self.airports[code]
        return Place("airport", f"{ap.name} ({code})", code, city_codes=(ap.city_code,), airport=code,
                     label_en=f"{ap.name_en} ({code})")

    def _region_countries(self, r: RegionDef) -> set[str]:
        out = set()
        for c in self.countries.values():
            fields = r.field.split("+")
            vals = {"region": c.region, "subregion": c.subregion, "intermediate": c.intermediate}
            if any(vals[f] in r.values for f in fields):
                out.add(c.code)
        return out

    # ── resolution of form input ─────────────────────────────────────────────
    def resolve_one(self, text: str | None) -> Place | None:
        raw = (text or "").strip()
        if normalize(raw) in ANYWHERE_ALIASES:
            return Place("anywhere", "Anywhere")
        up = raw.upper()
        if re.fullmatch(r"[A-Z]{3}", up):
            if up in self.cities and self.cities[up].flightable:
                return self._make("city", up)
            if up in self.airports:
                return self._make("airport", up)
        if re.fullmatch(r"[A-Z]{2}", up) and up in self.countries:
            return self._make("country", up)
        if re.fullmatch(r"[A-Z]{2}-[A-Z0-9]{1,3}", up) and up in self.states:  # ISO 3166-2, e.g. US-CA
            return self._make("state", up)
        found = self.lookup(raw, fuzzy=True)
        if found is None and (typo := self.lookup_typo(raw)):
            found = typo[0]  # the search summary shows the corrected name
        return found

    def resolve_many(self, text: str | None) -> tuple[Place | None, list[str]]:
        """One place or a list ("Москва, Казань или Питер"). Returns (place, names not understood)."""
        whole = self.resolve_one(text)
        if whole is not None:
            return whole, []
        places, unknown = [], []
        for part in re.split(r"[,;/]", text or ""):
            part = part.strip()
            if not part:
                continue
            one = self.resolve_one(part)
            if one is None:  # "Москва или Казань" inside one comma-separated part
                for sub in re.split(r"\s+(?:или|и|либо|or|and)\s+", part):
                    sub_place = self.resolve_one(sub)
                    if sub_place is None:
                        unknown.append(sub.strip())
                    else:
                        places.append(sub_place)
            else:
                places.append(one)
        return self.combine(places), unknown

    def resolve(self, text: str | None) -> Place | None:
        place, unknown = self.resolve_many(text)
        return None if unknown else place

    @staticmethod
    def combine(places: list[Place]) -> Place | None:
        unique: list[Place] = []
        for p in places:
            if p.kind == "anywhere":
                return p
            if not any((p.kind, p.code) == (u.kind, u.code) for u in unique):
                unique.append(p)
        if not unique:
            return None
        if len(unique) == 1:
            return unique[0]
        return Place("multi", ", ".join(p.label for p in unique), members=tuple(unique),
                     label_en=", ".join(p.name("en") for p in unique))

    # ── scope helpers ────────────────────────────────────────────────────────
    def contains(self, place: Place, city_code: str) -> bool:
        if place.kind == "anywhere":
            return True
        if place.kind == "multi":
            return any(self.contains(m, city_code) for m in place.members)
        if place.country_codes is not None:
            city = self.cities.get(city_code)
            if city is None or city.country_code not in place.country_codes:
                return False
            if place.lon_min is not None and (city.lon is None or city.lon < place.lon_min):
                return False
            if place.lon_max is not None and (city.lon is None or city.lon >= place.lon_max):
                return False
            return True
        return city_code in place.city_codes

    def cities_in(self, place: Place) -> list[str]:
        """Flightable cities in scope, most connected first (by real routes)."""
        if place.kind in ("city", "airport"):
            return list(place.city_codes)
        if place.kind == "state":  # every city with an airport there, best connected first
            return sorted(place.city_codes, key=lambda c: -self.cities[c].routes)
        if place.city_level:  # list of cities: keep them all, best connected first
            codes = list(dict.fromkeys(c for m in place.members for c in m.city_codes))
            return sorted(codes, key=lambda c: -(self.cities[c].routes if c in self.cities else 0))
        out = [c for c in self.cities.values()
               if c.flightable and c.routes > 0 and self.contains(place, c.code)]
        out.sort(key=lambda c: -c.routes)
        return [c.code for c in out]

    def country_codes_of(self, place: Place) -> set[str]:
        if place.country_codes is not None:
            return set(place.country_codes)
        return {self.cities[c].country_code for c in place.city_codes if c in self.cities}

    def city_name(self, code: str, lang: str = "ru") -> str:
        c = self.cities.get(code)
        if c is None:
            return code
        return c.name_en if lang == "en" and c.name_en else c.name

    def country_name(self, code: str, lang: str = "ru") -> str:
        c = self.countries.get(code)
        if c is None:
            return code
        return c.name_en if lang == "en" and c.name_en else c.name

    def airport_label(self, code: str, lang: str = "ru") -> str:
        ap = self.airports.get(code)
        if ap is None:
            return code
        return f"{ap.name_en if lang == 'en' and ap.name_en else ap.name} — {code}"

    def tz_of(self, airport_or_city: str) -> str | None:
        ap = self.airports.get(airport_or_city)
        if ap and ap.time_zone:
            return ap.time_zone
        city = self.cities.get(airport_or_city)
        return city.time_zone if city else None

    def region_label(self, country_code: str) -> str | None:
        c = self.countries.get(country_code)
        return c.region if c else None

    def suggest(self, prefix: str, limit: int = 8, lang: str = "ru") -> list[dict]:
        p = normalize(prefix)
        if not p:
            return []
        seen, out = set(), []
        for key, hits in self.index.items():
            if not key.startswith(p):
                continue
            place = self._place_from_hits(hits)
            ident = (place.kind, place.code)
            if ident in seen:
                continue
            seen.add(ident)
            weight = {"region": 10_000, "country": 5_000, "state": 3_000}.get(place.kind, 0)
            if place.kind == "city":
                weight = self.cities[place.code].routes
            out.append((weight, {"kind": place.kind, "code": place.code, "label": place.name(lang)}))
        out.sort(key=lambda x: -x[0])
        return [o for _, o in out[:limit]]


@lru_cache(maxsize=1)
def get_geo() -> Geo:
    return Geo()
