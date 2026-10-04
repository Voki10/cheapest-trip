"""Price watcher: every WATCH_INTERVAL_SECONDS re-checks every watched ticket with the provider
(bypassing the local cache) and records price changes / disappearance in the Store.

One provider request covers every watched ticket on the same route and day, so the cost per cycle
is the number of distinct (origin, destination, day) groups, capped by WATCH_MAX_REQUESTS.
While a full search is running the watcher pauses so the two never fight over the rate limit.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Callable

from .config import settings
from .geo import get_geo
from .http import HttpClient, quota_remaining
from .models import FlightOption
from .providers import AviasalesProvider
from .providers.base import FlightProvider
from .store import Store

log = logging.getLogger(__name__)
WATCH_MIN_QUOTA = 300  # skip a cycle when fewer requests than this are left in the API's minute


class Watcher:
    def __init__(self, store: Store, busy: Callable[[], bool] = lambda: False,
                 interval: float | None = None, max_requests: int | None = None):
        self.store = store
        self.busy = busy
        self.interval = interval or settings.watch_interval_seconds
        self.max_requests = max_requests or settings.watch_max_requests
        self.state: dict = {
            "interval": self.interval, "max_requests": self.max_requests, "enabled": bool(settings.travelpayouts_token),
            "paused": None, "cycles": 0, "last_cycle_at": None, "last_cycle_ms": None,
            "last_requests": 0, "last_groups_total": 0, "last_legs": 0, "last_changed": 0,
            "last_errors": 0, "last_error": None,
        }

    async def run(self) -> None:
        async with HttpClient() as http:
            provider = AviasalesProvider(http, get_geo(), settings.travelpayouts_token,
                                         settings.travelpayouts_marker, settings.aviasales_market)
            while True:
                t0 = time.monotonic()
                try:
                    if not settings.travelpayouts_token:
                        self.state["paused"] = {"key": "no_token"}
                    elif self.busy():
                        self.state["paused"] = {"key": "searching"}
                    elif (left := quota_remaining("api.travelpayouts.com")) is not None and left < WATCH_MIN_QUOTA:
                        # The token's per-minute quota is shared with searches (and other copies of the
                        # site): the monitor yields rather than make a visitor's search wait.
                        self.state["paused"] = {"key": "quota", "left": left}
                    else:
                        self.state["paused"] = None
                        await self.cycle(provider)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # the loop must survive anything a single cycle throws
                    log.exception("watch cycle failed")
                    self.state["last_error"] = f"{exc.__class__.__name__}: {exc}"
                await asyncio.sleep(max(0.5, self.interval - (time.monotonic() - t0)))

    async def cycle(self, provider: FlightProvider) -> dict:
        legs = self.store.active_legs()
        groups: dict[tuple[str, str, str], list] = defaultdict(list)
        for row in legs:
            groups[(row["origin"], row["destination"], row["departure_at"][:10])].append(row)
        # Pinned first, then whatever was checked longest ago: fair rotation when over budget.
        ordered = sorted(groups.items(), key=lambda kv: (
            -max(r["pinned"] for r in kv[1]), min(r["last_checked_at"] or "" for r in kv[1])))
        chosen = ordered[: self.max_requests]
        started = datetime.now(timezone.utc)
        t0 = time.monotonic()
        answers = await asyncio.gather(
            *(provider.one_way(o, d, day, fresh=True) for (o, d, day), _ in chosen), return_exceptions=True)
        changed = errors = 0
        for ((o, d, day), rows), answer in zip(chosen, answers):
            if isinstance(answer, Exception):
                errors += 1
                for row in rows:
                    self.store.record(row["key"], found=None, alt=None, error=str(answer))
                continue
            if not answer:
                # An empty answer for a whole route-day is more likely a provider hiccup than every
                # ticket vanishing at once: keep the last known state and say so.
                errors += 1
                for row in rows:
                    self.store.record(row["key"], found=None, alt=None,
                                      error="empty_answer")
                continue
            for row in rows:
                found, alt = self._match(row, answer)
                if self.store.record(row["key"], found=found, alt=alt):
                    changed += 1
        ms = int((time.monotonic() - t0) * 1000)
        self.store.log_cycle(started, ms, len(chosen), len(legs), changed, errors)
        self.state.update({
            "cycles": self.state["cycles"] + 1, "last_cycle_at": started.isoformat(timespec="seconds"),
            "last_cycle_ms": ms, "last_requests": len(chosen), "last_groups_total": len(groups),
            "last_legs": len(legs), "last_changed": changed, "last_errors": errors, "last_error": None,
        })
        return self.state

    @staticmethod
    def _match(row, answer: list[FlightOption]) -> tuple[FlightOption | None, FlightOption | None]:
        dep = datetime.fromisoformat(row["departure_at"])
        same = [f for f in answer if f.departure_at == dep and f.flight_number == row["flight_number"]
                and f.airline == row["airline"] and f.stops == row["stops"]]
        if same:
            return min(same, key=lambda f: f.price), None
        return None, min(answer, key=lambda f: f.price)
