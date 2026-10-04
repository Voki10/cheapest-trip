"""Flights from the Travelpayouts / Aviasales Data API (v3 prices_for_dates).

The Data API serves prices that real Aviasales users found in recent searches. It is real data,
but a cache, not a live seat check, and it does not return per-segment details. Both limits are
reported to the user through status().limitations instead of being papered over.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..geo import Geo
from ..http import HttpClient, HttpError
from ..i18n import tr
from ..models import FlightOption, ProviderStatus
from .base import FlightProvider, ProviderError

API_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
BOOKING_BASE = "https://www.aviasales.ru"
PAGE_LIMIT = 1000
PROVIDER_NAME = "Aviasales"


def limitations(lang: str) -> list[str]:
    return [tr("aviasales_lim_cache", lang), tr("aviasales_lim_live", lang), tr("aviasales_lim_segments", lang),
            tr("aviasales_lim_price", lang), tr("aviasales_lim_page", lang, n=PAGE_LIMIT)]


def _tz(name: str | None):
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return None


class AviasalesProvider(FlightProvider):
    name = PROVIDER_NAME

    def __init__(self, http: HttpClient, geo: Geo, token: str, marker: str = "", market: str = "ru",
                 lang: str = "ru"):
        self.lang = lang
        self.http = http
        self.geo = geo
        self.token = token
        self.marker = marker
        self.market = market

    def status(self) -> ProviderStatus:
        if not self.token:
            return ProviderStatus(
                name=self.name, kind="flights", available=False,
                message=tr("flights_missing", self.lang), limitations=limitations(self.lang),
            )
        return ProviderStatus(name=self.name, kind="flights", available=True,
                              message="Travelpayouts Data API", limitations=limitations(self.lang))

    # ── public queries ───────────────────────────────────────────────────────
    async def cheapest_from(self, origin_city: str, month: str) -> list[FlightOption]:
        return await self._query({"origin": origin_city, "departure_at": month, "unique": "true"})

    async def cheapest_to(self, destination_city: str, month: str) -> list[FlightOption]:
        return await self._query({"destination": destination_city, "departure_at": month})

    async def one_way(self, origin: str, destination: str, departure: str,
                      fresh: bool = False) -> list[FlightOption]:
        return await self._query({"origin": origin, "destination": destination,
                                  "departure_at": departure}, fresh=fresh)

    # ── internals ────────────────────────────────────────────────────────────
    async def _query(self, params: dict, fresh: bool = False) -> list[FlightOption]:
        if not self.token:
            raise ProviderError("TRAVELPAYOUTS_TOKEN is not set")
        full = {
            **params,
            "one_way": "true",
            "sorting": "price",
            "limit": PAGE_LIMIT,
            "currency": "rub",
            "market": self.market,
        }
        try:
            fetched = await self.http.get_json(
                API_URL, full, headers={"X-Access-Token": self.token},
                cache_seconds=0 if fresh else None,
            )
        except HttpError as exc:
            if exc.status == 401:
                raise ProviderError(tr("flights_bad_token", self.lang)) from exc
            if exc.status == 400:
                return []  # e.g. a month outside the provider's range: nothing to offer, not an outage
            raise ProviderError(str(exc)) from exc
        body = fetched.data
        if isinstance(body, dict) and body.get("success") is False:
            raise ProviderError(f"Travelpayouts: {body.get('error') or 'unknown error'}")
        currency = str((body or {}).get("currency") or "rub").upper()
        out = []
        for t in (body or {}).get("data") or []:
            opt = self._parse(t, currency, fetched.fetched_at)
            if opt is not None:
                out.append(opt)
        return out

    def _parse(self, t: dict, currency: str, fetched_at: datetime) -> FlightOption | None:
        price = t.get("price")
        dep_raw = t.get("departure_at")
        origin, dest = t.get("origin"), t.get("destination")
        if not price or price <= 0 or not dep_raw or not origin or not dest:
            return None
        try:
            dep = datetime.fromisoformat(dep_raw)
        except ValueError:
            return None
        if dep.tzinfo is None:
            tz = _tz(self.geo.tz_of(t.get("origin_airport") or origin))
            if tz is None:
                return None
            dep = dep.replace(tzinfo=tz)
        expires = None
        if t.get("expires_at"):
            try:
                expires = datetime.fromisoformat(str(t["expires_at"]).replace("Z", "+00:00"))
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
            except ValueError:
                expires = None
        if expires is not None and expires < datetime.now(timezone.utc):
            return None  # the provider itself says this price is no longer valid
        door_to_door = t.get("duration") or t.get("duration_to")
        dest_airport = t.get("destination_airport") or dest
        arrival = None
        if door_to_door:
            dest_tz = _tz(self.geo.tz_of(dest_airport)) or _tz(self.geo.tz_of(dest))
            if dest_tz is not None:
                arrival = (dep + timedelta(minutes=int(door_to_door))).astimezone(dest_tz)
        flight_number = t.get("flight_number")
        link = t.get("link") or ""
        seen = None
        if m := re.search(r"[?&]search_date=(\d{2})(\d{2})(\d{4})", link):
            try:
                seen = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            except ValueError:
                seen = None
        return FlightOption(
            provider=self.name,
            price_source=tr("aviasales_source", self.lang),
            origin_city=origin,
            destination_city=dest,
            origin_airport=t.get("origin_airport") or origin,
            destination_airport=dest_airport,
            departure_at=dep,
            arrival_at=arrival,
            duration_minutes=int(door_to_door) if door_to_door else None,
            flight_minutes=int(t["duration_to"]) if t.get("duration_to") else None,
            stops=int(t.get("transfers") or 0),
            airline=t.get("airline") or None,
            flight_number=str(flight_number) if flight_number else None,
            price=float(price),
            currency=currency,
            booking_url=self._booking_url(link, origin, dest, dep),
            booking_url_kind="ticket" if link else "route_search",
            seller=t.get("gate") or None,
            checked_at=fetched_at,
            price_seen_on=seen,
            expires_at=expires,
        )

    def _booking_url(self, link: str, origin: str, dest: str, dep: datetime) -> str:
        if not link:
            # Deterministic Aviasales route-search link: opens the live search for that day.
            link = f"/search/{origin}{dep:%d%m}{dest}1"
        if self.marker:
            path, _, query = link.partition("?")
            params = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True) if k != "marker"]
            params.append(("marker", self.marker))
            link = f"{path}?{urlencode(params)}"
        return BOOKING_BASE + link
