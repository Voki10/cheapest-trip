"""Shared async HTTP: disk cache, concurrency limit, 429/5xx retries, request accounting.

Every cached payload keeps the moment it was really fetched, so a price served from the
cache carries its true "checked at" time instead of pretending to be fresh.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from .config import CACHE_DIR, settings

log = logging.getLogger(__name__)

TIMEOUT = 30.0
MAX_RETRIES = 3
MAX_RETRY_DELAY = 60.0
# Hosts that answer slowly per request but tolerate more parallel calls.
HOST_CONCURRENCY = {"data.xotelo.com": 12}
# Requests per second per host, shared by every HttpClient in the process (searches + watcher).
# Travelpayouts allows 600/min; close to its servers 6 parallel requests would exceed that.
HOST_RATE = {"api.travelpayouts.com": 9.0}
_next_slot: dict[str, float] = {}


def reserve_slot(host: str) -> float:
    """Seconds to wait before the next request to `host` so its rate limit is never exceeded.

    Two limits: our own pace (HOST_RATE) and the quota the server reports (X-Rate-Limit-Remaining),
    which is shared by every process using the same token — another copy of the site, its monitor.
    No lock needed: it runs on the event loop thread and does not await in between."""
    rate = HOST_RATE.get(host)
    if not rate:
        return 0.0
    now = time.monotonic()
    slot = max(now, _next_slot.get(host, 0.0))
    q = _quota.get(host)
    if q is not None:
        if q["reset_at"] <= slot:
            _quota.pop(host)  # the server's window has rolled over
        else:
            if q["remaining"] <= QUOTA_RESERVE:
                slot = q["reset_at"]  # nearly exhausted: wait for the next window
                _quota.pop(host)
            else:
                q["remaining"] -= 1
    _next_slot[host] = slot + 1.0 / rate
    return slot - now


def note_quota(host: str, headers) -> None:
    """Remember what the server says is left of its rate-limit window."""
    try:
        remaining = int(headers["x-rate-limit-remaining"])
        reset = float(headers.get("x-rate-limit-reset") or 60)
    except (KeyError, TypeError, ValueError):
        return
    _quota[host] = {"remaining": remaining, "reset_at": time.monotonic() + max(1.0, min(reset, 60.0))}


def quota_remaining(host: str) -> int | None:
    q = _quota.get(host)
    if q is None or q["reset_at"] <= time.monotonic():
        return None
    return q["remaining"]


_quota: dict[str, dict] = {}
QUOTA_RESERVE = 15  # keep a few requests of every window unused: other copies may be mid-flight

# Query params that must never end up in a cache key or a log line.
SECRET_PARAMS = {"token", "apikey", "api_key", "secret"}


class HttpError(Exception):
    """Request failed after retries, or the server answered with a non-retryable error."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class Fetched:
    data: Any
    fetched_at: datetime
    from_cache: bool


@dataclass
class RequestStats:
    network: int = 0
    cached: int = 0
    errors: int = 0
    rate_limited: int = 0
    by_host: dict[str, int] = field(default_factory=dict)


def _cache_key(url: str, params: dict[str, Any]) -> str:
    public = sorted((k, str(v)) for k, v in params.items() if k.lower() not in SECRET_PARAMS)
    raw = url + "?" + "&".join(f"{k}={v}" for k, v in public)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _retry_after(resp: httpx.Response, attempt: int) -> float:
    raw = resp.headers.get("Retry-After")
    try:
        delay = float(raw) if raw is not None else 0.0
    except ValueError:
        delay = 0.0
    if not math.isfinite(delay) or delay <= 0:
        delay = 2.0 * (attempt + 1)
    return min(delay, MAX_RETRY_DELAY)


class HttpClient:
    def __init__(self, concurrency: int | None = None):
        self._default_limit = concurrency or settings.http_concurrency
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._client: httpx.AsyncClient | None = None
        self.stats = RequestStats()
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    async def __aenter__(self) -> "HttpClient":
        self._client = httpx.AsyncClient(
            timeout=TIMEOUT,
            headers={"Accept-Encoding": "gzip, deflate",
                     "User-Agent": "cheaptrip/1.0 (personal travel price search; python-httpx)"},
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.aclose()

    # ── cache ────────────────────────────────────────────────────────────────
    def _cache_path(self, key: str):
        return CACHE_DIR / "api" / key[:2] / f"{key}.json"

    def _read_cache(self, key: str, ttl_seconds: float) -> Fetched | None:
        path = self._cache_path(key)
        if ttl_seconds <= 0 or not path.exists():
            return None
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if time.time() - entry["ts"] > ttl_seconds:
            return None
        return Fetched(entry["data"], datetime.fromtimestamp(entry["ts"], timezone.utc), True)

    def _write_cache(self, key: str, data: Any, ts: float) -> None:
        path = self._cache_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"ts": ts, "data": data}, ensure_ascii=False), encoding="utf-8")

    # ── requests ─────────────────────────────────────────────────────────────
    async def get_json(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        cache_seconds: float | None = None,
    ) -> Fetched:
        """GET a JSON document. cache_seconds=0 forces a network call (used for revalidation)."""
        params = {k: v for k, v in (params or {}).items() if v is not None}
        ttl = settings.api_cache_minutes * 60 if cache_seconds is None else cache_seconds
        key = _cache_key(url, params)
        cached = self._read_cache(key, ttl)
        if cached is not None:
            self.stats.cached += 1
            return cached
        text = await self._get(url, params, headers)
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise HttpError(f"non-JSON response from {httpx.URL(url).host}") from exc
        ts = time.time()
        self._write_cache(key, data, ts)
        return Fetched(data, datetime.fromtimestamp(ts, timezone.utc), False)

    async def get_text(self, url: str, params: dict[str, Any] | None = None,
                       cache_seconds: float | None = None, encoding: str | None = None) -> Fetched:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        ttl = settings.api_cache_minutes * 60 if cache_seconds is None else cache_seconds
        key = _cache_key(url, params)
        cached = self._read_cache(key, ttl)
        if cached is not None:
            self.stats.cached += 1
            return cached
        text = await self._get(url, params, None, encoding)
        ts = time.time()
        self._write_cache(key, text, ts)
        return Fetched(text, datetime.fromtimestamp(ts, timezone.utc), False)

    async def _get(self, url: str, params: dict[str, Any], headers: dict[str, str] | None,
                   encoding: str | None = None) -> str:
        assert self._client is not None, "use HttpClient as an async context manager"
        host = httpx.URL(url).host
        last_error = "unknown error"
        sem = self._sems.get(host)
        if sem is None:
            sem = self._sems[host] = asyncio.Semaphore(HOST_CONCURRENCY.get(host, self._default_limit))
        for attempt in range(MAX_RETRIES + 1):
            async with sem:
                wait = reserve_slot(host)
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    resp = await self._client.get(url, params=params, headers=headers)
                except httpx.HTTPError as exc:
                    last_error = f"connection error: {exc.__class__.__name__}"
                    resp = None
            self.stats.by_host[host] = self.stats.by_host.get(host, 0) + 1
            if resp is None:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            self.stats.network += 1
            if host in HOST_RATE:
                note_quota(host, resp.headers)
            if resp.status_code == 429:
                self.stats.rate_limited += 1
                delay = _retry_after(resp, attempt)
                if host in HOST_RATE:  # everyone in this process waits, not just this request
                    _next_slot[host] = max(_next_slot.get(host, 0.0), time.monotonic() + delay)
                log.warning("%s rate limited, waiting %.0fs", host, delay)
                await asyncio.sleep(delay)
                last_error = "rate limited (HTTP 429)"
                continue
            if resp.status_code >= 500:
                last_error = f"HTTP {resp.status_code}"
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            if resp.status_code != 200:
                self.stats.errors += 1
                raise HttpError(f"{host}: HTTP {resp.status_code} {resp.text[:200]}", resp.status_code)
            if encoding:
                resp.encoding = encoding
            return resp.text
        self.stats.errors += 1
        raise HttpError(f"{host}: {last_error} after {MAX_RETRIES + 1} attempts")
