"""Keeps the state that must survive a restart in a secret GitHub Gist.

Free hosts like Render wipe the disk on every deploy, restart and spin-down. Watched (pinned)
tickets and Telegram links are small, so they are copied to a secret gist every few minutes and on
shutdown, and loaded back into the fresh database at startup. Search results are not kept: a new
search fetches them again anyway.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone

import httpx

from .store import Store

log = logging.getLogger(__name__)
API = "https://api.github.com"
GIST_FILE = "cheaptrip-state.json"
# Fields that change on every monitor check: a new copy is not worth a gist revision for them alone.
VOLATILE = ("last_checked_at", "checks", "last_error", "touched_at")


def _fingerprint(state: dict) -> str:
    stable = {**state, "legs": [{k: v for k, v in leg.items() if k not in VOLATILE} for leg in state["legs"]]}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, default=str).encode()).hexdigest()


class GistBackup:
    def __init__(self, store: Store, token: str, gist_id: str, every_seconds: float = 300,
                 client: httpx.AsyncClient | None = None):
        self.store = store
        self.token = token
        self.gist_id = gist_id
        self.every_seconds = every_seconds
        self.client = client
        self._saved_fingerprint: str | None = None
        self.state: dict = {"enabled": bool(token and gist_id), "restored": None, "last_saved_at": None,
                            "last_error": None}

    @property
    def enabled(self) -> bool:
        return self.state["enabled"]

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "cheaptrip-backup"}

    async def _request(self, method: str, url: str, **kw) -> httpx.Response:
        if self.client is not None:
            return await self.client.request(method, url, headers=self._headers(), timeout=20, **kw)
        async with httpx.AsyncClient() as client:
            return await client.request(method, url, headers=self._headers(), timeout=20, **kw)

    async def restore(self) -> int:
        """At startup: load the last copy into an empty database. Returns rows restored."""
        if not self.enabled or not self.store.is_empty():
            return 0
        try:
            r = await self._request("GET", f"{API}/gists/{self.gist_id}")
            r.raise_for_status()
            file = r.json().get("files", {}).get(GIST_FILE)
            if not file:
                self.state["restored"] = 0
                return 0
            content = file.get("content") or ""
            if file.get("truncated"):  # the API cuts files over 1 MB: the raw copy is complete
                raw = await self._request("GET", file["raw_url"])
                raw.raise_for_status()
                content = raw.text
            state = json.loads(content)
            n = self.store.import_state(state)
            self._saved_fingerprint = _fingerprint(self.store.export_state())
            self.state["restored"] = n
            log.warning("backup: restored %d rows from the gist", n)
            return n
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            self.state["last_error"] = f"restore: {exc.__class__.__name__}"
            log.warning("backup: restore failed (%s)", exc.__class__.__name__)
            return 0

    async def save(self) -> bool:
        """Write the current state to the gist if it changed. Returns True when written."""
        if not self.enabled:
            return False
        state = self.store.export_state()
        fingerprint = _fingerprint(state)
        if fingerprint == self._saved_fingerprint:
            return False
        body = {"files": {GIST_FILE: {"content": json.dumps(state, ensure_ascii=False, indent=1, default=str)}}}
        try:
            r = await self._request("PATCH", f"{API}/gists/{self.gist_id}", json=body)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            self.state["last_error"] = f"save: {exc.__class__.__name__}"
            log.warning("backup: save failed (%s)", exc.__class__.__name__)
            return False
        self._saved_fingerprint = fingerprint
        self.state["last_saved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.state["last_error"] = None
        return True

    async def run(self) -> None:
        while self.enabled:
            await asyncio.sleep(self.every_seconds)
            await self.save()
