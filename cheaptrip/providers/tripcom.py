"""Hotels from Trip.com.

Trip.com's hotel price API is partner-only. Until partner access (and its API documentation) is
in place this provider reports itself as unavailable. It never invents a hotel, price, rating,
address or link — the search then shows "Hotel provider connection required".
"""

from __future__ import annotations

from datetime import date

from ..models import HotelSearchResult, ProviderStatus
from ..i18n import tr
from .base import HotelProvider

NAME = "Trip.com"


class TripComProvider(HotelProvider):
    name = NAME

    def __init__(self, api_key: str = "", api_secret: str = "", lang: str = "ru"):
        self.lang = lang
        self.api_key = api_key
        self.api_secret = api_secret

    def _unavailable_reason(self) -> str:
        return tr("tripcom_no_client" if self.api_key else "tripcom_no_key", self.lang)

    def status(self) -> ProviderStatus:
        return ProviderStatus(name=self.name, kind="hotels", available=False,
                              message=self._unavailable_reason())

    async def searchHotels(self, destination: str, checkIn: date, checkOut: date,
                           maxPricePerNight: float | None, maxPriceCurrency: str,
                           rating: float | None, maxDistance: float | None,
                           privateRoom: bool | None, breakfast: bool | None) -> HotelSearchResult:
        return HotelSearchResult(status="unavailable", provider=self.name,
                                 message=self._unavailable_reason())
