"""Provider interfaces. Any flight or hotel source plugs in by implementing one of these."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from ..models import FlightOption, HotelSearchResult, ProviderStatus


class ProviderError(Exception):
    """The provider could not answer (bad token, outage). Never replaced by made-up data."""


class FlightProvider(ABC):
    name: str

    @abstractmethod
    def status(self) -> ProviderStatus: ...

    @abstractmethod
    async def cheapest_from(self, origin_city: str, month: str) -> list[FlightOption]:
        """Cheapest one-way tickets from a city to any destination, departing in YYYY-MM."""

    @abstractmethod
    async def cheapest_to(self, destination_city: str, month: str) -> list[FlightOption]:
        """Cheapest one-way tickets from any origin to a city, departing in YYYY-MM."""

    @abstractmethod
    async def one_way(self, origin: str, destination: str, departure: str,
                      fresh: bool = False) -> list[FlightOption]:
        """All one-way tickets the provider has for a route, departing in YYYY-MM or on YYYY-MM-DD.

        Every routing the provider returns is kept: direct, 1 stop, 2 stops, 3+ stops.
        fresh=True bypasses the local cache (used to revalidate top results).
        """


class HotelProvider(ABC):
    name: str

    @abstractmethod
    def status(self) -> ProviderStatus: ...

    @abstractmethod
    async def searchHotels(
        self,
        destination: str,
        checkIn: date,
        checkOut: date,
        maxPricePerNight: float | None,
        maxPriceCurrency: str,
        rating: float | None,
        maxDistance: float | None,
        privateRoom: bool | None,
        breakfast: bool | None,
    ) -> HotelSearchResult:
        """Real, bookable offers for the stay; status explains an empty answer."""

    async def typical_min_price(self, destination: str, maxPricePerNight: float | None, maxPriceCurrency: str,
                                rating: float | None, maxDistance: float | None,
                                privateRoom: bool | None) -> tuple[float, str] | None:
        """Usual lowest nightly price in the city, if the provider knows it. Used only to choose which
        (destination, dates) to price for real — never shown as a price."""
        return None
