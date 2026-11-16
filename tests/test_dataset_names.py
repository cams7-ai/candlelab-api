import json

import pytest

from candlelab.application.service import Service
from candlelab.domain.candles import parse_candles
from candlelab.schemas import BinanceRequest
from conftest import upload


def test_upload_name_is_persisted_and_updated_on_reuse(client, rows, store):
    first = upload(client, rows).json()
    assert first["name"] == "candles.json"
    result = client.post(
        "/api/v1/datasets",
        data={"symbol": "BTCUSDT", "interval": "5m"},
        files={"file": ("outro arquivo.json", json.dumps(rows).encode(), "application/json")},
    )
    assert result.status_code == 200
    assert result.json()["id"] == first["id"]
    assert store.dataset(first["id"])["name"] == "outro arquivo.json"
    assert client.get("/api/v1/datasets").json()["items"][0]["name"] == "outro arquivo.json"
    assert client.get(f"/api/v1/datasets/{first['id']}").json()["name"] == "outro arquivo.json"


@pytest.mark.parametrize("dates", [True, False])
def test_binance_name_uses_query_dates_or_returned_range(settings, store, rows, dates):
    candles = parse_candles(rows, "5m", 1000)
    start = 1794574800000  # 2026-11-13T13:00:00Z
    end = 1794592800000  # 2026-11-13T18:00:00Z

    class Binance:
        def acquire(self, request):
            query = {"startTime": start, "endTime": end} if dates else {}
            return candles, {"mode": "BINANCE_API", "query": query}

    result = Service(settings, store, Binance()).acquire(
        BinanceRequest(symbol="BTCUSDT", interval="5m")
    )
    expected = (
        "btcusdt_5m_2026-11-13T1300Z_2026-11-13T1800Z"
        if dates
        else "btcusdt_5m_2026-01-01T0000Z_2026-01-01T0055Z"
    )
    assert result["name"] == expected
    assert store.dataset(result["id"])["name"] == expected


def test_existing_datasets_without_name_remain_readable(client, rows, store):
    dataset = upload(client, rows).json()
    store.db.datasets.update_one({"id": dataset["id"]}, {"$unset": {"name": ""}})
    result = client.get(f"/api/v1/datasets/{dataset['id']}")
    assert result.status_code == 200
    assert result.json()["name"].startswith("btcusdt_5m_2026-01-01T0000Z_")
    assert client.get("/api/v1/datasets").json()["items"][0]["name"] == result.json()["name"]
