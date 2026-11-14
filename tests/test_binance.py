import json

import httpx
import pytest
from pydantic import ValidationError

from candlelab.application.service import Service
from candlelab.domain.errors import AppError
from candlelab.infrastructure.binance import BINANCE_URL, BinanceClient, utc_ms
from candlelab.schemas import BinanceRequest


BASE = {"symbol": "btcusdt", "interval": "5m"}


def remote(settings, responder, now=10**15, sleep=None):
    return BinanceClient(
        settings,
        httpx.Client(transport=httpx.MockTransport(responder)),
        sleep=sleep or (lambda _: None),
        now=lambda: now,
    )


@pytest.mark.parametrize(
    "extra", [{}, {"startTime": "2026-01-01T00:00:39Z"}, {"endTime": "2026-01-01T00:10:39Z"}]
)
def test_optional_dates_one_page(settings, rows, extra):
    seen = []
    data = rows[1:3] if "startTime" in extra else rows[:2]

    def respond(request):
        seen.append(dict(request.url.params))
        assert str(request.url).startswith(BINANCE_URL)
        return httpx.Response(200, json=data)

    candles, provenance = remote(settings, respond).acquire(BinanceRequest(**BASE, **extra))
    assert len(seen) == 1 and len(candles) == 2
    assert seen[0] == {
        "symbol": "BTCUSDT",
        "interval": "5m",
        **{k: str(utc_ms(v)) for k, v in extra.items()},
    }
    assert "limit" not in provenance["query"]
    if "startTime" in extra:
        assert provenance["query"]["startTime"] % 60000 == 39000


@pytest.mark.parametrize(
    "extra",
    [
        {"startTime": "2026-01-01"},
        {"startTime": "2026-01-01T00:00:00+00:00"},
        {"startTime": "2026-02-30T00:00:00Z"},
        {"startTime": "1969-12-31T00:00:00Z"},
        {"startTime": "2026-01-01T00:00:00.1Z"},
        {"startTime": "2026-01-01T00:00:60Z"},
        {"startTime": "2026-01-02T00:00:00Z", "endTime": "2026-01-01T00:00:00Z"},
        {"limit": 0},
        {"limit": 1001},
        {"limit": True},
        {"limit": "1000"},
    ],
)
def test_invalid_requests(extra):
    with pytest.raises(ValidationError):
        BinanceRequest(**BASE, **extra)


@pytest.mark.parametrize("limit", [None, 2])
def test_pagination_preserves_query_and_inclusive_end(settings, rows, limit):
    pages = [rows[:2], rows[2:4]]
    seen = []

    def respond(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200, json=pages.pop(0))

    extra = {} if limit is None else {"limit": limit}
    candles, _ = remote(settings, respond).acquire(
        BinanceRequest(
            **BASE, startTime="2026-01-01T00:00:00Z", endTime="2026-01-01T00:15:00Z", **extra
        )
    )
    assert [c.open_time_ms for c in candles] == [r[0] for r in rows[:4]]
    assert seen[1]["startTime"] == str(rows[2][0])
    assert seen[0]["endTime"] == seen[1]["endTime"]
    assert ("limit" in seen[0]) == (limit is not None)
    if limit:
        assert seen[0]["limit"] == seen[1]["limit"] == str(limit)


def test_empty_page_terminates_range(settings, rows):
    pages = [rows[:2], []]
    result, _ = remote(settings, lambda _: httpx.Response(200, json=pages.pop(0))).acquire(
        BinanceRequest(**BASE, startTime="2026-01-01T00:00:00Z", endTime="2026-01-01T01:00:00Z")
    )
    assert len(result) == 2 and not pages


@pytest.mark.parametrize("page", [[], [[1]], {"code": 100}, "broken"])
def test_empty_and_invalid_response(settings, page):
    api = remote(settings, lambda _: httpx.Response(200, json=page))
    with pytest.raises(AppError):
        api.acquire(BinanceRequest(**BASE))


def test_open_candles_excluded(settings, rows):
    api = remote(settings, lambda _: httpx.Response(200, json=rows[:2]), now=rows[1][6])
    result, _ = api.acquire(BinanceRequest(**BASE))
    assert len(result) == 1
    with pytest.raises(AppError, match="sem candles fechados"):
        remote(settings, lambda _: httpx.Response(200, json=rows[:1]), now=rows[0][0]).acquire(
            BinanceRequest(**BASE)
        )


@pytest.mark.parametrize("mode", ["gap", "repeat", "failure"])
def test_failed_pagination_never_publishes(settings, store, rows, mode):
    pages = [rows[:2], rows[3:4] if mode == "gap" else rows[:2]]
    calls = 0

    def respond(_):
        nonlocal calls
        calls += 1
        if mode == "failure" and calls > 1:
            return httpx.Response(503)
        return httpx.Response(200, json=pages.pop(0))

    svc = Service(settings, store, remote(settings, respond))
    with pytest.raises(AppError):
        svc.acquire(
            BinanceRequest(**BASE, startTime="2026-01-01T00:00:00Z", endTime="2026-01-01T00:20:00Z")
        )
    assert store.db.datasets.count_documents({}) == 0


def test_limit_total(settings, rows):
    settings.max_candles = 3
    pages = [rows[:2], rows[2:4]]
    with pytest.raises(AppError):
        remote(settings, lambda _: httpx.Response(200, json=pages.pop(0))).acquire(
            BinanceRequest(**BASE, startTime="2026-01-01T00:00:00Z", endTime="2026-01-01T00:20:00Z")
        )


def test_upload_and_acquire_reuse_snapshot(settings, store, rows):
    svc = Service(settings, store, remote(settings, lambda _: httpx.Response(200, json=rows)))
    first = svc.upload(json.dumps(rows).encode(), "source.json", "BTCUSDT", "5m")
    acquired = svc.acquire(BinanceRequest(**BASE))
    assert acquired["id"] == first["id"]
    assert {p["mode"] for p in store.db.acquisitions.find()} == {"UPLOAD", "BINANCE_API"}


def test_retry_after_and_network_timeout(settings, rows):
    pages = [httpx.Response(429, headers={"Retry-After": "2"}), httpx.Response(200, json=rows)]
    delays = []
    result, _ = remote(settings, lambda _: pages.pop(0), sleep=delays.append).acquire(
        BinanceRequest(**BASE)
    )
    assert len(result) == len(rows) and delays == [2]

    def timeout(_):
        raise httpx.ReadTimeout("timeout")

    with pytest.raises(AppError) as err:
        remote(settings, timeout).acquire(BinanceRequest(**BASE))
    assert err.value.status == 503
    with pytest.raises(AppError):
        remote(
            settings,
            lambda _: httpx.Response(429, headers={"Retry-After": "500"}),
            sleep=delays.append,
        ).acquire(BinanceRequest(**BASE))
    assert delays == [2]


def test_invalid_json_and_http_failure(settings):
    for response in [httpx.Response(200, text="bad json"), httpx.Response(400)]:
        with pytest.raises(AppError) as exc:
            remote(settings, lambda _: response).acquire(BinanceRequest(**BASE))
        assert exc.value.status == 502
