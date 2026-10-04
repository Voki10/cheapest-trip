"""Data model. Field names follow the "СТРУКТУРА ДАННЫХ РЕЗУЛЬТАТА" section of the spec."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field


# ── Input ────────────────────────────────────────────────────────────────────
class TripQuery(BaseModel):
    """What the user asked for. None always means "no limit", never a hidden default limit."""

    # A place or a comma-separated list: "Москва, Казань", "Европейская часть России", "Азия".
    origin: str = "Россия"
    destination: str = "Куда угодно"
    # Places that must never appear: "Минск", "Беларусь", "Турция"...
    exclude_origins: list[str] = Field(default_factory=list)
    exclude_destinations: list[str] = Field(default_factory=list)
    # Departure window. Both empty = "Anytime" = next `search_months` months.
    date_from: date | None = None
    date_to: date | None = None
    search_months: int | None = None
    # Trip length in nights. Both empty = "Flexible".
    nights_min: int | None = None
    nights_max: int | None = None
    # Budgets. flight_budget is for the whole flight part (outbound + return), per person.
    flight_budget: float | None = None
    total_budget: float | None = None
    hotel_budget_per_night: float | None = None
    hotel_budget_currency: str = "USD"
    show_above_budget: bool = False
    currency: str = "RUB"
    # Hotel filters.
    min_hotel_rating: float | None = None
    max_distance_km: float | None = None
    private_room: bool | None = None
    breakfast: bool | None = None
    # Other constraints.
    max_stops: int | None = None
    # Only use prices that Aviasales users saw at most N days ago. None = PRICE_MAX_AGE_DAYS from .env.
    max_price_age_days: int | None = None
    include_domestic: bool | None = None  # None = auto (off when FROM is a country/region)
    weather: Literal["warm"] | None = None
    mode: Literal["trip", "destinations"] = "trip"
    lang: Literal["ru", "en"] = "ru"  # language of every text the server returns


# ── Flights ──────────────────────────────────────────────────────────────────
class Segment(BaseModel):
    origin_airport: str
    destination_airport: str
    departure_at: datetime
    arrival_at: datetime | None = None
    airline: str | None = None
    flight_number: str | None = None


class FlightOption(BaseModel):
    """One real one-way itinerary as returned by a FlightProvider."""

    provider: str
    price_source: str
    origin_city: str
    destination_city: str
    origin_airport: str
    destination_airport: str
    departure_at: datetime  # local time at the origin airport (tz-aware)
    arrival_at: datetime | None = None  # local time at the destination airport
    duration_minutes: int | None = None  # door to door, incl. connections
    flight_minutes: int | None = None  # time in the air
    stops: int
    airline: str | None = None
    flight_number: str | None = None
    price: float
    currency: str
    segments: list[Segment] = Field(default_factory=list)
    segments_available: bool = False
    is_self_transfer: bool | None = None  # None = provider does not say
    airport_change: bool | None = None  # None = provider does not say
    booking_url: str
    booking_url_kind: Literal["ticket", "route_search"] = "ticket"
    seller: str | None = None  # agency that sells the ticket, when the provider says
    checked_at: datetime  # when this service received the price from the provider
    price_seen_on: date | None = None  # when the provider's users last saw this price
    expires_at: datetime | None = None

    @property
    def departure_date(self) -> date:
        return self.departure_at.date()

    @computed_field
    @property
    def watch_key(self) -> str:
        """Identity of this exact ticket, used by the price watcher and the live UI."""
        return (f"{self.origin_city}|{self.destination_city}|{self.departure_at.isoformat()}|"
                f"{self.airline or ''}|{self.flight_number or ''}|{self.stops}")

    def same_ticket(self, other: "FlightOption") -> bool:
        return (self.departure_at == other.departure_at and self.flight_number == other.flight_number
                and self.airline == other.airline and self.stops == other.stops)


# ── Hotels ───────────────────────────────────────────────────────────────────
class Fee(BaseModel):
    name: str
    amount: float
    currency: str
    mandatory: bool


class HotelOffer(BaseModel):
    name: str
    price_per_night: float
    total_price: float
    currency: str
    rating: float | None = None  # TripAdvisor scale, out of 5
    reviews: int | None = None
    distance_from_center_km: float | None = None
    private_room: bool | None = None
    breakfast: bool | None = None
    accommodation_type: str | None = None
    fees: list[Fee] = Field(default_factory=list)
    sold_by: str | None = None  # site with the lowest price
    site_prices: list[dict] = Field(default_factory=list)  # [{"site": "Agoda.com", "rate": 112}, …]
    provider: str
    url: str
    checked_at: datetime


class HotelSearchResult(BaseModel):
    status: Literal["ok", "no_results", "unavailable", "error", "not_checked"]
    provider: str
    message: str = ""
    offers: list[HotelOffer] = Field(default_factory=list)


class ProviderStatus(BaseModel):
    name: str
    kind: Literal["flights", "hotels", "fx", "weather"]
    available: bool
    message: str = ""
    limitations: list[str] = Field(default_factory=list)


class FxRate(BaseModel):
    base: str
    quote: str
    rate: float  # 1 base = rate quote
    exchangeRateSource: str
    exchangeRateCheckedAt: datetime
    rate_date: date | None = None


# ── Output ───────────────────────────────────────────────────────────────────
class TripResult(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    destination: str  # city name
    destination_code: str
    country: str
    country_code: str
    flag: str
    region: str | None = None

    origin: str  # city name
    origin_code: str
    originAirport: str
    destinationAirport: str

    departureDate: date
    returnDate: date
    nights: int

    outbound: FlightOption
    return_: FlightOption = Field(alias="return")

    hotel: HotelOffer | None = None
    hotel_status: str = "unavailable"
    hotel_message: str = ""

    currency: str
    flightTotal: float
    hotelTotal: float | None = None
    mandatoryFees: float = 0.0
    optionalFees: float = 0.0
    tripTotal: float
    total_includes_hotel: bool = False

    above_budget: bool = False
    budget_notes: list[str] = Field(default_factory=list)

    provider: str
    checkedAt: datetime
    revalidated: bool = False
    revalidation_note: str = ""

    flightUrl: str
    returnFlightUrl: str
    hotelUrl: str | None = None

    isSelfTransfer: bool | None = None
    airportChange: bool | None = None
    separate_tickets: bool = True  # outbound and return are two one-way tickets
    warnings: list[str] = Field(default_factory=list)
    climate_note: str | None = None


class SearchStats(BaseModel):
    origin_cities_searched: list[str] = Field(default_factory=list)
    origin_cities_available: int = 0
    months_searched: list[str] = Field(default_factory=list)
    destinations_discovered: int = 0
    candidate_destinations: int = 0
    combinations_evaluated: int = 0
    api_requests: int = 0
    api_cached: int = 0
    rate_limited: int = 0
    seconds: float = 0.0


class SearchReport(BaseModel):
    query: TripQuery
    interpreted: dict
    results: list[TripResult]
    alternatives: list[TripResult] = Field(default_factory=list)  # other dates, best destination
    providers: list[ProviderStatus]
    fx: list[FxRate] = Field(default_factory=list)
    stats: SearchStats
    limits: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    generated_at: datetime
