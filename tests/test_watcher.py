"""Watcher + Store: price changes, disappearance and return are detected and logged."""

import asyncio

from cheaptrip.store import Store
from cheaptrip.watcher import Watcher
from test_search import D, FakeFlights, ticket


def setup(tmp_path, tickets):
    store = Store(tmp_path / "t.db")
    flights = FakeFlights(tickets)
    watcher = Watcher(store, interval=10, max_requests=50)
    return store, flights, watcher


def legs_by_key(store):
    return {l["key"]: l for l in store.snapshot()}


def test_price_change_gone_and_back(tmp_path):
    mine = ticket("MOW", "IST", D(11, 15), 7500, stops=2, fn="2")
    other = ticket("MOW", "IST", D(11, 15), 9900, stops=0, fn="7", hour=15)
    store, flights, watcher = setup(tmp_path, [mine, other])
    store.track_search("s1", [mine])
    key = mine.watch_key

    asyncio.run(watcher.cycle(flights))
    leg = legs_by_key(store)[key]
    assert leg["status"] == "available" and leg["price"] == 7500 and leg["changes"] == 0

    flights.tickets = [mine.model_copy(update={"price": 8100}), other]
    asyncio.run(watcher.cycle(flights))
    leg = legs_by_key(store)[key]
    assert leg["price"] == 8100 and leg["history"][0]["kind"] == "price_up"
    assert watcher.state["last_changed"] == 1 and watcher.state["last_requests"] == 1

    flights.tickets = [other]
    asyncio.run(watcher.cycle(flights))
    leg = legs_by_key(store)[key]
    assert leg["status"] == "gone" and leg["alt_price"] == 9900
    assert leg["history"][0]["kind"] == "gone"

    flights.tickets = [mine.model_copy(update={"price": 7000}), other]
    asyncio.run(watcher.cycle(flights))
    leg = legs_by_key(store)[key]
    assert leg["status"] == "available" and leg["price"] == 7000 and leg["history"][0]["kind"] == "back"
    assert leg["checks"] == 4 and leg["changes"] == 3


def test_empty_answer_is_an_error_not_gone(tmp_path):
    mine = ticket("MOW", "TBS", D(11, 10), 5000)
    store, flights, watcher = setup(tmp_path, [mine])
    store.track_search("s1", [mine])
    flights.tickets = []
    asyncio.run(watcher.cycle(flights))
    leg = legs_by_key(store)[mine.watch_key]
    assert leg["status"] == "available" and leg["last_error"]


def test_one_request_per_route_day_and_new_search_replaces_unpinned(tmp_path):
    a = ticket("MOW", "IST", D(11, 15), 7500, fn="1")
    b = ticket("MOW", "IST", D(11, 15), 8000, fn="2", hour=18)
    c = ticket("IST", "MOW", D(11, 20), 6800, fn="3")
    store, flights, watcher = setup(tmp_path, [a, b, c])
    store.track_search("s1", [a, b, c])
    asyncio.run(watcher.cycle(flights))
    assert watcher.state["last_requests"] == 2 and watcher.state["last_legs"] == 3

    # Another visitor's search does not wipe this one's tickets...
    d = ticket("MOW", "TBS", D(11, 10), 5000)
    store.track_search("s2", [d])
    assert set(legs_by_key(store)) == {a.watch_key, b.watch_key, c.watch_key, d.watch_key}
    # ...but once the keep window passes, only pinned tickets stay watched.
    store.pin([c.watch_key])
    store.track_search("s3", [], keep_seconds=-1)
    assert set(legs_by_key(store)) == {c.watch_key}
    # A visitor only gets their own tickets back.
    assert [l["key"] for l in store.snapshot([c.watch_key, "nope"])] == [c.watch_key]
    assert store.snapshot([]) == []


def test_watch_list_is_capped(tmp_path):
    tickets = [ticket("MOW", "IST", D(11, 1 + i), 5000 + i, fn=str(i)) for i in range(6)]
    store, _, _ = setup(tmp_path, tickets)
    store.track_search("s1", tickets, max_legs=4)
    assert store.active_count() == 4
