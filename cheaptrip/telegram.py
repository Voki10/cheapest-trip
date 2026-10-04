"""Telegram alerts for watched tickets.

Linking: every browser keeps a random secret code; the page's button opens t.me/<bot>?start=<code>
and the bot's /start links that code to the chat. The tickets the browser watches (📌) are
subscriptions of that code. Every few seconds the notifier compares each subscribed leg with the
state the chat was last told about and sends what changed: price up/down, gone from the data, back.
Only what the watcher really saw is reported — nothing is estimated.

Updates arrive by webhook when PUBLIC_URL is set (the server copy of the site) and by long polling
otherwise (a copy running on someone's computer). A local copy never removes the server's webhook.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import logging
import re
from datetime import datetime

import httpx

from .geo import get_geo
from .i18n import norm_lang, stops, tr
from .search import fmt_money
from .store import Store

log = logging.getLogger(__name__)
API = "https://api.telegram.org"
CODE_RE = re.compile(r"[A-Za-z0-9_-]{16,64}")
MAX_LEGS_PER_MESSAGE = 10
MAX_LIST = 25
MONTHS = {
    "ru": ["янв.", "февр.", "марта", "апр.", "мая", "июня", "июля", "авг.", "сент.", "окт.", "нояб.", "дек."],
    "en": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
}


class TelegramError(Exception):
    def __init__(self, code: int, description: str = "", retry_after: float | None = None):
        super().__init__(f"{code} {description}".strip())
        self.code, self.description, self.retry_after = code, description, retry_after


def webhook_secret(token: str) -> str:
    """Secret Telegram sends back with every webhook call, so nobody else can post fake updates."""
    return hashlib.sha256(f"cheaptrip-webhook:{token}".encode()).hexdigest()[:48]


def valid_code(code: str) -> bool:
    return bool(CODE_RE.fullmatch(code or ""))


# ── message texts ────────────────────────────────────────────────────────────
def _when(departure_at: str, lang: str) -> str:
    d = datetime.fromisoformat(departure_at)  # local time at the departure airport
    return f"{d.day} {MONTHS[norm_lang(lang)][d.month - 1]} {d.year}, {d:%H:%M}"


def _leg_title(leg: dict, lang: str) -> str:
    geo = get_geo()
    route = f"{geo.city_name(leg['origin'], lang)} → {geo.city_name(leg['destination'], lang)}"
    flight = " ".join(x for x in (leg.get("airline"), leg.get("flight_number")) if x)
    airports = f"{leg.get('origin_airport') or leg['origin']}→{leg.get('destination_airport') or leg['destination']}"
    details = " · ".join(x for x in (_when(leg["departure_at"], lang), airports, flight, stops(leg["stops"], lang)) if x)
    return f"✈️ <b>{html.escape(route)}</b>\n{html.escape(details)}"


def _link(url: str | None, text: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{html.escape(text)}</a>' if url else html.escape(text)


def change_text(row: dict, lang: str) -> str:
    """One leg's change since the chat was last told (a row from Store.tg_due)."""
    cur = row["currency"]
    money = lambda v: fmt_money(v, cur, lang)  # noqa: E731
    old = row.get("notified_price")
    if row["status"] == "gone":
        line = tr("tg_gone", lang, old=money(old)) if old is not None else tr("tg_gone_plain", lang)
        if row.get("alt_price") is not None:
            line += "\n" + tr("tg_gone_alt", lang, price=_link(row.get("alt_url"), money(row["alt_price"])))
    elif row.get("notified_status") == "gone":
        line = (tr("tg_back_was", lang, price=money(row["price"]), old=money(old)) if old is not None
                else tr("tg_back", lang, price=money(row["price"])))
    else:
        diff = row["price"] - old
        sign = "+" if diff > 0 else "−"
        line = tr("tg_up" if diff > 0 else "tg_down", lang, old=money(old), new=money(row["price"]),
                  diff=sign + money(abs(diff)))
    ticket = "" if row["status"] == "gone" else "\n" + _link(row.get("booking_url"), tr("tg_open", lang))
    return f"{_leg_title(row, lang)}\n{line}{ticket}"


def changes_message(rows: list[dict], lang: str) -> str:
    return "\n\n".join(change_text(r, lang) for r in rows) + "\n\n<i>" + html.escape(tr("tg_footer", lang)) + "</i>"


def list_message(legs: list[dict], lang: str) -> str:
    if not legs:
        return html.escape(tr("tg_list_empty", lang))
    lines = [html.escape(tr("tg_list_head", lang))]
    for leg in legs[:MAX_LIST]:
        state = (html.escape(tr("tg_list_gone", lang)) if leg["status"] == "gone"
                 else _link(leg.get("booking_url"), fmt_money(leg["price"], leg["currency"], lang)))
        lines.append(f"\n{_leg_title(leg, lang)}\n{state}")
    if len(legs) > MAX_LIST:
        lines.append("\n" + html.escape(tr("tg_list_more", lang, n=len(legs) - MAX_LIST)))
    return "\n".join(lines)


# ── the bot ──────────────────────────────────────────────────────────────────
class TelegramBot:
    def __init__(self, store: Store, token: str, public_url: str = "", *, confirm_seconds: float = 60,
                 min_interval_seconds: float = 300, watch_days: int = 7, notify_every: float = 10):
        self.store = store
        self.token = token
        self.public_url = public_url.rstrip("/")
        self.confirm_seconds = confirm_seconds
        self.min_interval_seconds = min_interval_seconds
        self.watch_days = watch_days
        self.notify_every = notify_every
        self.client: httpx.AsyncClient | None = None
        self.pending_lang: dict[str, str] = {}  # code → page language, until /start arrives
        # mode: None (connecting) | webhook | polling | elsewhere (another copy of the site gets the updates)
        self.state: dict = {"configured": bool(token), "bot": None, "mode": None, "error": None, "sent": 0}

    @property
    def ready(self) -> bool:
        return bool(self.state["bot"]) and self.state["mode"] in ("webhook", "polling")

    def public_state(self) -> dict:
        return {"enabled": self.ready, "bot": self.state["bot"] if self.ready else None}

    async def call(self, method: str, payload: dict | None = None, timeout: float = 20) -> dict | list | bool:
        assert self.client is not None
        try:
            r = await self.client.post(f"{API}/bot{self.token}/{method}", json=payload or {}, timeout=timeout)
            data = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            # The request URL contains the token: report only the kind of failure.
            raise TelegramError(0, exc.__class__.__name__) from None
        if not data.get("ok"):
            raise TelegramError(int(data.get("error_code") or r.status_code), str(data.get("description") or ""),
                                (data.get("parameters") or {}).get("retry_after"))
        return data["result"]

    async def send(self, chat_id: int, text: str) -> None:
        await self.call("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                                        "link_preview_options": {"is_disabled": True}})

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def run(self) -> None:
        if not self.token:
            return
        async with httpx.AsyncClient() as client:
            self.client = client
            await self._connect()
            loops = [self._notify_loop()]
            if not self.public_url:
                loops.append(self._poll_loop())
            await asyncio.gather(*loops)

    async def _connect(self) -> None:
        while True:
            try:
                me = await self.call("getMe")
                self.state["bot"] = me["username"]
                for lang in ("ru", "en"):
                    await self.call("setMyCommands", {
                        "commands": [{"command": "list", "description": tr("tg_cmd_list", lang)},
                                     {"command": "stop", "description": tr("tg_cmd_stop", lang)}],
                        **({"language_code": "ru"} if lang == "ru" else {})})
                if self.public_url:
                    await self.call("setWebhook", {"url": f"{self.public_url}/api/tg/webhook",
                                                   "secret_token": webhook_secret(self.token),
                                                   "allowed_updates": ["message"]})
                    self.state["mode"] = "webhook"
                else:
                    self.state["mode"] = "polling"
                self.state["error"] = None
                return
            except TelegramError as exc:
                self.state["error"] = "bad_token" if exc.code in (401, 404) else str(exc)
                log.warning("telegram: cannot connect (%s)", self.state["error"])
                await asyncio.sleep(300 if exc.code in (401, 404) else 30)

    async def _poll_loop(self) -> None:
        offset = None
        while True:
            try:
                updates = await self.call("getUpdates", {"timeout": 25, "offset": offset,
                                                         "allowed_updates": ["message"]}, timeout=40)
                self.state["mode"], self.state["error"] = "polling", None
                for update in updates:
                    offset = update["update_id"] + 1
                    try:
                        await self.handle_update(update)
                    except Exception:  # one bad message must not stop the bot
                        log.exception("telegram: update failed")
            except TelegramError as exc:
                if exc.code == 409:
                    # The server copy of the site has a webhook (or another copy is polling): it owns the bot.
                    self.state["mode"] = "elsewhere"
                    await asyncio.sleep(300)
                else:
                    self.state["error"] = str(exc)
                    await asyncio.sleep(10)

    async def _notify_loop(self) -> None:
        while True:
            try:
                await self.notify_once()
            except Exception:
                log.exception("telegram: notify failed")
            await asyncio.sleep(self.notify_every)

    # ── incoming messages ────────────────────────────────────────────────────
    async def handle_update(self, update: dict) -> None:
        msg = update.get("message") or {}
        chat = msg.get("chat") or {}
        text = msg.get("text")
        if chat.get("type") != "private" or not isinstance(text, str):
            return
        chat_id = chat["id"]
        app_lang = "ru" if str((msg.get("from") or {}).get("language_code") or "").startswith(("ru", "uk", "be", "kk")) else "en"
        cmd, _, arg = text.strip().partition(" ")
        cmd, arg = cmd.split("@")[0].lower(), arg.strip()
        lang = self.store.tg_chat_lang(chat_id) or app_lang
        if cmd == "/start" and valid_code(arg):
            lang = self.pending_lang.pop(arg, None) or self.store.tg_code_lang(arg) or lang
            self.store.tg_link(arg, chat_id, lang)
            await self.send(chat_id, html.escape(tr("tg_linked", lang, days=self.watch_days)) + "\n\n"
                            + list_message(self.store.tg_chat_legs(chat_id), lang))
        elif cmd == "/start":
            url = f" ({self.public_url})" if self.public_url else ""
            await self.send(chat_id, html.escape(tr("tg_hello", lang, url=url)))
        elif cmd == "/stop":
            self.store.tg_unlink(chat_id=chat_id)
            await self.send(chat_id, html.escape(tr("tg_stopped", lang)))
        elif cmd == "/list":
            await self.send(chat_id, list_message(self.store.tg_chat_legs(chat_id), lang))
        else:
            await self.send(chat_id, html.escape(tr("tg_help", lang)))

    # ── outgoing alerts ──────────────────────────────────────────────────────
    async def notify_once(self) -> int:
        """Send every due change, one message per chat. Returns the number of messages sent."""
        by_chat: dict[int, list[dict]] = {}
        for row in self.store.tg_due(self.confirm_seconds, self.min_interval_seconds):
            by_chat.setdefault(row["chat_id"], []).append(row)
        sent = 0
        for chat_id, rows in by_chat.items():
            legs: dict[str, dict] = {}  # the same ticket watched from two browsers is told once
            for row in rows:
                legs.setdefault(row["key"], row)
            told = list(legs.values())[:MAX_LEGS_PER_MESSAGE]  # the rest go in the next round
            lang = norm_lang(rows[0]["lang"])
            try:
                await self.send(chat_id, changes_message(told, lang))
            except TelegramError as exc:
                if exc.code == 403 or (exc.code == 400 and "chat not found" in exc.description.lower()):
                    self.store.tg_unlink(chat_id=chat_id)  # the user blocked the bot or deleted the chat
                    continue
                self.state["error"] = str(exc)
                if exc.code == 429:
                    await asyncio.sleep(min(float(exc.retry_after or 5), 60))
                return sent  # network or rate limit: everything stays due for the next round
            keys = {r["key"] for r in told}
            for row in rows:
                if row["key"] in keys:
                    price = row["price"] if row["status"] == "available" else row["notified_price"]
                    self.store.tg_mark(row["code"], row["key"], row["status"], price)
            sent += 1
            self.state["sent"] += 1
            await asyncio.sleep(0.05)
        return sent
