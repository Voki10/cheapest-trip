"""Hotels from Xotelo: TripAdvisor price comparison (Booking.com, Trip.com, Agoda, Expedia, …).

Free and keyless. For a stay it lists the city's hotels (rating, coordinates, typical price range),
keeps those matching the user's filters, and fetches real rates for the given dates from every
site TripAdvisor compares. The cheapest site's price becomes the offer; all sites are shown.
"""

from __future__ import annotations

import asyncio
from datetime import date

from ..fx import Fx, FxUnavailable
from ..geo import Geo
from ..http import HttpClient, HttpError
from ..i18n import tr
from ..models import Fee, HotelOffer, HotelSearchResult, ProviderStatus
from ..tripadvisor_geo import LIST_CACHE_SECONDS, TripAdvisorLocator, km
from .base import HotelProvider

LIST_URL = "https://data.xotelo.com/api/list"
RATES_URL = "https://data.xotelo.com/api/rates"
NAME = "Xotelo / TripAdvisor"
LIST_PAGES = 2  # 2 × 100 hotels per city, sorted by TripAdvisor "best value"
CURRENCY = "USD"

LIMITATION_KEYS = ("xotelo_lim_source", "xotelo_lim_taxes", "xotelo_lim_breakfast", "xotelo_lim_rating",
                   "xotelo_lim_geo")


class XoteloProvider(HotelProvider):
    name = NAME

    def __init__(self, http: HttpClient, geo: Geo, fx: Fx, rapidapi_key: str = "",
                 candidates_per_stay: int = 3, lang: str = "ru"):
        self.lang = lang
        self.rapidapi_key = rapidapi_key
        self.http = http
        self.geo = geo
        self.fx = fx
        self.locator = TripAdvisorLocator(http, geo, rapidapi_key)
        self.candidates_per_stay = candidates_per_stay

    def status(self) -> ProviderStatus:
        return ProviderStatus(name=self.name, kind="hotels", available=True,
                              message=tr("xotelo_status", self.lang),
                              limitations=[tr(k, self.lang) for k in LIMITATION_KEYS])

    # ── city hotel list ──────────────────────────────────────────────────────
    async def _hotels(self, city: str) -> tuple[list[dict] | None, str]:
        geo_id, how = await self.locator.location_for(city)
        if geo_id is None:
            if how == "no_coords":
                return None, tr("hotels_no_coords", self.lang)
            return None, tr("hotels_no_location", self.lang,
                            hint="" if self.rapidapi_key else tr("hotels_location_hint", self.lang))
        hotels: list[dict] = []
        for page in range(LIST_PAGES):
            try:
                fetched = await self.http.get_json(
                    LIST_URL, {"location_key": f"g{geo_id}", "limit": 100, "offset": page * 100,
                               "sort": "best_value"}, cache_seconds=LIST_CACHE_SECONDS)
            except HttpError as exc:
                if not hotels:
                    return None, tr("xotelo_down", self.lang, err=exc)
                break
            batch = ((fetched.data or {}).get("result") or {}).get("list") or []
            hotels.extend(batch)
            if len(batch) < 100:
                break
        return hotels, how

    def _usd(self, amount: float | None, currency: str) -> float | None:
        if amount is None:
            return None
        try:
            return self.fx.convert(amount, currency, CURRENCY)
        except FxUnavailable:
            return None

    def _filter(self, city: str, hotels: list[dict], max_usd: float | None, rating: float | None,
                max_distance: float | None, private_room: bool | None) -> list[dict]:
        c = self.geo.cities.get(city)
        out = []
        for h in hotels:
            pr = h.get("price_ranges") or {}
            h["_min"] = pr.get("minimum")
            g = h.get("geo") or {}
            h["_km"] = (km(c.lat, c.lon, g["latitude"], g["longitude"])
                        if c and c.lat is not None and g.get("latitude") is not None else None)
            rs = h.get("review_summary") or {}
            r = rs.get("rating") if rs.get("count") else None
            if rating is not None and (r is None or r < rating):
                continue
            if max_distance is not None and (h["_km"] is None or h["_km"] > max_distance):
                continue
            if private_room and "hostel" in (h.get("accommodation_type") or "").lower():
                continue
            if max_usd is not None and h["_min"] is not None and h["_min"] > max_usd * 1.15:
                continue  # its usual price is well above the budget: skip the rate call
            out.append(h)
        # Cheapest typical price first; hotels without a range go last.
        out.sort(key=lambda h: (h["_min"] is None, h["_min"] or 0))
        return out

    async def typical_min_price(self, destination: str, maxPricePerNight: float | None, maxPriceCurrency: str,
                                rating: float | None, maxDistance: float | None,
                                privateRoom: bool | None) -> tuple[float, str] | None:
        """Lowest usual nightly price among matching hotels — only used to decide which stays to price."""
        hotels, _ = await self._hotels(destination)
        if not hotels:
            return None
        matching = self._filter(destination, hotels, self._usd(maxPricePerNight, maxPriceCurrency),
                                rating, maxDistance, privateRoom)
        mins = [h["_min"] for h in matching if h["_min"]]
        return (min(mins), CURRENCY) if mins else None

    # ── rates for the dates ──────────────────────────────────────────────────
    async def searchHotels(self, destination: str, checkIn: date, checkOut: date,
                           maxPricePerNight: float | None, maxPriceCurrency: str,
                           rating: float | None, maxDistance: float | None,
                           privateRoom: bool | None, breakfast: bool | None) -> HotelSearchResult:
        hotels, how = await self._hotels(destination)
        if hotels is None:
            return HotelSearchResult(status="unavailable", provider=self.name, message=how)
        if not any((h.get("price_ranges") or {}).get("minimum") for h in hotels):
            # TripAdvisor lists the hotels but no site sells them (e.g. no OTA works in the country):
            # there is no price to show — that is missing data, not "no hotel under your budget".
            return HotelSearchResult(status="unavailable", provider=self.name,
                                     message=tr("hotels_no_prices_city", self.lang))
        max_usd = self._usd(maxPricePerNight, maxPriceCurrency)
        matching = self._filter(destination, hotels, max_usd, rating, maxDistance, privateRoom)
        if not matching:
            return HotelSearchResult(status="no_results", provider=self.name,
                                     message=tr("hotels_no_match", self.lang))
        nights = (checkOut - checkIn).days
        offers: list[HotelOffer] = []
        # Walk down the cheapest-typical list until enough hotels have real rates for these dates.
        for start in range(0, min(len(matching), self.candidates_per_stay * 3), self.candidates_per_stay):
            batch = matching[start:start + self.candidates_per_stay]
            found = await asyncio.gather(*(self._offer(h, checkIn, checkOut, nights) for h in batch))
            offers.extend(o for o in found if o is not None)
            if offers:
                break
        if not offers:
            return HotelSearchResult(status="unavailable", provider=self.name,
                                     message=tr("hotels_no_prices_dates", self.lang))
        return HotelSearchResult(status="ok", provider=self.name, offers=offers,
                                 message=tr("hotels_ok", self.lang))

    async def _offer(self, h: dict, check_in: date, check_out: date, nights: int) -> HotelOffer | None:
        key = h.get("key")
        if not key:
            return None
        try:
            fetched = await self.http.get_json(
                RATES_URL, {"hotel_key": key, "chk_in": check_in.isoformat(), "chk_out": check_out.isoformat(),
                            "currency": CURRENCY})
        except HttpError:
            return None
        body = fetched.data or {}
        if body.get("error"):
            return None
        rates = [r for r in ((body.get("result") or {}).get("rates") or []) if (r.get("rate") or 0) > 0]
        if not rates:
            return None
        best = min(rates, key=lambda r: r["rate"] + (r.get("tax") or 0))
        fees = []
        if best.get("tax"):
            fees.append(Fee(name=tr("hotels_taxes", self.lang, site=best.get("name")), amount=best["tax"] * nights,
                            currency=CURRENCY, mandatory=True))
        reviews = (h.get("review_summary") or {}).get("count") or 0
        return HotelOffer(
            name=h.get("name") or key,
            price_per_night=float(best["rate"]),
            total_price=float(best["rate"]) * nights,
            currency=CURRENCY,
            rating=(h.get("review_summary") or {}).get("rating") if reviews else None,  # 0 reviews ≠ rating 0
            reviews=reviews,
            distance_from_center_km=round(h["_km"], 1) if h.get("_km") is not None else None,
            private_room=None if "hostel" in (h.get("accommodation_type") or "").lower() else True,
            breakfast=None,
            accommodation_type=h.get("accommodation_type"),
            fees=fees,
            sold_by=best.get("name"),
            site_prices=[{"site": r.get("name"), "rate": r["rate"]} for r in sorted(rates, key=lambda r: r["rate"])],
            provider=self.name,
            url=h.get("url") or "",
            checked_at=fetched.fetched_at,
        )
