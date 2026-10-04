"""State backup for free hosts that wipe the disk: export/import and the GitHub Gist copy."""

import asyncio
import json
from datetime import date, timedelta

import httpx

from cheaptrip.api import keep_awake
from cheaptrip.backup import GIST_FILE, GistBackup
from cheaptrip.store import Store
from cheaptrip.watcher import Watcher
from test_search import FakeFlights, ticket

CODE = "AbCdEfGhIjKlMnOpQrStUvWxYz012345"
SOON = date.today() + timedelta(days=60)


class FakeGist:
    def __init__(self, content: str | None = None):
        self.content = content
        self.patches = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer ghp_test"
        if request.method == "PATCH":
            self.patches += 1
            self.content = json.loads(request.content)["files"][GIST_FILE]["content"]
            return httpx.Response(200, json={"id": "g1"})
        files = {GIST_FILE: {"content": self.content, "truncated": False}} if self.content else {}
        return httpx.Response(200, json={"id": "g1", "files": files})


def watched_store(path):
    mine = ticket("MOW", "IST", SOON, 7500, fn="2")
    passing = ticket("MOW", "TBS", SOON, 5000, fn="9")  # found by a search, not watched: not kept
    store = Store(path)
    store.track_search("s1", [mine, passing])
    store.pin([mine.watch_key])
    store.tg_subscribe(CODE, [mine.watch_key])
    store.tg_link(CODE, 555, "ru")
    return store, mine, passing


def gist_backup(store, gist):
    return GistBackup(store, "ghp_test", "g1", client=httpx.AsyncClient(transport=httpx.MockTransport(gist.handler)))


def test_export_import_round_trip(tmp_path):
    store, mine, passing = watched_store(tmp_path / "a.db")
    flights = FakeFlights([mine.model_copy(update={"price": 7100})])
    asyncio.run(Watcher(store, interval=10, max_requests=50).cycle(flights))
    state = store.export_state()
    assert [leg["key"] for leg in state["legs"]] == [mine.watch_key]
    assert state["changes"][0]["kind"] == "price_down"

    fresh = Store(tmp_path / "b.db")
    assert fresh.is_empty()
    fresh.import_state(json.loads(json.dumps(state)))
    assert not fresh.is_empty() and fresh.tg_is_linked(CODE)
    assert fresh.export_state() == state
    assert fresh.tg_chat_legs(555)[0]["price"] == 7100


def test_gist_save_only_on_change_and_restore(tmp_path):
    store, mine, _ = watched_store(tmp_path / "a.db")
    gist = FakeGist()
    backup = gist_backup(store, gist)
    assert asyncio.run(backup.save()) is True
    assert asyncio.run(backup.save()) is False and gist.patches == 1  # nothing new

    flights = FakeFlights([mine.model_copy(update={"price": 7500})])
    asyncio.run(Watcher(store, interval=10, max_requests=50).cycle(flights))
    assert asyncio.run(backup.save()) is False  # only "checked again": not worth a revision
    flights.tickets = [mine.model_copy(update={"price": 6900})]
    asyncio.run(Watcher(store, interval=10, max_requests=50).cycle(flights))
    assert asyncio.run(backup.save()) is True and gist.patches == 2

    restarted = Store(tmp_path / "after_restart.db")  # the host wiped the disk
    assert asyncio.run(gist_backup(restarted, gist).restore()) > 0
    assert restarted.tg_is_linked(CODE) and restarted.tg_chat_legs(555)[0]["price"] == 6900


def test_restore_never_overwrites_a_live_database(tmp_path):
    store, *_ = watched_store(tmp_path / "a.db")
    gist = FakeGist(json.dumps({"version": 1, "legs": [], "changes": [], "tg_links": [], "tg_subs": []}))
    assert asyncio.run(gist_backup(store, gist).restore()) == 0
    assert store.tg_is_linked(CODE)


def test_backup_and_keep_awake_are_off_without_settings(tmp_path):
    store = Store(tmp_path / "a.db")
    off = GistBackup(store, "", "")
    assert not off.enabled and asyncio.run(off.save()) is False and asyncio.run(off.restore()) == 0
    asyncio.run(asyncio.wait_for(keep_awake("", 10), 1))  # returns at once instead of looping
