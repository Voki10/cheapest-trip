"""CheapTripSearchService — minimizes TOTAL TRIP COST = outbound + return + hotel + mandatory fees.

Staged search (full brute force of destinations × dates × durations × flights × hotels would burn
the provider's rate limit):

  1 UNDERSTAND   resolve places, dates, trip length, budgets
  2 DISCOVERY    cheapest real one-way fares per destination and month (provider decides where)
  3 CANDIDATES   keep the most promising destinations / origin cities (limits come from config)
  4 ROUTES       full outbound and return calendars per candidate, every routing the provider has
  5 COMBINE      independent outbound + return per nights value, connection checks
  6 HOTELS       hotel search only for surviving (destination, check-in, check-out)
  7 TOTAL        flight + hotel + mandatory fees, converted to the user's currency, budgets
  8 REVALIDATE   re-query the top results bypassing the cache, drop what disappeared
  9 RANK         sort by tripTotal ASC

Nothing is ever generated: every flight, price and link in a result came from a provider answer.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Awaitable, Callable

from .config import Settings
from .fx import Fx, FxUnavailable
from .geo import Geo, Place, flag
from .http import HttpClient
from .i18n import nights_range, norm_lang, stops, tr
from .models import (FlightOption, HotelOffer, HotelSearchResult, SearchReport, SearchStats,
                     TripQuery, TripResult)
from .providers.base import FlightProvider, HotelProvider, ProviderError
from . import weather

ProgressFn = Callable[[dict], None]

NIGHT_ARRIVAL_HOURS = range(0, 5)
EARLY_DEPARTURE_HOURS = range(0, 6)
HOTEL_COMBOS_PER_DESTINATION = 4
DIRECT_PAIRS_MAX = 25  # explicit city lists up to 5 × 5 skip discovery
ALTERNATIVES = 4


class SearchInputError(Exception):
    """The query cannot be understood (unknown place, impossible dates)."""


class SearchAborted(Exception):
    """A provider refused service for the whole search (e.g. invalid token)."""


@dataclass
class Combo:
    origin: str
    dest: str
    out: FlightOption
    ret: FlightOption
    nights: int
    check_in: date
    check_out: date
    warnings: list[str] = field(default_factory=list)

    @property
    def flight_price_src(self) -> float:
        return self.out.price + self.ret.price


def add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    days = [31, 29 if y % 4 == 0 and (y % 100 or y % 400 == 0) else 28,
            31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return date(y, m, min(d.day, days))


def months_between(start: date, end: date) -> list[str]:
    out, cur = [], date(start.year, start.month, 1)
    while cur <= end:
        out.append(f"{cur:%Y-%m}")
        cur = add_months(cur, 1)
    return out


def fmt_money(amount: float | None, currency: str, lang: str = "ru") -> str:
    if amount is None:
        return tr("no_limit", lang)
    sym = {"RUB": "₽", "USD": "$", "EUR": "€"}.get(currency.upper())
    num = f"{amount:,.0f}".replace(",", " ")
    return f"{sym}{num}" if sym else f"{num} {currency.upper()}"


class CheapTripSearchService:
    def __init__(self, geo: Geo, flights: FlightProvider, hotels: HotelProvider, fx: Fx,
                 http: HttpClient, settings: Settings, progress: ProgressFn | None = None,
                 today: date | None = None):
        self.geo = geo
        self.flights = flights
        self.hotels = hotels
        self.fx = fx
        self.http = http
        self.cfg = settings
        self.progress = progress or (lambda _e: None)
        self.today = today or date.today()
        self.warnings: list[str] = []
        self.limits: list[str] = []
        self.stats = SearchStats()
        self._provider_failures = 0
        self._provider_calls = 0
        self._stale = 0
        self._cheapest_over_budget: float | None = None
        self._budget_unverified = 0
        self.lang = "ru"

    def t(self, key: str, **params) -> str:
        return tr(key, self.lang, **params)

    # ── helpers ──────────────────────────────────────────────────────────────
    def _emit(self, stage: str, message: str, done: int | None = None, total: int | None = None):
        self.progress({"stage": stage, "message": message, "done": done, "total": total,
                       "at": datetime.now(timezone.utc).isoformat()})

    async def _run_all(self, stage: str, label: str, coros: list[Awaitable]) -> list:
        """Run provider calls concurrently, report progress, keep going on isolated failures."""
        total = len(coros)
        done = 0
        self._emit(stage, f"{label}: 0/{total}", 0, total)

        async def wrap(c):
            nonlocal done
            try:
                return await c
            except ProviderError as exc:
                self._provider_failures += 1
                if "401" in str(exc):
                    raise SearchAborted(str(exc)) from exc
                return exc
            finally:
                done += 1
                self._provider_calls += 1
                if done == total or done % 5 == 0:
                    self._emit(stage, f"{label}: {done}/{total}", done, total)

        results = await asyncio.gather(*(wrap(c) for c in coros))
        errors = [r for r in results if isinstance(r, Exception)]
        if errors:
            self.warnings.append(self.t("provider_failures", label=label, failed=len(errors), total=total,
                                        error=errors[0]))
        return [r if not isinstance(r, Exception) else [] for r in results]

    def _to_user(self, amount: float, currency: str, user_currency: str) -> float:
        return self.fx.convert(amount, currency, user_currency)

    def _stops_ok(self, f: FlightOption, q: TripQuery) -> bool:
        return q.max_stops is None or f.stops <= q.max_stops

    # ── 1. understand ────────────────────────────────────────────────────────
    def _resolve(self, text: str, what: str) -> Place:
        place, unknown = self.geo.resolve_many(text)
        if unknown or place is None:
            names = ", ".join(f"«{n}»" for n in unknown) or f"«{text}»"
            raise SearchInputError(self.t("not_found", what=self.t(what), names=names))
        return place

    def _understand(self, q: TripQuery) -> dict:
        origin = self._resolve(q.origin, "what_origin")
        dest = self._resolve(q.destination, "what_dest")
        excl_origin = [self._resolve(x, "what_excl_origin") for x in q.exclude_origins if x.strip()]
        excl_dest = [self._resolve(x, "what_excl_dest") for x in q.exclude_destinations if x.strip()]
        tomorrow = self.today + timedelta(days=1)
        start = max(q.date_from or tomorrow, tomorrow)
        months = q.search_months or self.cfg.default_search_months
        end = q.date_to or add_months(self.today, months)
        if end < start:
            raise SearchInputError(self.t("period_empty"))
        if q.nights_min is None and q.nights_max is None:
            nmin, nmax, flexible = self.cfg.flexible_nights_min, self.cfg.flexible_nights_max, True
        else:
            # Only one bound given means "at least N" / "at most N" nights, not "exactly N".
            nmin = q.nights_min if q.nights_min is not None else self.cfg.flexible_nights_min
            nmax = q.nights_max if q.nights_max is not None else self.cfg.flexible_nights_max
            flexible = False
        lo, hi = self.cfg.flexible_nights_min, self.cfg.flexible_nights_max
        if nmin < lo or nmax > hi or nmax < nmin:
            raise SearchInputError(self.t("nights_bounds", lo=lo, hi=hi))
        include_domestic = q.include_domestic
        if include_domestic is None:
            include_domestic = not dest.region_like
        max_age = q.max_price_age_days if q.max_price_age_days is not None else self.cfg.price_max_age_days
        return dict(origin=origin, dest=dest, excl_origin=excl_origin, excl_dest=excl_dest,
                    start=start, end=end, nmin=nmin, nmax=nmax,
                    flexible=flexible, include_domestic=include_domestic,
                    currency=q.currency.upper(), max_age=max(0, max_age))

    def _excluded(self, city: str, places: list[Place]) -> bool:
        return any(self.geo.contains(p, city) for p in places)

    def _fresh(self, f: FlightOption, u: dict) -> bool:
        """Drop prices the provider's users last saw more than max_age days ago."""
        if f.price_seen_on is None:
            return True
        if (self.today - f.price_seen_on).days <= u["max_age"]:
            return True
        self._stale += 1
        return False

    def _interpreted(self, q: TripQuery, u: dict) -> dict:
        cur, lang = u["currency"], self.lang
        hotel = (self.t("per_night", money=fmt_money(q.hotel_budget_per_night, q.hotel_budget_currency, lang))
                 if q.hotel_budget_per_night else self.t("no_limit"))
        length = nights_range(u["nmin"], u["nmax"], lang)
        return {
            "from": u["origin"].describe(lang),
            "to": u["dest"].describe(lang),
            **({"exceptFrom": self.t("except", places=", ".join(p.describe(lang) for p in u["excl_origin"]))}
               if u["excl_origin"] else {}),
            **({"exceptTo": self.t("except", places=", ".join(p.describe(lang) for p in u["excl_dest"]))}
               if u["excl_dest"] else {}),
            "when": self.t("when_range", start=f"{u['start']:%d.%m.%Y}", end=f"{u['end']:%d.%m.%Y}"),
            "tripLength": self.t("flexible", range=length) if u["flexible"] else length,
            "flightBudget": fmt_money(q.flight_budget, cur, lang),
            "hotelBudget": hotel,
            "totalBudget": fmt_money(q.total_budget, cur, lang),
            "showAboveBudget": self.t("yes") if q.show_above_budget else self.t("no"),
            "stops": self.t("no_limit") if q.max_stops is None else self.t("stops_max", n=q.max_stops),
            "domestic": self.t("domestic_yes") if u["include_domestic"] else self.t("domestic_no"),
            "weather": self.t("weather_warm") if q.weather == "warm" else self.t("weather_any"),
            "priceFreshness": (self.t("fresh_today") if u["max_age"] == 0
                               else self.t("fresh_days", days=u["max_age"])),
            "currency": cur,
        }

    # ── 2. discovery ─────────────────────────────────────────────────────────
    def _pair_allowed(self, o: str, d: str, u: dict) -> bool:
        if o == d or not self.geo.contains(u["origin"], o) or not self.geo.contains(u["dest"], d):
            return False
        if self._excluded(o, u["excl_origin"]) or self._excluded(d, u["excl_dest"]):
            return False
        if not u["include_domestic"]:
            oc, dc = self.geo.cities.get(o), self.geo.cities.get(d)
            if oc is not None and dc is not None and oc.country_code == dc.country_code:
                return False
        return True

    async def _discover(self, q: TripQuery, u: dict) -> dict[tuple[str, str], float]:
        origin: Place = u["origin"]
        dest: Place = u["dest"]
        months = months_between(u["start"], u["end"])
        self.stats.months_searched = months
        cap = self.cfg.max_origin_cities
        best: dict[tuple[str, str], float] = {}

        def keep(f: FlightOption):
            if not (u["start"] <= f.departure_date <= u["end"]) or not self._stops_ok(f, q):
                return
            if not self._fresh(f, u):
                return
            key = (f.origin_city, f.destination_city)
            if not self._pair_allowed(*key, u):
                return
            price = self._to_user(f.price, f.currency, u["currency"])
            if key not in best or price < best[key]:
                best[key] = price

        # Exclusions are applied before the MAX_ORIGIN_CITIES cap, so they never eat the budget.
        origin_cities = [c for c in self.geo.cities_in(origin) if not self._excluded(c, u["excl_origin"])]
        self.stats.origin_cities_available = len(origin_cities)

        if origin.city_level and dest.city_level:
            dest_cities = [c for c in self.geo.cities_in(dest) if not self._excluded(c, u["excl_dest"])]
            if len(origin_cities) * len(dest_cities) <= DIRECT_PAIRS_MAX:
                # Both ends are explicit cities: nothing to discover, straight to the full calendars.
                u["direct_pairs"] = True
                self.stats.origin_cities_searched = origin_cities
                for o in origin_cities:
                    for d in dest_cities:
                        if self._pair_allowed(o, d, {**u, "include_domestic": True}):
                            best[(o, d)] = 0.0
                return best

        # FROM = Anywhere: ask "who flies cheapest into these cities". Any other origin scope is
        # searched forward from its own cities, so far-away cheap origins cannot crowd it out.
        reverse = origin.kind == "anywhere"
        if reverse:
            dest_cities = ([c for c in self.geo.cities_in(dest) if not self._excluded(c, u["excl_dest"])]
                           if dest.kind != "anywhere" else [])
            if not dest_cities:  # Anywhere → Anywhere: search from the best-connected cities
                reverse = False
        if reverse:
            used = dest_cities[:cap]
            if len(dest_cities) > cap:
                self.limits.append(self.t("limit_dest_cities", cap=cap, total=len(dest_cities)))
            self.stats.origin_cities_searched = [self.t("any_city")] if origin.kind == "anywhere" else origin_cities
            coros = [self.flights.cheapest_to(d, m) for d in used for m in months]
            label = self.t("stage_to", n=len(used), m=len(months))
        else:
            used = origin_cities[:cap]
            if len(origin_cities) > cap:
                self.limits.append(self.t("limit_origin_cities", cap=cap, total=len(origin_cities)))
            self.stats.origin_cities_searched = used
            coros = [self.flights.cheapest_from(o, m) for o in used for m in months]
            label = self.t("stage_from", n=len(used), m=len(months))
        for batch in await self._run_all("DISCOVERY", label, coros):
            for f in batch:
                keep(f)
        return best

    # ── 3. candidates ────────────────────────────────────────────────────────
    async def _candidates(self, q: TripQuery, u: dict, best: dict[tuple[str, str], float]
                          ) -> tuple[list[tuple[str, str]], dict[str, str]]:
        by_dest: dict[str, list[tuple[float, str]]] = defaultdict(list)
        for (o, d), price in best.items():
            by_dest[d].append((price, o))
        self.stats.destinations_discovered = len(by_dest)
        ranked = sorted(by_dest, key=lambda d: min(p for p, _ in by_dest[d]))

        if q.flight_budget is not None and not q.show_above_budget:
            # One-way price alone already over the round-trip budget: cannot fit, safe to drop.
            before = len(ranked)
            ranked = [d for d in ranked if min(p for p, _ in by_dest[d]) <= q.flight_budget]
            if before != len(ranked):
                self._emit("CANDIDATES", self.t("stage_over_budget", n=before - len(ranked)))

        climate: dict[str, str] = {}
        if q.weather == "warm" and ranked:
            pool = ranked[: self.cfg.max_candidate_destinations * 4]
            self._emit("CANDIDATES", self.t("stage_climate", n=len(pool)))
            coords = {d: (self.geo.cities[d].lat, self.geo.cities[d].lon) for d in pool if d in self.geo.cities}
            temps = await weather.mean_temperatures(self.http, coords, u["start"], u["end"] + timedelta(days=u["nmax"]))
            if not temps:
                self.warnings.append(self.t("climate_unavailable"))
            else:
                ranked = [d for d in pool if temps.get(d, -99) >= weather.WARM_MIN_MEAN_C]
                climate = {d: self.t("climate_note", t=f"{temps[d]:.0f}", source=weather.SOURCE) for d in ranked}
                missing = [d for d in pool if d not in temps]
                if missing:
                    self.warnings.append(self.t("climate_missing", n=len(missing)))

        k = self.cfg.max_candidate_destinations
        if len(ranked) > k:
            self.limits.append(self.t("limit_candidates", k=k, n=len(ranked)))
        chosen = ranked[:k]
        self.stats.candidate_destinations = len(chosen)
        # An explicit list of cities on both ends is small by construction: check every pair.
        m = len(best) if u.get("direct_pairs") else self.cfg.max_origins_per_destination
        pairs = []
        for d in chosen:
            origins = [o for _, o in sorted(by_dest[d])][:m]
            pairs.extend((o, d) for o in origins)
        if any(len(by_dest[d]) > m for d in chosen):
            self.limits.append(self.t("limit_origins_per_dest", m=m))
        return pairs, climate

    # ── 4. routes ────────────────────────────────────────────────────────────
    async def _routes(self, q: TripQuery, u: dict, pairs: list[tuple[str, str]]
                      ) -> dict[tuple[str, str], tuple[list[FlightOption], list[FlightOption]]]:
        out_months = months_between(u["start"], u["end"])
        ret_months = months_between(u["start"] + timedelta(days=u["nmin"]),
                                    u["end"] + timedelta(days=u["nmax"] + 1))
        jobs, keys = [], []
        for o, d in pairs:
            for m in out_months:
                jobs.append(self.flights.one_way(o, d, m))
                keys.append(("out", o, d))
            for m in ret_months:
                jobs.append(self.flights.one_way(d, o, m))
                keys.append(("ret", o, d))
        results = await self._run_all("ROUTES", self.t("stage_routes", n=len(pairs)), jobs)
        routes: dict[tuple[str, str], tuple[list, list]] = {p: ([], []) for p in pairs}
        for (kind, o, d), batch in zip(keys, results):
            routes[(o, d)][0 if kind == "out" else 1].extend(batch)
        return routes

    # ── 5. combine ───────────────────────────────────────────────────────────
    def _combine(self, q: TripQuery, u: dict, routes) -> dict[str, list[Combo]]:
        per_dest: dict[str, list[Combo]] = defaultdict(list)
        evaluated = 0
        for (o, d), (outs, rets) in routes.items():
            outs = [f for f in outs if u["start"] <= f.departure_date <= u["end"] and self._stops_ok(f, q)
                    and f.origin_city == o and f.destination_city == d and self._fresh(f, u)]
            by_day: dict[date, list[FlightOption]] = defaultdict(list)
            for r in rets:
                if (self._stops_ok(r, q) and r.origin_city == d and r.destination_city == o
                        and self._fresh(r, u)):
                    by_day[r.departure_date].append(r)
            for day in by_day:
                by_day[day].sort(key=lambda r: r.price)
            combos: list[Combo] = []
            for f in outs:
                arrival = f.arrival_at or f.departure_at
                check_in = arrival.date()
                for n in range(u["nmin"], u["nmax"] + 1):
                    check_out = check_in + timedelta(days=n)
                    for r in by_day.get(check_out, []):
                        evaluated += 1
                        if r.departure_at <= arrival:  # would leave before arriving
                            continue
                        combos.append(Combo(o, d, f, r, n, check_in, check_out))
                        break  # by_day is price-sorted: first valid return is the cheapest
            if combos:  # outbound flights with no return fitting the trip length are not a trip
                per_dest[d].extend(combos)
        self.stats.combinations_evaluated = evaluated
        # Keep the cheapest flight combo for every trip length, plus the overall cheapest few:
        # a longer stay is not assumed to be dearer, nor a shorter one cheaper.
        trimmed: dict[str, list[Combo]] = {}
        for d, combos in per_dest.items():
            combos.sort(key=lambda c: c.flight_price_src)
            keep: dict[tuple[date, date, str], Combo] = {}
            per_n: dict[int, Combo] = {}
            for c in combos:
                per_n.setdefault(c.nights, c)
            for c in list(per_n.values()) + combos[:HOTEL_COMBOS_PER_DESTINATION]:
                keep.setdefault((c.check_in, c.check_out, c.origin), c)
            trimmed[d] = sorted(keep.values(), key=lambda c: c.flight_price_src)
        return trimmed

    # ── 6. hotels ────────────────────────────────────────────────────────────
    async def _hotels(self, q: TripQuery, u: dict, combos: dict[str, list[Combo]]):
        """Price hotels for the most promising (destination, dates), not for every combination.

        1. Destinations are ordered by their cheapest flights; the first HOTEL_MAX_DESTINATIONS get hotels.
        2. The provider's usual lowest nightly price in the city ranks each destination's stays by
           flight + nights × that price; the best HOTEL_STAYS_PER_DESTINATION stays are priced for real.
        Everything else is marked "not checked", so its total never pretends to include a hotel.
        """
        combos = {d: cs for d, cs in combos.items() if cs}
        stays_all = sorted({(d, c.check_in, c.check_out) for d, cs in combos.items() for c in cs})
        status = self.hotels.status()
        if not status.available:
            self._emit("HOTELS", f"{self.hotels.name}: {status.message}")
            return {s: None for s in stays_all}, status.message
        if q.breakfast:
            self.warnings.append(self.t("breakfast_unchecked"))
        cur = u["currency"]

        def flight_user(c: Combo) -> float:
            return self._to_user(c.out.price, c.out.currency, cur) + self._to_user(c.ret.price, c.ret.currency, cur)

        n = self.cfg.hotel_max_destinations
        ranked = sorted(combos, key=lambda d: min(flight_user(c) for c in combos[d]))
        pool = ranked[: 2 * n]  # cities without hotel data must not use up the N slots
        typical = await self._run_all("HOTELS", self.t("stage_hotel_lists", n=len(pool)), [
            self.hotels.typical_min_price(d, q.hotel_budget_per_night, q.hotel_budget_currency,
                                          q.min_hotel_rating, q.max_distance_km, q.private_room)
            for d in pool])
        with_data = [(d, t) for d, t in zip(pool, typical) if t][:n]
        # No usual price known (no location, no prices in the city, filters, or a provider without that
        # feature): price its cheapest-flight stays directly. A city-level "no data" answer is then
        # applied to all its dates; a real price only to the dates it was quoted for.
        s_max = self.cfg.hotel_stays_per_destination
        probes = [(d, c) for d, t in zip(pool, typical) if not t
                  for c in sorted(combos[d], key=flight_user)[:s_max]]
        if len(ranked) > len(with_data) + len(probes):
            self.limits.append(self.t("limit_hotels", n=len(with_data), max=n))
        chosen: list[tuple[str, date, date]] = []
        for d, t in with_data:
            per_night = self._to_user(t[0], t[1], cur) if t else 0.0
            by_estimate = sorted(combos[d], key=lambda c: flight_user(c) + c.nights * per_night)
            picked: list[tuple[str, date, date]] = []
            for c in by_estimate:
                stay = (d, c.check_in, c.check_out)
                if stay not in picked:
                    picked.append(stay)
                if len(picked) >= self.cfg.hotel_stays_per_destination:
                    break
            chosen.extend(picked)
        probe_stays = [(d, c.check_in, c.check_out) for d, c in probes]
        jobs = chosen + probe_stays
        results = await self._run_all("HOTELS", self.t("stage_hotel_rates", n=len(chosen)), [
            self.hotels.searchHotels(d, ci, co, q.hotel_budget_per_night, q.hotel_budget_currency,
                                     q.min_hotel_rating, q.max_distance_km, q.private_room, q.breakfast)
            for d, ci, co in jobs])
        failed = HotelSearchResult(status="error", provider=self.hotels.name,
                                   message=self.t("hotel_error"))
        results = [r if isinstance(r, HotelSearchResult) else failed for r in results]
        skipped = HotelSearchResult(status="not_checked", provider=self.hotels.name,
                                    message=self.t("hotel_not_checked"))
        out = {s: skipped for s in stays_all}
        probe_results: dict[str, list[HotelSearchResult]] = defaultdict(list)
        for s, r in zip(probe_stays, results[len(chosen):]):
            probe_results[s[0]].append(r)
            out[s] = r
        for d, rs in probe_results.items():
            if all(r.status in ("unavailable", "no_results") for r in rs):
                for s in stays_all:
                    if s[0] == d and out[s].status == "not_checked":
                        out[s] = rs[0]
        out.update(dict(zip(chosen, results[: len(chosen)])))
        return out, ""

    def _pick_hotel(self, q: TripQuery, offers: list[HotelOffer], user_cur: str) -> tuple[HotelOffer, float, float] | None:
        best = None
        for h in offers:
            try:
                ppn = self._to_user(h.price_per_night, h.currency, q.hotel_budget_currency)
            except FxUnavailable:
                continue
            if q.hotel_budget_per_night is not None and ppn > q.hotel_budget_per_night:
                continue
            if q.min_hotel_rating is not None and (h.rating is None or h.rating < q.min_hotel_rating):
                continue
            if q.max_distance_km is not None and (h.distance_from_center_km is None
                                                  or h.distance_from_center_km > q.max_distance_km):
                continue
            if q.private_room and h.private_room is False:
                continue
            if q.breakfast and h.breakfast is False:
                continue
            total = self._to_user(h.total_price, h.currency, user_cur)
            mandatory = sum(self._to_user(f.amount, f.currency, user_cur) for f in h.fees if f.mandatory)
            if best is None or total + mandatory < best[1] + best[2]:
                best = (h, total, mandatory)
        return best

    # ── 7. totals ────────────────────────────────────────────────────────────
    def _build_result(self, q: TripQuery, u: dict, c: Combo, hotel_res, hotel_msg: str,
                      climate: dict[str, str]) -> TripResult | None:
        cur = u["currency"]
        flight_total = (self._to_user(c.out.price, c.out.currency, cur)
                        + self._to_user(c.ret.price, c.ret.currency, cur))
        hotel = None
        hotel_total = None
        mandatory = optional = 0.0
        hotel_status = "unavailable"
        if hotel_res is not None:
            hotel_status = hotel_res.status
            hotel_msg = hotel_res.message
            if hotel_res.status in ("ok", "no_results"):
                picked = self._pick_hotel(q, hotel_res.offers, cur) if hotel_res.offers else None
                if picked is None:
                    return None  # a real hotel under the user's constraints does not exist for these dates
                hotel, hotel_total, mandatory = picked
                optional = sum(self._to_user(f.amount, f.currency, cur) for f in hotel.fees if not f.mandatory)
                hotel_status = "ok"
        trip_total = flight_total + (hotel_total or 0.0) + mandatory
        notes = []
        above = False
        if q.flight_budget is not None and flight_total > q.flight_budget:
            above = True
            notes.append(self.t("above_flight", total=fmt_money(flight_total, cur, self.lang),
                                budget=fmt_money(q.flight_budget, cur, self.lang)))
        if q.total_budget is not None and trip_total > q.total_budget:
            above = True
            notes.append(self.t("above_trip", total=fmt_money(trip_total, cur, self.lang),
                                budget=fmt_money(q.total_budget, cur, self.lang)))
        if above and not q.show_above_budget:
            if (hotel_status != "not_checked"
                    and (self._cheapest_over_budget is None or trip_total < self._cheapest_over_budget)):
                self._cheapest_over_budget = trip_total
            return None
        if q.total_budget is not None and hotel_status == "not_checked":
            # The hotel exists but was not priced for these dates: "fits the budget" would only mean
            # "the hotel was left out". Such a trip cannot be shown as within the total budget.
            self._budget_unverified += 1
            return None
        if q.total_budget is not None and hotel_total is None:
            notes.append(self.t("budget_without_hotel"))

        warnings = self._warnings(c)
        if hotel is not None and "hostel" in (hotel.accommodation_type or "").lower():
            warnings.append(self.t("hostel"))
        city = self.geo.cities.get(c.dest)
        country = self.geo.countries.get(city.country_code) if city else None
        checked = min(c.out.checked_at, c.ret.checked_at, *( [hotel.checked_at] if hotel else []))
        return TripResult(
            destination=self.geo.city_name(c.dest, self.lang), destination_code=c.dest,
            country=self.geo.country_name(country.code, self.lang) if country else (city.country_code if city else ""),
            country_code=city.country_code if city else "",
            # Travelpayouts has codes like AB (Abkhazia) that are not ISO: no emoji flag exists for them.
            flag=flag(city.country_code) if city and country and country.region else "🏳️",
            region=country.region if country else None,
            origin=self.geo.city_name(c.origin, self.lang), origin_code=c.origin,
            originAirport=c.out.origin_airport, destinationAirport=c.out.destination_airport,
            departureDate=c.out.departure_date, returnDate=c.ret.departure_date, nights=c.nights,
            outbound=c.out, **{"return": c.ret},
            hotel=hotel, hotel_status=hotel_status, hotel_message=hotel_msg,
            currency=cur, flightTotal=round(flight_total, 2),
            hotelTotal=round(hotel_total, 2) if hotel_total is not None else None,
            mandatoryFees=round(mandatory, 2), optionalFees=round(optional, 2),
            tripTotal=round(trip_total, 2), total_includes_hotel=hotel_total is not None,
            above_budget=above, budget_notes=notes,
            provider=f"{c.out.provider}" + (f" + {hotel.provider}" if hotel else ""),
            checkedAt=checked,
            flightUrl=c.out.booking_url, returnFlightUrl=c.ret.booking_url,
            hotelUrl=hotel.url if hotel else None,
            isSelfTransfer=self._merge_flag(c.out.is_self_transfer, c.ret.is_self_transfer),
            airportChange=self._merge_flag(c.out.airport_change, c.ret.airport_change),
            warnings=warnings,
            climate_note=climate.get(c.dest),
        )

    @staticmethod
    def _merge_flag(a: bool | None, b: bool | None) -> bool | None:
        if a or b:
            return True
        if a is None or b is None:
            return None
        return False

    def _warnings(self, c: Combo) -> list[str]:
        w = []
        if c.out.destination_airport != c.ret.origin_airport:
            w.append(self.t("w_airports_differ", a=self.geo.airport_label(c.out.destination_airport, self.lang),
                            b=self.geo.airport_label(c.ret.origin_airport, self.lang)))
        if c.out.origin_airport != c.ret.destination_airport:
            w.append(self.t("w_home_airports", a=c.out.origin_airport, b=c.ret.destination_airport))
        if c.out.arrival_at is None:
            w.append(self.t("w_no_arrival"))
        elif c.out.arrival_at.hour in NIGHT_ARRIVAL_HOURS:
            w.append(self.t("w_night_arrival", time=f"{c.out.arrival_at:%H:%M}"))
        if c.ret.departure_at.hour in EARLY_DEPARTURE_HOURS:
            w.append(self.t("w_early_return", time=f"{c.ret.departure_at:%H:%M}"))
        for leg, f in ((self.t("leg_out"), c.out), (self.t("leg_ret"), c.ret)):
            if f.stops >= 3:
                w.append(self.t("w_many_stops", leg=leg, stops=stops(f.stops, self.lang)))
            if f.booking_url_kind == "route_search":
                w.append(self.t("w_route_link", leg=leg))
        return w

    # ── 8. revalidate ────────────────────────────────────────────────────────
    async def _revalidate_leg(self, f: FlightOption) -> tuple[FlightOption | None, str]:
        fresh = await self.flights.one_way(f.origin_city, f.destination_city,
                                           f.departure_date.isoformat(), fresh=True)
        same = [x for x in fresh if x.same_ticket(f)]
        if same:
            x = min(same, key=lambda x: x.price)
            note = ("" if abs(x.price - f.price) < 1
                    else self.t("price_changed", old=f"{f.price:.0f}", new=f"{x.price:.0f}", cur=x.currency))
            return x, note
        return None, self.t("ticket_gone")

    async def _revalidate(self, q: TripQuery, u: dict, results: list[TripResult], build) -> list[TripResult]:
        top = results[: self.cfg.revalidate_top]
        if not top:
            return results
        self._emit("REVALIDATE", self.t("stage_revalidate", n=len(top)))
        jobs = []
        for r in top:
            jobs.append(self._revalidate_leg(r.outbound))
            jobs.append(self._revalidate_leg(r.return_))
        checked = await self._run_all("REVALIDATE", self.t("stage_recheck"), jobs)
        out = []
        for i, r in enumerate(top):
            leg_out, leg_ret = checked[2 * i], checked[2 * i + 1]
            if not leg_out or not leg_ret:  # provider error: keep, but say it was not rechecked
                r.revalidation_note = self.t("recheck_failed")
                out.append(r)
                continue
            (new_out, n1), (new_ret, n2) = leg_out, leg_ret
            if new_out is None or new_ret is None:
                self.warnings.append(self.t("recheck_dropped", place=r.destination,
                                            dates=f"{r.departureDate:%d.%m}–{r.returnDate:%d.%m}", reason=n1 or n2))
                continue
            rebuilt = build(Combo(r.origin_code, r.destination_code, new_out, new_ret, r.nights,
                                  (new_out.arrival_at or new_out.departure_at).date(),
                                  new_ret.departure_date))
            if rebuilt is None:
                continue
            rebuilt.revalidated = True
            rebuilt.revalidation_note = "; ".join(n for n in (n1, n2) if n) or self.t("price_confirmed")
            out.append(rebuilt)
        return out + results[len(top):]

    # ── 9. validate & rank ───────────────────────────────────────────────────
    def _valid(self, r: TripResult, u: dict) -> bool:
        legs = (r.outbound, r.return_)
        return (
            all(f.price > 0 and f.currency and f.provider and f.checked_at and f.booking_url for f in legs)
            and r.departureDate > self.today
            and r.returnDate > r.departureDate
            and u["nmin"] <= r.nights <= u["nmax"]
            and r.return_.departure_at > (r.outbound.arrival_at or r.outbound.departure_at)
            and bool(r.currency)
        )

    def _rank_key(self, r: TripResult):
        # With a hotel provider on, totals that include a real hotel come before flight-only totals,
        # which would otherwise look cheaper just because the hotel is missing.
        hotels_on = self.hotels.status().available
        return (r.above_budget, hotels_on and not r.total_includes_hotel, r.tripTotal)

    # ── main ─────────────────────────────────────────────────────────────────
    async def search(self, q: TripQuery) -> SearchReport:
        t0 = time.monotonic()
        self.lang = norm_lang(q.lang)
        self._emit("UNDERSTAND", self.t("stage_understand"))
        u = self._understand(q)
        interpreted = self._interpreted(q, u)
        providers = [self.flights.status(), self.hotels.status()]
        if not providers[0].available:
            raise SearchAborted(providers[0].message)
        try:
            await self.fx.load()
        except FxUnavailable as exc:
            if u["currency"] != "RUB":
                raise SearchAborted(self.t("fx_needed", err=exc, cur=u["currency"])) from exc
            self.warnings.append(str(exc))

        best = await self._discover(q, u)
        self._emit("DISCOVERY", self.t("stage_found", n=len({d for _, d in best})))
        if not best:
            return self._report(q, u, interpreted, providers, [], [], t0)

        pairs, climate = await self._candidates(q, u, best)
        routes = await self._routes(q, u, pairs)
        self._emit("COMBINE", self.t("stage_combine"))
        combos = self._combine(q, u, routes)
        self._emit("COMBINE", self.t("stage_combined", n=self.stats.combinations_evaluated))

        hotel_map, hotel_msg = await self._hotels(q, u, combos)
        self._emit("TOTAL", self.t("stage_total"))

        def build(c: Combo) -> TripResult | None:
            return self._build_result(q, u, c, hotel_map.get((c.dest, c.check_in, c.check_out)),
                                      hotel_msg, climate)

        per_dest: dict[str, list[TripResult]] = {}
        for d, cs in combos.items():
            built = [r for r in (build(c) for c in cs) if r is not None and self._valid(r, u)]
            if built:
                per_dest[d] = sorted(built, key=self._rank_key)

        results = sorted((rs[0] for rs in per_dest.values()), key=self._rank_key)
        results = await self._revalidate(q, u, results, build)
        results = sorted((r for r in results if self._valid(r, u)), key=self._rank_key)

        alternatives = []
        if results:
            head = results[0]
            alternatives = [r for r in per_dest.get(head.destination_code, [])
                            if (r.departureDate, r.returnDate) != (head.departureDate, head.returnDate)][:ALTERNATIVES]
        self._emit("RANK", self.t("stage_done", n=len(results)))
        return self._report(q, u, interpreted, providers, results, alternatives, t0)

    def _report(self, q, u, interpreted, providers, results, alternatives, t0) -> SearchReport:
        self.stats.api_requests = self.http.stats.network
        self.stats.api_cached = self.http.stats.cached
        self.stats.rate_limited = self.http.stats.rate_limited
        self.stats.seconds = round(time.monotonic() - t0, 1)
        if self._stale:
            self.limits.append(self.t("limit_stale", n=self._stale, days=u["max_age"]))
        if self._budget_unverified:
            self.warnings.append(self.t("budget_unverified", n=self._budget_unverified))
        if not results and self._cheapest_over_budget is not None:
            self.warnings.append(self.t("all_over_budget",
                                        cheapest=fmt_money(self._cheapest_over_budget, u["currency"], self.lang)))
        elif not results:
            self.warnings.append(self.t("nothing_found"))
        return SearchReport(
            query=q, interpreted=interpreted, results=results, alternatives=alternatives,
            providers=providers, fx=list(self.fx.used.values()), stats=self.stats,
            limits=self.limits, warnings=self.warnings, generated_at=datetime.now(timezone.utc),
        )
