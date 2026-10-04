"""Telegram alerts: linking by /start <code>, alerts on real changes only, debouncing, unpin per browser."""

import asyncio
import json
from datetime import date, timedelta

import httpx
from fastapi.testclient import TestClient

from cheaptrip import api
from cheaptrip.store import Store
from cheaptrip.telegram import TelegramBot, valid_code, webhook_secret
from cheaptrip.watcher import Watcher
from test_search import FakeFlights, ticket

CODE = "AbCdEfGhIjKlMnOpQrStUvWxYz012345"
CHAT = 555
SOON = date.today() + timedelta(days=60)


class FakeTelegram:
    """Records every Bot API call; sendMessage answers with `send_error` when set."""

    def __init__(self):
        self.sent: list[dict] = []
        self.send_error: tuple[int, str] | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        if method == "sendMessage":
            if self.send_error:
                code, text = self.send_error
                return httpx.Response(code, json={"ok": False, "error_code": code, "description": text})
            self.sent.append(body)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": len(self.sent)}})
        return httpx.Response(200, json={"ok": True, "result": True})


def setup(tmp_path, *, confirm=0, interval=0):
    mine = ticket("MOW", "IST", SOON, 7500, stops=1, fn="2")
    other = ticket("MOW", "IST", SOON, 9900, fn="7", hour=15)
    store = Store(tmp_path / "t.db")
    store.track_search("s1", [mine])
    store.pin([mine.watch_key])
    flights = FakeFlights([mine, other])
    tg = FakeTelegram()
    bot = TelegramBot(store, "123:TEST", confirm_seconds=confirm, min_interval_seconds=interval)
    bot.client = httpx.AsyncClient(transport=httpx.MockTransport(tg.handler))
    bot.state.update(bot="cheaptrip_test_bot", mode="polling")
    return store, flights, Watcher(store, interval=10, max_requests=50), bot, tg, mine, other


def start(bot, code=CODE, text=None):
    update = {"update_id": 1, "message": {"chat": {"id": CHAT, "type": "private"}, "from": {"language_code": "ru"},
                                          "text": text or f"/start {code}"}}
    asyncio.run(bot.handle_update(update))


def test_link_then_price_drop_is_sent_once(tmp_path):
    store, flights, watcher, bot, tg, mine, other = setup(tmp_path)
    store.tg_subscribe(CODE, [mine.watch_key])
    start(bot)
    assert store.tg_is_linked(CODE)
    assert "Уведомления подключены" in tg.sent[0]["text"] and "₽7 500" in tg.sent[0]["text"]

    asyncio.run(watcher.cycle(flights))  # nothing changed
    assert asyncio.run(bot.notify_once()) == 0

    flights.tickets = [mine.model_copy(update={"price": 7000}), other]
    asyncio.run(watcher.cycle(flights))
    assert asyncio.run(bot.notify_once()) == 1
    text = tg.sent[-1]["text"]
    assert "Цена снизилась: ₽7 500 → <b>₽7 000</b> (−₽500)" in text
    assert "https://example.test/" in text and tg.sent[-1]["chat_id"] == CHAT
    assert asyncio.run(bot.notify_once()) == 0  # told once


def test_gone_and_back_with_confirmation_delay(tmp_path):
    store, flights, watcher, bot, tg, mine, other = setup(tmp_path, confirm=3600)
    store.tg_subscribe(CODE, [mine.watch_key])
    start(bot)
    flights.tickets = [other]
    asyncio.run(watcher.cycle(flights))
    assert asyncio.run(bot.notify_once()) == 0  # a fresh change has to hold first (cache flicker)

    bot.confirm_seconds = 0
    assert asyncio.run(bot.notify_once()) == 1
    text = tg.sent[-1]["text"]
    assert "Билет пропал из данных Aviasales (был ₽7 500)" in text and "₽9 900" in text

    flights.tickets = [mine.model_copy(update={"price": 8000}), other]
    asyncio.run(watcher.cycle(flights))
    assert asyncio.run(bot.notify_once()) == 1
    assert "Билет снова в продаже: <b>₽8 000</b> (до пропажи был ₽7 500)" in tg.sent[-1]["text"]


def test_flapping_inside_quiet_interval_is_folded(tmp_path):
    store, flights, watcher, bot, tg, mine, other = setup(tmp_path, interval=300)
    store.tg_subscribe(CODE, [mine.watch_key])
    start(bot)
    flights.tickets = [mine.model_copy(update={"price": 8000}), other]
    asyncio.run(watcher.cycle(flights))
    assert asyncio.run(bot.notify_once()) == 1  # first change goes at once

    flights.tickets = [mine.model_copy(update={"price": 8600}), other]
    asyncio.run(watcher.cycle(flights))
    flights.tickets = [mine.model_copy(update={"price": 8000}), other]
    asyncio.run(watcher.cycle(flights))
    bot.min_interval_seconds = 0  # the quiet interval is over: the price is back to what was told
    assert asyncio.run(bot.notify_once()) == 0


def test_nothing_is_sent_to_unlinked_or_blocked_chats(tmp_path):
    store, flights, watcher, bot, tg, mine, other = setup(tmp_path)
    store.tg_subscribe(CODE, [mine.watch_key])
    flights.tickets = [mine.model_copy(update={"price": 7000}), other]
    asyncio.run(watcher.cycle(flights))
    assert asyncio.run(bot.notify_once()) == 0  # this browser never pressed Start

    start(bot)
    flights.tickets = [mine.model_copy(update={"price": 6500}), other]
    asyncio.run(watcher.cycle(flights))
    tg.send_error = (403, "Forbidden: bot was blocked by the user")
    assert asyncio.run(bot.notify_once()) == 0
    assert not store.tg_is_linked(CODE)


def test_stop_and_bad_codes(tmp_path):
    store, flights, watcher, bot, tg, mine, other = setup(tmp_path)
    start(bot, code="x" * 5)  # not a code: just a greeting
    assert not store.tg_is_linked("x" * 5) and "Чтобы подключить" in tg.sent[-1]["text"]
    start(bot)
    start(bot, text="/stop")
    assert not store.tg_is_linked(CODE) and "отключены" in tg.sent[-1]["text"]
    assert not valid_code("bad code with spaces") and not valid_code("<script>") and valid_code(CODE)


def test_unpin_keeps_tickets_other_browsers_watch(tmp_path):
    store, *_rest, mine, _other = setup(tmp_path)
    key, other_code = mine.watch_key, "Z" * 32
    store.tg_subscribe(CODE, [key])
    store.tg_subscribe(other_code, [key])
    store.tg_unsubscribe(CODE, [key])
    store.pin([key], False)
    assert store.snapshot([key])[0]["pinned"] == 1
    store.tg_unsubscribe(other_code, [key])
    store.pin([key], False)
    assert store.snapshot([key])[0]["pinned"] == 0


def test_webhook_needs_the_secret(monkeypatch):
    import dataclasses
    monkeypatch.setattr(api, "settings", dataclasses.replace(api.settings, telegram_bot_token="123:TEST"))
    seen = []

    async def handle(update):
        seen.append(update)

    monkeypatch.setattr(api.bot, "handle_update", handle)
    client = TestClient(api.app)
    assert client.post("/api/tg/webhook", json={"update_id": 1}).status_code == 403
    ok = client.post("/api/tg/webhook", json={"update_id": 2},
                     headers={"X-Telegram-Bot-Api-Secret-Token": webhook_secret("123:TEST")})
    assert ok.status_code == 200 and seen == [{"update_id": 2}]
