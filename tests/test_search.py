"""Optimization logic tests. The fake providers below exist only in tests; the service never
ships with fake data."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from cheaptrip.config import load_settings
from cheaptrip.fx import Fx
from cheaptrip.geo import get_geo
from cheaptrip.http import HttpClient
from cheaptrip.models import (FlightOption, HotelOffer, HotelSearchResult, ProviderStatus, TripQuery)
from cheaptrip.providers.base import FlightProvider, HotelProvider
from cheaptrip.search import CheapTripSearchService

TODAY = date(2026, 9, 29)
NOW = datetime(2026, 9, 29, 18, 0, tzinfo=timezone.utc)
TZ = {"MOW": "Europe/Moscow", "IST": "Europe/Istanbul", "TBS": "Asia/Tbilisi",
      "AER": "Europe/Moscow", "BKK": "Asia/Bangkok", "LED": "Europe/Moscow", "EVN": "Asia/Yerevan"}


def ticket(o, d, day, price, stops=0, hour=10, duration=240, fn="100", o_ap=None, d_ap=None):
    dep = datetime(day.year, day.month, day.day, hour, 0, tzinfo=ZoneInfo(TZ[o]))
    arr = (dep + timedelta(minutes=duration)).astimezone(ZoneInfo(TZ[d]))
    return FlightOption(
        provider="FakeAir", price_source="test fixture", origin_city=o, destination_city=d,
        origin_airport=o_ap or o, destination_airport=d_ap or d, departure_at=dep, arrival_at=arr,
        duration_minutes=duration, stops=stops, airline="XX", flight_number=fn, price=price,
        currency="RUB", booking_url=f"https://example.test/{o}{d}{day:%d%m}{fn}", checked_at=NOW,
    )


class FakeFlights(FlightProvider):
    name = "FakeAir"

    def __init__(self, tickets, vanish=()):
        self.tickets = tickets
        self.vanish = set(vanish)  # booking urls that disappear on revalidation
        self.calls = 0

    def status(self):
        return ProviderStatus(name=self.name, kind="flights", available=True)

    def _match(self, f, departure):
        return f"{f.departure_at:%Y-%m-%d}".startswith(departure)

    async def cheapest_from(self, origin_city, month):
        self.calls += 1
        best = {}
        for f in self.tickets:
            if f.origin_city == origin_city and self._match(f, month):
                if f.destination_city not in best or f.price < best[f.destination_city].price:
                    best[f.destination_city] = f
        return list(best.values())

    async def cheapest_to(self, destination_city, month):
        self.calls += 1
        return [f for f in self.tickets if f.destination_city == destination_city and self._match(f, month)]

    async def one_way(self, origin, destination, departure, fresh=False):
        self.calls += 1
        out = [f for f in self.tickets if f.origin_city == origin and f.destination_city == destination
               and self._match(f, departure)]
        if fresh:
            out = [f for f in out if f.booking_url not in self.vanish]
        return out


class FakeHotels(HotelProvider):
    name = "FakeHotels"

    def __init__(self, per_night_usd: dict[str, float] | None):
        self.prices = per_night_usd

    def status(self):
        return ProviderStatus(name=self.name, kind="hotels", available=self.prices is not None,
                              message="" if self.prices is not None else "Hotel provider connection required")

    async def searchHotels(self, destination, checkIn, checkOut, maxPricePerNight, maxPriceCurrency,
                           rating, maxDistance, privateRoom, breakfast):
        if self.prices is None:
            return HotelSearchResult(status="unavailable", provider=self.name, message="n/a")
        nights = (checkOut - checkIn).days
        p = self.prices.get(destination)
        if p is None:
            return HotelSearchResult(status="no_results", provider=self.name)
        return HotelSearchResult(status="ok", provider=self.name, offers=[HotelOffer(
            name=f"Hotel {destination}", price_per_night=p, total_price=p * nights, currency="USD",
            rating=8.0, distance_from_center_km=1.0, provider=self.name,
            url=f"https://example.test/h/{destination}", checked_at=NOW)])


class FixedFx(Fx):
    def __init__(self, http):
        super().__init__(http)

    async def load(self):
        self._rub_per_unit = {"RUB": 1.0, "USD": 100.0, "EUR": 110.0}
        self._checked_at = NOW
        self._rate_date = TODAY


def run(query: TripQuery, flights: FakeFlights, hotels: FakeHotels, **cfg):
    settings = load_settings()
    settings = settings.__class__(**{**settings.__dict__, "revalidate_top": 5, **cfg})

    async def go():
        async with HttpClient() as http:
            svc = CheapTripSearchService(get_geo(), flights, hotels, FixedFx(http), http, settings,
                                         today=TODAY)
            return await svc.search(query)

    return asyncio.run(go())


D = lambda m, d: date(2026, m, d)  # noqa: E731


def test_total_trip_cost_beats_cheapest_ticket():
    # A: flight 10 000 total, hotel $80/night; B: flight 15 000, hotel $25/night — B must win.
    tickets = [
        ticket("MOW", "IST", D(11, 10), 5000), ticket("IST", "MOW", D(11, 15), 5000),
        ticket("MOW", "TBS", D(11, 10), 7500), ticket("TBS", "MOW", D(11, 15), 7500),
    ]
    hotels = FakeHotels({"IST": 80.0, "TBS": 25.0})
    rep = run(TripQuery(origin="MOW", destination="Anywhere", nights_min=5, nights_max=5), FakeFlights(tickets), hotels)
    assert [r.destination_code for r in rep.results] == ["TBS", "IST"]
    tbs = rep.results[0]
    assert tbs.flightTotal == 15000 and tbs.hotelTotal == 25 * 5 * 100
    assert tbs.tripTotal == 15000 + 12500 and tbs.total_includes_hotel
    assert rep.fx and rep.fx[0].exchangeRateSource


def test_outbound_and_return_chosen_independently():
    tickets = [
        ticket("MOW", "IST", D(11, 15), 12000, stops=0, fn="1"),
        ticket("MOW", "IST", D(11, 15), 7500, stops=2, fn="2", duration=600),
        ticket("IST", "MOW", D(11, 20), 11000, stops=0, fn="3"),
        ticket("IST", "MOW", D(11, 20), 6800, stops=1, fn="4", duration=500),
    ]
    rep = run(TripQuery(origin="MOW", destination="IST", nights_min=5, nights_max=5),
              FakeFlights(tickets), FakeHotels(None))
    r = rep.results[0]
    assert r.outbound.stops == 2 and r.return_.stops == 1
    assert r.flightTotal == 14300
    assert not r.total_includes_hotel and r.hotel_status == "unavailable"


def test_many_stops_allowed_when_cheapest():
    tickets = [
        ticket("MOW", "BKK", D(12, 1), 40000, stops=0, fn="1"),
        ticket("MOW", "BKK", D(12, 1), 21000, stops=4, fn="2", duration=2000),
        ticket("BKK", "MOW", D(12, 8), 30000, stops=1, fn="3"),
    ]
    rep = run(TripQuery(origin="MOW", destination="BKK", nights_min=5, nights_max=7),
              FakeFlights(tickets), FakeHotels(None))
    r = rep.results[0]
    assert r.outbound.stops == 4 and r.flightTotal == 51000
    assert any("4 пересадки" in w for w in r.warnings)


def test_flight_budget_hard_limit_and_above_budget_flag():
    tickets = [
        ticket("MOW", "IST", D(11, 10), 6000), ticket("IST", "MOW", D(11, 15), 6000),   # 12 000
        ticket("MOW", "TBS", D(11, 10), 4000), ticket("TBS", "MOW", D(11, 15), 4000),   # 8 000
    ]
    q = TripQuery(origin="MOW", destination="Anywhere", nights_min=5, nights_max=5, flight_budget=10000)
    rep = run(q, FakeFlights(tickets), FakeHotels(None))
    assert [r.destination_code for r in rep.results] == ["TBS"]
    rep = run(q.model_copy(update={"show_above_budget": True}), FakeFlights(tickets), FakeHotels(None))
    assert [(r.destination_code, r.above_budget) for r in rep.results] == [("TBS", False), ("IST", True)]


def test_no_flight_budget_means_no_limit():
    tickets = [ticket("MOW", "IST", D(11, 10), 90000), ticket("IST", "MOW", D(11, 15), 90000)]
    rep = run(TripQuery(origin="MOW", destination="IST", nights_min=5, nights_max=5),
              FakeFlights(tickets), FakeHotels(None))
    assert rep.results and rep.results[0].flightTotal == 180000


def test_trip_length_range_and_overnight_arrival():
    # Departs 23:00, lands next day → check-in is the arrival date, so 4 nights, not 5.
    tickets = [
        ticket("MOW", "TBS", D(11, 10), 5000, hour=23, duration=200),
        ticket("TBS", "MOW", D(11, 15), 5000), ticket("TBS", "MOW", D(11, 18), 3000),
    ]
    rep = run(TripQuery(origin="MOW", destination="TBS", nights_min=3, nights_max=7),
              FakeFlights(tickets), FakeHotels(None))
    r = rep.results[0]
    assert r.returnDate == D(11, 18) and r.nights == 7 and r.flightTotal == 8000
    rep = run(TripQuery(origin="MOW", destination="TBS", nights_min=4, nights_max=4),
              FakeFlights(tickets), FakeHotels(None))
    assert rep.results[0].nights == 4 and rep.results[0].returnDate == D(11, 15)


def test_longer_trip_can_be_cheaper_with_hotels():
    tickets = [
        ticket("MOW", "IST", D(11, 10), 5000),
        ticket("IST", "MOW", D(11, 13), 20000),  # 3 nights, pricey return
        ticket("IST", "MOW", D(11, 17), 4000),   # 7 nights, cheap return
    ]
    rep = run(TripQuery(origin="MOW", destination="IST", nights_min=3, nights_max=7),
              FakeFlights(tickets), FakeHotels({"IST": 10.0}))
    r = rep.results[0]
    assert r.nights == 7 and r.tripTotal == 9000 + 7 * 10 * 100


def test_domestic_excluded_for_anywhere_by_default():
    tickets = [
        ticket("MOW", "AER", D(11, 10), 2000), ticket("AER", "MOW", D(11, 15), 2000),
        ticket("MOW", "IST", D(11, 10), 6000), ticket("IST", "MOW", D(11, 15), 6000),
    ]
    rep = run(TripQuery(origin="MOW", destination="Anywhere", nights_min=5, nights_max=5),
              FakeFlights(tickets), FakeHotels(None))
    assert [r.destination_code for r in rep.results] == ["IST"]
    rep = run(TripQuery(origin="MOW", destination="Anywhere", nights_min=5, nights_max=5, include_domestic=True),
              FakeFlights(tickets), FakeHotels(None))
    assert [r.destination_code for r in rep.results] == ["AER", "IST"]


def test_vanished_ticket_is_dropped_on_revalidation():
    gone = ticket("MOW", "TBS", D(11, 10), 3000, fn="9")
    tickets = [gone, ticket("TBS", "MOW", D(11, 15), 3000),
               ticket("MOW", "IST", D(11, 10), 6000), ticket("IST", "MOW", D(11, 15), 6000)]
    rep = run(TripQuery(origin="MOW", destination="Anywhere", nights_min=5, nights_max=5),
              FakeFlights(tickets, vanish={gone.booking_url}), FakeHotels(None))
    assert [r.destination_code for r in rep.results] == ["IST"]
    assert rep.results[0].revalidated
    assert any("больше не найден" in w for w in rep.warnings)


def test_hotel_missing_under_constraints_drops_destination():
    tickets = [
        ticket("MOW", "IST", D(11, 10), 3000), ticket("IST", "MOW", D(11, 15), 3000),
        ticket("MOW", "TBS", D(11, 10), 6000), ticket("TBS", "MOW", D(11, 15), 6000),
    ]
    rep = run(TripQuery(origin="MOW", destination="Anywhere", nights_min=5, nights_max=5),
              FakeFlights(tickets), FakeHotels({"TBS": 20.0}))
    assert [r.destination_code for r in rep.results] == ["TBS"]


def test_country_origin_searches_several_cities_and_airport_change_warning():
    tickets = [
        ticket("LED", "IST", D(11, 10), 4000, d_ap="SAW"),
        ticket("IST", "LED", D(11, 15), 4000, o_ap="IST"),
        ticket("MOW", "IST", D(11, 10), 9000), ticket("IST", "MOW", D(11, 15), 9000),
    ]
    rep = run(TripQuery(origin="Russia", destination="IST", nights_min=5, nights_max=5),
              FakeFlights(tickets), FakeHotels(None))
    r = rep.results[0]
    assert r.origin_code == "LED" and r.flightTotal == 8000
    assert any("разные аэропорты" in w for w in r.warnings)


def test_stale_prices_are_skipped_by_freshness_filter():
    stale = ticket("MOW", "IST", D(11, 10), 3000, fn="old").model_copy(update={"price_seen_on": TODAY - timedelta(days=6)})
    fresh = ticket("MOW", "IST", D(11, 10), 5000, fn="new").model_copy(update={"price_seen_on": TODAY})
    back = ticket("IST", "MOW", D(11, 15), 4000).model_copy(update={"price_seen_on": TODAY - timedelta(days=1)})
    q = TripQuery(origin="MOW", destination="IST", nights_min=5, nights_max=5, max_price_age_days=2)
    rep = run(q, FakeFlights([stale, fresh, back]), FakeHotels(None))
    assert rep.results[0].outbound.flight_number == "new" and rep.results[0].flightTotal == 9000
    assert any("Пропущено" in l for l in rep.limits)
    rep = run(q.model_copy(update={"max_price_age_days": 7}), FakeFlights([stale, fresh, back]), FakeHotels(None))
    assert rep.results[0].outbound.flight_number == "old"


def test_unknown_place_is_an_input_error():
    from cheaptrip.search import SearchInputError
    with pytest.raises(SearchInputError):
        run(TripQuery(origin="Нарния", destination="Anywhere"), FakeFlights([]), FakeHotels(None))


def test_excluded_destination_never_appears():
    tickets = [
        ticket("MOW", "IST", D(11, 10), 6000), ticket("IST", "MOW", D(11, 15), 6000),
        ticket("MOW", "TBS", D(11, 10), 3000), ticket("TBS", "MOW", D(11, 15), 3000),
    ]
    q = TripQuery(origin="MOW", destination="Anywhere", nights_min=5, nights_max=5, exclude_destinations=["Тбилиси"])
    rep = run(q, FakeFlights(tickets), FakeHotels(None))
    assert [r.destination_code for r in rep.results] == ["IST"]
    q = q.model_copy(update={"exclude_destinations": ["Грузия"]})  # a whole country works too
    assert [r.destination_code for r in run(q, FakeFlights(tickets), FakeHotels(None)).results] == ["IST"]


def test_origin_list_and_excluded_origin():
    tickets = [
        ticket("LED", "IST", D(11, 10), 3000), ticket("IST", "LED", D(11, 15), 3000),
        ticket("MOW", "IST", D(11, 10), 5000), ticket("IST", "MOW", D(11, 15), 5000),
    ]
    rep = run(TripQuery(origin="Москва, Санкт-Петербург", destination="Стамбул", nights_min=5, nights_max=5),
              FakeFlights(tickets), FakeHotels(None))
    assert rep.results[0].origin_code == "LED"
    rep = run(TripQuery(origin="Россия", destination="Стамбул", nights_min=5, nights_max=5,
                        exclude_origins=["Санкт-Петербург"]), FakeFlights(tickets), FakeHotels(None))
    assert rep.results[0].origin_code == "MOW"


def test_european_part_of_russia_excludes_cities_beyond_urals():
    TZ["SVX"] = "Asia/Yekaterinburg"
    tickets = [
        ticket("SVX", "IST", D(11, 10), 2000), ticket("IST", "SVX", D(11, 15), 2000),
        ticket("MOW", "IST", D(11, 10), 5000), ticket("IST", "MOW", D(11, 15), 5000),
    ]
    rep = run(TripQuery(origin="Европейская часть России", destination="Стамбул", nights_min=5, nights_max=5),
              FakeFlights(tickets), FakeHotels(None))
    assert [r.origin_code for r in rep.results] == ["MOW"]


def test_unknown_item_in_list_names_it():
    from cheaptrip.search import SearchInputError
    with pytest.raises(SearchInputError, match="Нарния"):
        run(TripQuery(origin="Москва, Нарния", destination="Anywhere"), FakeFlights([]), FakeHotels(None))


def test_hotel_price_only_applies_to_the_dates_it_was_quoted_for():
    # Three stays of different length; the provider quotes per stay, never copied across lengths.
    tickets = [
        ticket("MOW", "IST", D(11, 10), 5000),
        ticket("IST", "MOW", D(11, 13), 4000), ticket("IST", "MOW", D(11, 15), 4500),
        ticket("IST", "MOW", D(11, 17), 4200),
    ]
    rep = run(TripQuery(origin="MOW", destination="IST", nights_min=3, nights_max=7),
              FakeFlights(tickets), FakeHotels({"IST": 10.0}))
    for r in [*rep.results, *rep.alternatives]:
        if r.total_includes_hotel:
            assert r.hotelTotal == r.nights * 10 * 100
    assert rep.results[0].total_includes_hotel


def test_city_without_hotel_data_stays_listed_without_hotel():
    tickets = [ticket("MOW", "TBS", D(11, 10), 3000), ticket("TBS", "MOW", D(11, 15), 3000)]

    class NoData(FakeHotels):
        async def searchHotels(self, destination, *a, **k):
            return HotelSearchResult(status="unavailable", provider=self.name, message="Hotel data unavailable")

    rep = run(TripQuery(origin="MOW", destination="TBS", nights_min=5, nights_max=5),
              FakeFlights(tickets), NoData({}))
    r = rep.results[0]
    assert not r.total_includes_hotel and r.hotel_status == "unavailable" and r.tripTotal == 6000


def test_destination_with_outbound_but_no_matching_return_does_not_crash():
    # IST has a flight out but no return within the trip length; TBS is a full trip.
    tickets = [
        ticket("MOW", "IST", D(11, 10), 3000), ticket("IST", "MOW", D(12, 20), 3000),
        ticket("MOW", "TBS", D(11, 10), 5000), ticket("TBS", "MOW", D(11, 15), 5000),
    ]
    rep = run(TripQuery(origin="MOW", destination="Anywhere", nights_min=5, nights_max=5),
              FakeFlights(tickets), FakeHotels({"TBS": 20.0, "IST": 20.0}))
    assert [r.destination_code for r in rep.results] == ["TBS"]


def test_state_as_destination_and_over_budget_message():
    TZ["LAX"] = "America/Los_Angeles"
    TZ["SFO"] = "America/Los_Angeles"
    tickets = [
        ticket("MOW", "LAX", D(11, 10), 60000, stops=1), ticket("LAX", "MOW", D(11, 17), 55000, stops=1),
        ticket("MOW", "SFO", D(11, 10), 70000, stops=1), ticket("SFO", "MOW", D(11, 17), 50000, stops=1),
        ticket("MOW", "IST", D(11, 10), 5000), ticket("IST", "MOW", D(11, 17), 5000),
    ]
    q = TripQuery(origin="Москва", destination="Калифорния", nights_min=7, nights_max=7)
    rep = run(q, FakeFlights(tickets), FakeHotels(None))
    assert [r.destination_code for r in rep.results] == ["LAX", "SFO"]  # Istanbul is not in California
    rep = run(q.model_copy(update={"total_budget": 100000}), FakeFlights(tickets), FakeHotels(None))
    assert rep.results == []
    assert any("₽115 000" in w and "Показывать варианты дороже бюджета" in w for w in rep.warnings)


def test_total_budget_is_never_met_by_leaving_the_hotel_out():
    # Three stays in Istanbul. Only 2 get hotel-priced (HOTEL_STAYS_PER_DESTINATION=2), both over budget;
    # the third, unpriced one must NOT appear as "within budget" just because its hotel is missing.
    tickets = [
        ticket("MOW", "IST", D(11, 10), 40000),
        ticket("IST", "MOW", D(11, 13), 40000), ticket("IST", "MOW", D(11, 15), 41000),
        ticket("IST", "MOW", D(11, 17), 42000),
    ]
    q = TripQuery(origin="MOW", destination="IST", nights_min=3, nights_max=7, total_budget=85000)
    rep = run(q, FakeFlights(tickets), FakeHotels({"IST": 100.0}))
    assert rep.results == []
    assert any("не проверялся" in w for w in rep.warnings)
    assert any("самый дешёвый стоит ₽110 000" in w for w in rep.warnings)  # 80 000 + 3 × $100


def test_trip_length_outside_2_to_30_is_rejected():
    from cheaptrip.search import SearchInputError
    for lo, hi in ((1, 5), (5, 31)):
        with pytest.raises(SearchInputError, match="от 2 до 30"):
            run(TripQuery(origin="MOW", destination="IST", nights_min=lo, nights_max=hi), FakeFlights([]), FakeHotels(None))


def test_flexible_covers_up_to_30_nights():
    tickets = [ticket("MOW", "IST", D(11, 1), 5000), ticket("IST", "MOW", D(11, 29), 1000)]  # 28 nights
    rep = run(TripQuery(origin="MOW", destination="IST"), FakeFlights(tickets), FakeHotels(None))
    assert rep.results and rep.results[0].nights == 28


def test_only_a_minimum_means_from_that_many_nights():
    tickets = [ticket("MOW", "IST", D(11, 1), 3000),
               ticket("IST", "MOW", D(11, 3), 1000),   # 2 nights: cheapest, but below the minimum
               ticket("IST", "MOW", D(11, 21), 2000)]  # 20 nights
    rep = run(TripQuery(origin="MOW", destination="IST", nights_min=4), FakeFlights(tickets), FakeHotels(None))
    assert rep.results and rep.results[0].nights == 20
    assert rep.interpreted["tripLength"] == "4–30 ночей"
