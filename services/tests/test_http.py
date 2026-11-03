import pytest
import responses

from services.http import HttpClient, HttpError, RateLimiter


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds


def test_rate_limiter_sleeps_when_exhausted():
    clock = FakeClock()
    limiter = RateLimiter(rate_per_sec=2, capacity=1, monotonic=clock.monotonic, sleep=clock.sleep)
    limiter.acquire()  # consumes the one token, no sleep
    limiter.acquire()  # must wait ~0.5s for a new token
    assert clock.slept
    assert clock.slept[0] == pytest.approx(0.5, rel=0.1)


@responses.activate
def test_get_json_success():
    responses.add(responses.GET, "https://api.test/x", json={"ok": True}, status=200)
    assert HttpClient().get_json("https://api.test/x") == {"ok": True}


@responses.activate
def test_retries_on_429_then_succeeds():
    responses.add(responses.GET, "https://api.test/x", status=429, headers={"Retry-After": "0"})
    responses.add(responses.GET, "https://api.test/x", json={"ok": True}, status=200)
    client = HttpClient(max_retries=2, sleep=lambda _s: None)
    assert client.get_json("https://api.test/x") == {"ok": True}


@responses.activate
def test_raises_after_exhausting_retries_on_500():
    for _ in range(4):
        responses.add(responses.GET, "https://api.test/x", status=500)
    client = HttpClient(max_retries=2, sleep=lambda _s: None)
    with pytest.raises(HttpError):
        client.get("https://api.test/x")


@responses.activate
def test_raises_on_404():
    responses.add(responses.GET, "https://api.test/x", status=404)
    with pytest.raises(HttpError):
        HttpClient().get("https://api.test/x")


@responses.activate
def test_http_error_carries_status_and_body_on_4xx():
    responses.add(
        responses.GET,
        "https://api.test/x",
        status=402,
        body='{"errors":[{"detail":"quota exceeded"}]}',
    )
    with pytest.raises(HttpError) as ei:
        HttpClient().get("https://api.test/x")
    assert ei.value.status == 402
    assert "quota exceeded" in (ei.value.body or "")


@responses.activate
def test_paginate_follows_next():
    responses.add(
        responses.GET,
        "https://api.test/items",
        json={"data": [1, 2], "next": "https://api.test/items?page=2"},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://api.test/items?page=2",
        json={"data": [3], "next": None},
        status=200,
    )
    client = HttpClient()
    got = list(
        client.paginate(
            "https://api.test/items",
            items=lambda d: d["data"],
            next_url=lambda d: d["next"],
        )
    )
    assert got == [1, 2, 3]
