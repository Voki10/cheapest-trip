"""Publish the site on Render's free plan, or update it there. Run from the project folder:

    python deploy/render_setup.py

Reads from .env: DEPLOY_GITHUB_TOKEN (classic token, scopes: repo, gist) and RENDER_API_KEY, then
1. pushes the code to the private GitHub repo <login>/cheapest-trip (never .env, databases, caches, logs);
2. finds or creates the secret gist that keeps watched tickets and Telegram links across restarts
   (Render wipes the disk on every restart; a new gist starts with this computer's watched tickets);
3. creates the Render web service (free plan, Docker, Frankfurt) or updates its settings, deploys,
   and waits until the site answers.
Secrets go only to Render's environment settings, never into the repository.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cheaptrip.backup import GIST_FILE  # noqa: E402
from cheaptrip.config import DATA_DIR  # noqa: E402
from cheaptrip.store import Store  # noqa: E402

REPO = "cheapest-trip"
SERVICE = "cheapest-trip"
REGION = "frankfurt"
GIST_DESCRIPTION = "Cheapest Trip: state backup (watched tickets, Telegram links)"
GH = "https://api.github.com"
RENDER = "https://api.render.com/v1"
# Settings that differ on Render: stay awake, and a lighter monitor for the 0.1-CPU free instance.
RENDER_SETTINGS = {"KEEP_AWAKE_MINUTES": "10", "WATCH_MAX_REQUESTS": "10"}
# Render sets its own port and address; the publishing keys stay on this computer.
NOT_FOR_RENDER = {"PORT", "PUBLIC_URL", "DEPLOY_GITHUB_TOKEN", "RENDER_API_KEY"}
COAUTHOR = "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"


def fail(msg: str) -> None:
    print(f"!! {msg}")
    sys.exit(1)


def git(*args: str, auth: str | None = None) -> subprocess.CompletedProcess:
    cmd = ["git"] + (["-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {auth}"] if auth else [])
    # Never print the command: with `auth` it carries the token.
    return subprocess.run(cmd + list(args), cwd=ROOT, capture_output=True, text=True, encoding="utf-8")


def must(result: subprocess.CompletedProcess, what: str) -> str:
    if result.returncode != 0:
        fail(f"git {what} failed (code {result.returncode}): {(result.stderr or result.stdout).strip()[:500]}")
    return result.stdout


def set_config(key: str, value: str) -> None:
    if git("config", key).stdout.strip() != value:
        must(git("config", key, value), f"config {key}")


class Api:
    def __init__(self, base: str, token: str):
        self.client = httpx.Client(base_url=base, timeout=30, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "cheaptrip-deploy"})

    def call(self, method: str, path: str, ok=(200, 201), **kw) -> httpx.Response:
        r = self.client.request(method, path, **kw)
        if r.status_code not in ok:
            fail(f"{method} {path} → {r.status_code}: {r.text[:500]}")
        return r


def push_code(gh: Api, login: str, user_id: int, token: str) -> bool:
    """Commit everything that changed and push it. Returns True when a new commit went up."""
    if gh.call("GET", f"/repos/{login}/{REPO}", ok=(200, 404)).status_code == 404:
        gh.call("POST", "/user/repos", json={"name": REPO, "private": True,
                                              "description": "Cheapest Trip: the cheapest real whole trip"})
        print(f"== created private repo github.com/{login}/{REPO}")
    if not (ROOT / ".git").exists():
        must(git("init", "-b", "main"), "init")
    set_config("user.name", login)
    set_config("user.email", f"{user_id}+{login}@users.noreply.github.com")
    set_config("core.autocrlf", "false")
    url = f"https://github.com/{login}/{REPO}.git"
    if git("remote", "get-url", "origin").returncode != 0:
        must(git("remote", "add", "origin", url), "remote add")
    else:
        must(git("remote", "set-url", "origin", url), "remote set-url")

    must(git("add", "-A"), "add")
    files = must(git("ls-files", "--cached"), "ls-files").splitlines()
    leaked = [f for f in files if Path(f).name in (".env", ".env.server")
              or f.endswith((".db", ".db-journal", ".db-wal", ".db-shm", ".log")) or "data/cache/" in f]
    if leaked:
        fail(f"refusing to publish private files: {leaked}")
    new_commit = git("diff", "--cached", "--quiet").returncode != 0
    if new_commit:
        first = git("rev-parse", "--verify", "HEAD").returncode != 0
        title = "Cheapest Trip: first upload for Render" if first else "Update Cheapest Trip"
        must(git("commit", "-q", "-m", f"{title}\n\n{COAUTHOR}"), "commit")
    auth = base64.b64encode(f"{login}:{token}".encode()).decode()
    must(git("push", "-q", "origin", "HEAD:main", auth=auth), "push")
    print(f"== code pushed ({len(files)} files){'' if new_commit else ', nothing new'}")
    return new_commit


def ensure_gist(gh: Api) -> str:
    for gist in gh.call("GET", "/gists", params={"per_page": 100}).json():
        if gist.get("description") == GIST_DESCRIPTION:
            print("== state gist found")
            return gist["id"]
    state = Store(DATA_DIR / "trips.db").export_state()
    gist = gh.call("POST", "/gists", json={"description": GIST_DESCRIPTION, "public": False, "files": {
        GIST_FILE: {"content": json.dumps(state, ensure_ascii=False, indent=1, default=str)}}}).json()
    print(f"== created secret state gist (starts with {len(state['legs'])} watched tickets, "
          f"{len(state['tg_links'])} Telegram links from this computer)")
    return gist["id"]


def render_env(env: dict, gist_id: str, github_token: str) -> list[dict]:
    documented = dotenv_values(ROOT / ".env.example")  # every setting of the site, nothing else
    values = {k: v for k, v in env.items() if k in documented and k not in NOT_FOR_RENDER and v}
    values.update(RENDER_SETTINGS, BACKUP_GIST_ID=gist_id, BACKUP_GITHUB_TOKEN=github_token)
    return [{"key": k, "value": v} for k, v in sorted(values.items())]


def latest_deploy(rd: Api, service_id: str) -> dict:
    deploys = rd.call("GET", f"/services/{service_id}/deploys", params={"limit": 1}).json()
    return deploys[0]["deploy"] if deploys else {}


def wait_live(rd: Api, service_id: str, url: str, since: float, previous: str | None = None) -> None:
    """Wait for the newest deploy to go live; `previous` is the deploy that was live before ours."""
    print("== waiting for Render to build and start the site (usually 3–8 minutes)")
    last = None
    for _ in range(120):
        deploy = latest_deploy(rd, service_id)
        status = deploy.get("status") if deploy.get("id") != previous else "waiting for the new deploy"
        if status != last:
            print(f"   deploy: {status}")
            last = status
        if status == "live":
            break
        if status in ("build_failed", "update_failed", "canceled", "pre_deploy_failed"):
            fail(f"deploy {status}: see the logs at https://dashboard.render.com/web/{service_id}")
        time.sleep(15)
    else:
        fail("deploy did not finish in 30 minutes")
    r = httpx.get(f"{url}/api/status", timeout=60)
    tg = r.json().get("telegram") if r.status_code == 200 else None
    print(f"== live: {url}  (status {r.status_code}, telegram {tg}, took {int(time.time() - since)} s)")


def main() -> None:
    env = dotenv_values(ROOT / ".env")
    github_token, render_key = env.get("DEPLOY_GITHUB_TOKEN"), env.get("RENDER_API_KEY")
    if not github_token or not render_key:
        fail("put DEPLOY_GITHUB_TOKEN and RENDER_API_KEY into .env first")
    started = time.time()
    gh, rd = Api(GH, github_token), Api(RENDER, render_key)
    me = gh.call("GET", "/user").json()
    login = me["login"]

    gist_id = ensure_gist(gh)
    owner_id = rd.call("GET", "/owners", params={"limit": 20}).json()[0]["owner"]["id"]
    found = [s["service"] for s in rd.call("GET", "/services", params={"name": SERVICE, "limit": 20}).json()]
    env_vars = render_env(env, gist_id, github_token)
    previous = None
    if found:  # settings first, so the deploy below starts with them
        previous = latest_deploy(rd, found[0]["id"]).get("id")
        rd.call("PUT", f"/services/{found[0]['id']}/env-vars", json=env_vars)
        print(f"== Render settings updated ({len(env_vars)} variables)")

    new_commit = push_code(gh, login, me["id"], github_token)

    if not found:
        body = {"type": "web_service", "name": SERVICE, "ownerId": owner_id,
                "repo": f"https://github.com/{login}/{REPO}", "branch": "main", "autoDeploy": "yes",
                "envVars": env_vars,
                "serviceDetails": {"runtime": "docker", "plan": "free", "region": REGION, "healthCheckPath": "/api/ping",
                                   "envSpecificDetails": {"dockerfilePath": "./Dockerfile", "dockerContext": "."}}}
        service = rd.call("POST", "/services", json=body).json()["service"]
        print(f"== created Render service {service['name']} ({len(env_vars)} variables)")
    else:
        service = found[0]
        # A public repo linked by URL sends Render no push events, so every update is deployed explicitly.
        rd.call("POST", f"/services/{service['id']}/deploys", json={})
        print(f"== deploy started ({'new code + settings' if new_commit else 'settings only'})")
    wait_live(rd, service["id"], service["serviceDetails"]["url"], started, previous)


if __name__ == "__main__":
    main()
