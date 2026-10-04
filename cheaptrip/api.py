"""FastAPI app: search jobs with live stage progress, NL parsing, place suggestions, provider status."""

from __future__ import annotations

import asyncio
import hmac
import logging
import time
import traceback
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .backup import GistBackup
from .config import DATA_DIR, ROOT, settings
from .fx import Fx
from .geo import get_geo
from .http import HttpClient
from .i18n import norm_lang, tr
from .models import TripQuery
from .nlp import parse
from .providers import AviasalesProvider, TripComProvider, XoteloProvider
from .search import CheapTripSearchService, SearchAborted, SearchInputError
from .store import Store
from .telegram import TelegramBot, valid_code, webhook_secret
from .watcher import Watcher

log = logging.getLogger("cheaptrip")
MAX_JOBS = 50
MAX_WATCH_KEYS = 200
MAX_PIN_KEYS = 4


@dataclass
class Job:
    id: str
    query: TripQuery
    ip: str = ""
    status: str = "running"  # running | done | error
    events: list[dict] = field(default_factory=list)
    report: dict | None = None
    error: str | None = None
    created: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


jobs: dict[str, Job] = {}
store = Store(DATA_DIR / "trips.db")
watcher = Watcher(store, busy=lambda: any(j.status == "running" for j in jobs.values()))
bot = TelegramBot(store, settings.telegram_bot_token, settings.public_url,
                  confirm_seconds=settings.telegram_confirm_seconds,
                  min_interval_seconds=settings.telegram_min_interval_minutes * 60,
                  watch_days=settings.watch_pin_days)
backup = GistBackup(store, settings.backup_github_token, settings.backup_gist_id, settings.backup_every_minutes * 60)
# Public site: every search spends the shared Aviasales quota, so searches are rationed per visitor.
searches_by_ip: dict[str, deque[float]] = defaultdict(deque)


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def admit_search(ip: str, lang: str = "ru") -> None:
    """Raise 429 when the server or this visitor has too many searches going."""
    running = [j for j in jobs.values() if j.status == "running"]
    if len(running) >= settings.max_concurrent_searches:
        raise HTTPException(429, tr("busy_server", lang))
    if any(j.ip == ip for j in running):
        raise HTTPException(429, tr("busy_visitor", lang))
    hits = searches_by_ip[ip]
    now = time.time()
    while hits and now - hits[0] > 3600:
        hits.popleft()
    if len(hits) >= settings.searches_per_ip_per_hour:
        wait = int((3600 - (now - hits[0])) / 60) + 1
        raise HTTPException(429, tr("hourly_quota", lang, n=settings.searches_per_ip_per_hour, wait=wait))
    hits.append(now)


async def keep_awake(url: str, minutes: int) -> None:
    """Free hosts (Render) stop a site after 15 minutes without visitors, and with it the price
    monitor and the Telegram bot. A visit to our own public address every few minutes prevents that."""
    if not url or minutes <= 0:
        return
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            await asyncio.sleep(minutes * 60)
            try:
                await client.get(f"{url}/api/ping")
            except httpx.HTTPError as exc:
                log.warning("keep-awake ping failed: %s", exc.__class__.__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await asyncio.to_thread(get_geo)  # reference data: download once, then from disk
    await backup.restore()  # a fresh disk on a free host: bring back watched tickets and Telegram links
    tasks = [asyncio.create_task(t) for t in (watcher.run(), bot.run(), backup.run(),
                                              keep_awake(settings.public_url, settings.keep_awake_minutes))]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await backup.save()  # the host is stopping us (deploy, restart): keep the latest state


app = FastAPI(title="Cheapest Trip", lifespan=lifespan)


def hotel_provider(http, fx, lang: str = "ru"):
    """HOTEL_PROVIDER=xotelo (default, free) or tripcom (needs partner access)."""
    if settings.hotel_provider == "tripcom":
        return TripComProvider(settings.tripcom_api_key, settings.tripcom_api_secret, lang)
    return XoteloProvider(http, get_geo(), fx, settings.rapidapi_key, settings.hotel_candidates, lang)


def _providers(lang: str = "ru"):
    geo = get_geo()
    flights = AviasalesProvider(None, geo, settings.travelpayouts_token,  # type: ignore[arg-type]
                                settings.travelpayouts_marker, settings.aviasales_market, lang)
    return flights, hotel_provider(None, None, lang)


async def _run(job: Job) -> None:
    try:
        async with HttpClient() as http:
            geo = get_geo()
            lang = norm_lang(job.query.lang)
            flights = AviasalesProvider(http, geo, settings.travelpayouts_token,
                                        settings.travelpayouts_marker, settings.aviasales_market, lang)
            fx = Fx(http)
            service = CheapTripSearchService(geo, flights, hotel_provider(http, fx, lang), fx, http, settings,
                                             progress=job.events.append)
            report = await service.search(job.query)
        watched = [*report.results[: settings.watch_auto_top], *report.alternatives]
        store.track_search(job.id, [leg for r in watched for leg in (r.outbound, r.return_)],
                           keep_seconds=settings.watch_keep_hours * 3600,
                           pin_seconds=settings.watch_pin_days * 86400, max_legs=settings.watch_max_legs)
        job.report = report.model_dump(mode="json", by_alias=True)
        job.status = "done"
    except (SearchInputError, SearchAborted) as exc:
        job.error, job.status = str(exc), "error"
    except Exception as exc:  # surfaced to the UI instead of silently hanging
        log.error("search failed: %s", traceback.format_exc())
        job.error = tr("internal_error", job.query.lang, err=f"{exc.__class__.__name__}: {exc}")
        job.status = "error"


@app.get("/")
async def index():
    return FileResponse(ROOT / "web" / "index.html")


@app.get("/api/ping")
async def ping():
    """Cheap health check for the host and for keep-awake visits."""
    return {"ok": True}


@app.get("/api/status")
async def status(lang: str = "ru"):
    flights, hotels = _providers(norm_lang(lang))
    return {
        "providers": [flights.status().model_dump(), hotels.status().model_dump()],
        "defaults": {
            "currency": settings.default_currency,
            "search_months": settings.default_search_months,
            "flexible_nights": [settings.flexible_nights_min, settings.flexible_nights_max],
        },
        "limits": {
            "MAX_ORIGIN_CITIES": settings.max_origin_cities,
            "MAX_CANDIDATE_DESTINATIONS": settings.max_candidate_destinations,
            "MAX_ORIGINS_PER_DESTINATION": settings.max_origins_per_destination,
            "REVALIDATE_TOP": settings.revalidate_top,
            "PRICE_MAX_AGE_DAYS": settings.price_max_age_days,
            "WATCH_INTERVAL_SECONDS": settings.watch_interval_seconds,
            "WATCH_AUTO_TOP": settings.watch_auto_top,
            "WATCH_MAX_REQUESTS": settings.watch_max_requests,
        },
        "today": date.today().isoformat(),
        "telegram": bot.public_state(),
        "backup": {k: backup.state[k] for k in ("enabled", "restored", "last_saved_at")},
    }


class ParseIn(BaseModel):
    text: str
    lang: str = "ru"


@app.post("/api/parse")
async def parse_text(body: ParseIn):
    query, notes = parse(body.text, get_geo(), lang=norm_lang(body.lang))
    return {"query": query.model_dump(mode="json"), "notes": notes}


@app.get("/api/places")
async def places(q: str = "", lang: str = "ru"):
    return get_geo().suggest(q, lang=norm_lang(lang))


@app.post("/api/search")
async def start_search(query: TripQuery, request: Request):
    ip = client_ip(request)
    admit_search(ip, query.lang)
    if len(jobs) >= MAX_JOBS:
        for old in sorted(jobs.values(), key=lambda j: j.created)[: len(jobs) - MAX_JOBS + 1]:
            if old.status != "running":
                jobs.pop(old.id, None)
    job = Job(id=uuid.uuid4().hex[:12], query=query, ip=ip)
    jobs[job.id] = job
    asyncio.create_task(_run(job))
    return {"job_id": job.id}


class WatchIn(BaseModel):
    keys: list[str] = []
    lang: str = "ru"


@app.post("/api/watch/state")
async def watch_state(body: WatchIn):
    """Monitor state plus the live status of THIS visitor's tickets (the keys on their page)."""
    geo = get_geo()
    lang = norm_lang(body.lang)
    legs = store.snapshot(body.keys[:MAX_WATCH_KEYS])
    for leg in legs:
        leg["origin_name"] = geo.city_name(leg["origin"], lang)
        leg["destination_name"] = geo.city_name(leg["destination"], lang)
    return {"watcher": {**watcher.state, "total_legs": store.active_count()}, "legs": legs}


class PinIn(BaseModel):
    keys: list[str]
    pinned: bool = True
    tg: str = ""  # this browser's Telegram code: its alerts follow its pins


@app.post("/api/watch/pin")
async def watch_pin(body: PinIn):
    keys = body.keys[:MAX_PIN_KEYS]
    if body.pinned:
        updated = store.pin(keys, True)
        if valid_code(body.tg):
            store.tg_subscribe(body.tg, keys, settings.telegram_max_tickets)
        return {"updated": updated}
    if valid_code(body.tg):
        store.tg_unsubscribe(body.tg, keys)
    return {"updated": store.pin(keys, False)}


class TgStateIn(BaseModel):
    code: str
    lang: str = "ru"
    pins: list[str] = []


@app.post("/api/tg/state")
async def tg_state(body: TgStateIn):
    """Is this browser linked to a Telegram chat? Also files pins made before it was linked."""
    if not valid_code(body.code):
        raise HTTPException(400, "bad code")
    lang = norm_lang(body.lang)
    if len(bot.pending_lang) > 1000:
        bot.pending_lang.pop(next(iter(bot.pending_lang)))
    bot.pending_lang[body.code] = lang
    linked = store.tg_is_linked(body.code)
    if linked:
        store.tg_set_lang(body.code, lang)
    if body.pins:
        store.tg_subscribe(body.code, body.pins[:MAX_WATCH_KEYS], settings.telegram_max_tickets)
    return {**bot.public_state(), "linked": linked}


class TgCodeIn(BaseModel):
    code: str


@app.post("/api/tg/unlink")
async def tg_unlink(body: TgCodeIn):
    if not valid_code(body.code):
        raise HTTPException(400, "bad code")
    return {"unlinked": store.tg_unlink(code=body.code)}


@app.post("/api/tg/webhook")
async def tg_webhook(request: Request):
    """Telegram delivers bot messages here on the server copy of the site (PUBLIC_URL set)."""
    if not settings.telegram_bot_token:
        raise HTTPException(404)
    secret = request.headers.get("x-telegram-bot-api-secret-token", "")
    if not hmac.compare_digest(secret, webhook_secret(settings.telegram_bot_token)):
        raise HTTPException(403)
    try:
        await bot.handle_update(await request.json())
    except Exception:  # answer 200 anyway, or Telegram keeps re-sending the same update
        log.exception("telegram webhook update failed")
    return {"ok": True}


@app.get("/api/search/{job_id}")
async def poll(job_id: str, since: int = 0):
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "search not found")
    return {
        "status": job.status,
        "events": job.events[since:],
        "next": len(job.events),
        "report": job.report if job.status == "done" else None,
        "error": job.error,
    }
