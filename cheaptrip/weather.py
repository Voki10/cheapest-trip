"""Climate check for "warm" requests: real observed temperatures (Open-Meteo ERA5 archive)
for the same calendar window one year earlier. No climate guesses from a hand-written table."""

from __future__ import annotations

from datetime import date, timedelta

from .http import HttpClient, HttpError

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
SOURCE = "Open-Meteo historical archive (ERA5), same dates last year"
WARM_MIN_MEAN_C = 18.0
BATCH = 25


def _last_year(d: date) -> date:
    try:
        return d.replace(year=d.year - 1)
    except ValueError:  # 29 Feb
        return d.replace(year=d.year - 1, day=28)


async def mean_temperatures(
    http: HttpClient, coords: dict[str, tuple[float, float]], start: date, end: date
) -> dict[str, float]:
    """City code -> mean daily temperature (°C) over [start, end] shifted back one year."""
    s, e = _last_year(start), _last_year(end)
    latest = date.today() - timedelta(days=7)  # archive lags a few days
    if e > latest:
        e = latest
    if s > e:
        s = e - timedelta(days=30)
    codes = [c for c in coords if coords[c][0] is not None and coords[c][1] is not None]
    out: dict[str, float] = {}
    for i in range(0, len(codes), BATCH):
        chunk = codes[i:i + BATCH]
        params = {
            "latitude": ",".join(f"{coords[c][0]:.4f}" for c in chunk),
            "longitude": ",".join(f"{coords[c][1]:.4f}" for c in chunk),
            "start_date": s.isoformat(),
            "end_date": e.isoformat(),
            "daily": "temperature_2m_mean",
            "timezone": "GMT",
        }
        try:
            fetched = await http.get_json(ARCHIVE_URL, params, cache_seconds=7 * 24 * 3600)
        except HttpError:
            continue
        payload = fetched.data if isinstance(fetched.data, list) else [fetched.data]
        for code, loc in zip(chunk, payload):
            temps = [t for t in (loc.get("daily") or {}).get("temperature_2m_mean") or [] if t is not None]
            if temps:
                out[code] = sum(temps) / len(temps)
    return out
