"""Exchange rates from the Central Bank of Russia daily XML. No hardcoded rates anywhere."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime

from .http import HttpClient, HttpError
from .models import FxRate

CBR_URL = "https://www.cbr.ru/scripts/XML_daily.asp"
CBR_SOURCE = "Central Bank of Russia (cbr.ru/scripts/XML_daily.asp)"
MIRROR_URL = "https://www.cbr-xml-daily.ru/daily_json.js"
MIRROR_SOURCE = "Central Bank of Russia rates via mirror cbr-xml-daily.ru"


class FxUnavailable(Exception):
    pass


class Fx:
    """Converts between currencies through RUB using the CBR official daily rates."""

    def __init__(self, http: HttpClient):
        self.http = http
        self._rub_per_unit: dict[str, float] | None = None
        self._checked_at: datetime | None = None
        self._rate_date = None
        self.source = CBR_SOURCE
        self.used: dict[tuple[str, str], FxRate] = {}

    async def load(self) -> None:
        if self._rub_per_unit is not None:
            return
        try:
            await self._load_cbr()
        except (HttpError, ET.ParseError) as exc:
            # cbr.ru is sometimes closed to servers abroad; the mirror republishes the same daily rates.
            try:
                await self._load_mirror()
            except (HttpError, KeyError, TypeError, ValueError) as exc2:
                raise FxUnavailable(f"CBR exchange rates unavailable: {exc}; mirror: {exc2}") from exc2

    async def _load_cbr(self) -> None:
        fetched = await self.http.get_text(CBR_URL, cache_seconds=3600, encoding="windows-1251")
        # The declared encoding is windows-1251; the text is already decoded, so drop the header.
        body = fetched.data.split("?>", 1)[-1]
        root = ET.fromstring(body)
        rates = {"RUB": 1.0}
        for v in root.findall("Valute"):
            code = v.findtext("CharCode")
            nominal = float((v.findtext("Nominal") or "1").replace(",", "."))
            value = float((v.findtext("Value") or "0").replace(",", "."))
            if code and value > 0:
                rates[code] = value / nominal
        if len(rates) < 10:
            raise ET.ParseError("CBR answer has no rates")
        self._set(rates, fetched.fetched_at, CBR_SOURCE)
        try:
            self._rate_date = datetime.strptime(root.get("Date", ""), "%d.%m.%Y").date()
        except ValueError:
            self._rate_date = None

    async def _load_mirror(self) -> None:
        fetched = await self.http.get_json(MIRROR_URL, cache_seconds=3600)
        rates = {"RUB": 1.0}
        for code, v in fetched.data["Valute"].items():
            if v.get("Value") and v.get("Nominal"):
                rates[code] = float(v["Value"]) / float(v["Nominal"])
        self._set(rates, fetched.fetched_at, MIRROR_SOURCE)
        try:
            self._rate_date = datetime.fromisoformat(fetched.data["Date"]).date()
        except (KeyError, ValueError):
            self._rate_date = None

    def _set(self, rates: dict[str, float], checked_at, source: str) -> None:
        self._rub_per_unit = rates
        self._checked_at = checked_at
        self.source = source

    def supports(self, currency: str) -> bool:
        return self._rub_per_unit is not None and currency.upper() in self._rub_per_unit

    def convert(self, amount: float, src: str, dst: str) -> float:
        src, dst = src.upper(), dst.upper()
        if src == dst:
            return amount
        if self._rub_per_unit is None:
            raise FxUnavailable("exchange rates not loaded")
        if src not in self._rub_per_unit or dst not in self._rub_per_unit:
            raise FxUnavailable(f"no CBR rate for {src}/{dst}")
        rate = self._rub_per_unit[src] / self._rub_per_unit[dst]
        self.used[(src, dst)] = FxRate(
            base=src, quote=dst, rate=round(rate, 6),
            exchangeRateSource=self.source,
            exchangeRateCheckedAt=self._checked_at,
            rate_date=self._rate_date,
        )
        return amount * rate
