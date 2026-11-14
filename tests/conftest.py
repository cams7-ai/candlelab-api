import json
from pathlib import Path

import httpx
import mongomock
import pytest
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError

from candlelab.api import create_app
from candlelab.config import Settings
from candlelab.infrastructure.binance import BinanceClient
from candlelab.infrastructure.cache import CandleCache
from candlelab.infrastructure.store import MongoStore


class OfflineRedis:
    def get(self, *args):
        raise ConnectionError("offline")

    def setex(self, *args):
        raise ConnectionError("offline")


@pytest.fixture
def settings():
    return Settings(_env_file=None, binance_retries=1)


@pytest.fixture
def store(settings):
    store = MongoStore(settings, mongomock.MongoClient(tz_aware=True).db)
    store.indexes()
    return store


@pytest.fixture
def rows():
    return json.loads((Path(__file__).parent / "fixtures/btcusdt-5m.json").read_text())


@pytest.fixture
def client(settings, store):
    def offline(request):
        raise httpx.ConnectError("offline")

    binance = BinanceClient(
        settings, httpx.Client(transport=httpx.MockTransport(offline)), sleep=lambda _: None
    )
    app = create_app(settings, store, binance, CandleCache(settings, OfflineRedis()))
    with TestClient(app) as client:
        yield client


def upload(client, rows, interval="5m", symbol="btcusdt"):
    return client.post(
        "/api/v1/datasets",
        data={"symbol": symbol, "interval": interval},
        files={"file": ("candles.json", json.dumps(rows).encode(), "application/json")},
    )


def session(client, dataset_id, **extra):
    return client.post(
        "/api/v1/sessions",
        json={
            "name": "Teste",
            "dataset_id": dataset_id,
            "initial_cash": "1000",
            "fee_rate": "0.001",
            **extra,
        },
    )
