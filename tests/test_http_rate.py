from cheaptrip import http


def test_requests_to_rate_limited_host_are_spaced():
    http._next_slot.clear()
    waits = [http.reserve_slot("api.travelpayouts.com") for _ in range(19)]
    # 19 requests at 9/s: the last one may go no earlier than 2 s after the first.
    assert waits[0] == 0.0
    assert abs(waits[-1] - 18 / 9.0) < 0.05
    assert all(b > a for a, b in zip(waits, waits[1:]))


def test_other_hosts_are_not_paced():
    assert http.reserve_slot("data.xotelo.com") == 0.0


def test_server_reported_quota_makes_everyone_wait_for_the_next_window():
    http._next_slot.clear()
    http._quota.clear()
    http.note_quota("api.travelpayouts.com", {"x-rate-limit-remaining": "10", "x-rate-limit-reset": "30"})
    assert http.quota_remaining("api.travelpayouts.com") == 10
    wait = http.reserve_slot("api.travelpayouts.com")  # 10 left <= reserve 15: wait for reset
    assert 29 < wait <= 30.1
    http._next_slot.clear()
    http._quota.clear()
