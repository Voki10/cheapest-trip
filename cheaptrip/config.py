"""Settings loaded from .env. Every search limit lives here so it is explicit, not hidden in code."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(__file__).resolve().parent / "data"
CACHE_DIR = DATA_DIR / "cache"

load_dotenv(ROOT / ".env")
# Written by deploy/deploy_vps.sh on the server only (PUBLIC_URL of this copy of the site).
load_dotenv(ROOT / ".env.server", override=True)


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


def _str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip() or default


@dataclass(frozen=True)
class Settings:
    travelpayouts_token: str
    travelpayouts_marker: str
    aviasales_market: str
    tripcom_api_key: str
    tripcom_api_secret: str
    hotel_provider: str
    rapidapi_key: str
    hotel_max_destinations: int
    hotel_stays_per_destination: int
    hotel_candidates: int
    default_currency: str
    default_search_months: int
    flexible_nights_min: int
    flexible_nights_max: int
    max_origin_cities: int
    max_candidate_destinations: int
    max_origins_per_destination: int
    revalidate_top: int
    price_max_age_days: int
    watch_interval_seconds: int
    watch_auto_top: int
    watch_max_requests: int
    watch_keep_hours: int
    watch_pin_days: int
    watch_max_legs: int
    max_concurrent_searches: int
    searches_per_ip_per_hour: int
    telegram_bot_token: str
    public_url: str
    telegram_confirm_seconds: int
    telegram_min_interval_minutes: int
    telegram_max_tickets: int
    keep_awake_minutes: int
    backup_github_token: str
    backup_gist_id: str
    backup_every_minutes: int
    http_concurrency: int
    api_cache_minutes: int
    port: int


def load_settings() -> Settings:
    return Settings(
        travelpayouts_token=_str("TRAVELPAYOUTS_TOKEN"),
        travelpayouts_marker=_str("TRAVELPAYOUTS_MARKER"),
        aviasales_market=_str("AVIASALES_MARKET", "ru"),
        tripcom_api_key=_str("TRIPCOM_API_KEY"),
        tripcom_api_secret=_str("TRIPCOM_API_SECRET"),
        hotel_provider=_str("HOTEL_PROVIDER", "xotelo").lower(),
        rapidapi_key=_str("RAPIDAPI_KEY"),
        hotel_max_destinations=_int("HOTEL_MAX_DESTINATIONS", 8),
        hotel_stays_per_destination=_int("HOTEL_STAYS_PER_DESTINATION", 2),
        hotel_candidates=_int("HOTEL_CANDIDATES", 3),
        default_currency=_str("DEFAULT_CURRENCY", "RUB").upper(),
        default_search_months=_int("DEFAULT_SEARCH_MONTHS", 3),
        flexible_nights_min=_int("FLEXIBLE_NIGHTS_MIN", 2),
        flexible_nights_max=_int("FLEXIBLE_NIGHTS_MAX", 30),
        max_origin_cities=_int("MAX_ORIGIN_CITIES", 25),
        max_candidate_destinations=_int("MAX_CANDIDATE_DESTINATIONS", 20),
        max_origins_per_destination=_int("MAX_ORIGINS_PER_DESTINATION", 2),
        revalidate_top=_int("REVALIDATE_TOP", 5),
        price_max_age_days=_int("PRICE_MAX_AGE_DAYS", 2),
        watch_interval_seconds=_int("WATCH_INTERVAL_SECONDS", 10),
        watch_auto_top=_int("WATCH_AUTO_TOP", 20),
        watch_max_requests=_int("WATCH_MAX_REQUESTS", 25),
        watch_keep_hours=_int("WATCH_KEEP_HOURS", 3),
        watch_pin_days=_int("WATCH_PIN_DAYS", 7),
        watch_max_legs=_int("WATCH_MAX_LEGS", 200),
        max_concurrent_searches=_int("MAX_CONCURRENT_SEARCHES", 2),
        searches_per_ip_per_hour=_int("SEARCHES_PER_IP_PER_HOUR", 20),
        telegram_bot_token=_str("TELEGRAM_BOT_TOKEN"),
        # Render tells every service its own address in RENDER_EXTERNAL_URL.
        public_url=(_str("PUBLIC_URL") or _str("RENDER_EXTERNAL_URL")).rstrip("/"),
        telegram_confirm_seconds=_int("TELEGRAM_CONFIRM_SECONDS", 60),
        telegram_min_interval_minutes=_int("TELEGRAM_MIN_INTERVAL_MINUTES", 5),
        telegram_max_tickets=_int("TELEGRAM_MAX_TICKETS", 40),
        keep_awake_minutes=_int("KEEP_AWAKE_MINUTES", 0),
        backup_github_token=_str("BACKUP_GITHUB_TOKEN"),
        backup_gist_id=_str("BACKUP_GIST_ID"),
        backup_every_minutes=_int("BACKUP_EVERY_MINUTES", 5),
        http_concurrency=_int("HTTP_CONCURRENCY", 6),
        api_cache_minutes=_int("API_CACHE_MINUTES", 60),
        port=_int("PORT", 8770),
    )


settings = load_settings()
