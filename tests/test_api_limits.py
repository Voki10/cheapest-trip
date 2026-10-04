"""Public-site rationing: concurrent searches and searches per visitor per hour."""

import pytest
from fastapi import HTTPException

from cheaptrip import api
from cheaptrip.models import TripQuery


@pytest.fixture(autouse=True)
def clean():
    api.jobs.clear()
    api.searches_by_ip.clear()
    yield
    api.jobs.clear()
    api.searches_by_ip.clear()


def running(ip):
    job = api.Job(id=ip + "-job", query=TripQuery(), ip=ip)
    api.jobs[job.id] = job
    return job


def test_one_running_search_per_visitor():
    running("1.1.1.1")
    with pytest.raises(HTTPException, match="предыдущий поиск"):
        api.admit_search("1.1.1.1")
    api.admit_search("2.2.2.2")  # someone else may search


def test_server_wide_concurrency():
    for ip in ("1.1.1.1", "2.2.2.2")[: api.settings.max_concurrent_searches]:
        running(ip)
    with pytest.raises(HTTPException, match="слишком много"):
        api.admit_search("3.3.3.3")


def test_hourly_quota_per_visitor():
    for _ in range(api.settings.searches_per_ip_per_hour):
        api.admit_search("4.4.4.4")
    with pytest.raises(HTTPException, match="Лимит"):
        api.admit_search("4.4.4.4")
