"""Mesmo adaptador com MongoDB e Redis reais; sem nenhuma chamada à Binance."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from redis import Redis

from candlelab.api import create_app
from candlelab.application.service import Service
from candlelab.config import Settings
from candlelab.infrastructure.binance import BinanceClient
from candlelab.infrastructure.store import MongoStore, now
from candlelab.worker import Worker
from conftest import session

pytestmark = pytest.mark.integration


@pytest.fixture
def real_store():
    uri = os.getenv("CANDLELAB_TEST_MONGO_URI")
    if not uri:
        pytest.skip("CANDLELAB_TEST_MONGO_URI não configurada.")
    settings = Settings(
        _env_file=None,
        mongo_uri=uri,
        mongo_database="candlelab_test_" + uuid4().hex,
        redis_url=os.getenv("CANDLELAB_TEST_REDIS_URL", "redis://localhost:6379/0"),
    )
    store = MongoStore(settings)
    store.indexes()
    try:
        yield store
    finally:
        cleanup = MongoStore(settings)
        cleanup.client.drop_database(settings.mongo_database)
        cleanup.client.close()
        store.client.close()


def test_mongodb_concurrency_reconnect_and_worker(real_store, rows):
    settings = real_store.settings
    service = Service(settings, real_store, None)
    with ThreadPoolExecutor(max_workers=8) as pool:
        datasets = list(
            pool.map(
                lambda _: service.upload(
                    json.dumps(rows).encode(), "fixture.json", "BTCUSDT", "5m"
                ),
                range(16),
            )
        )
    assert len({d["id"] for d in datasets}) == 1
    assert real_store.db.candles.count_documents({}) == len(rows)
    # Close the client and construct a new adapter, as separate API/worker processes do.
    real_store.client.close()
    reopened = MongoStore(settings)
    try:
        assert reopened.dataset(datasets[0]["id"])["candle_count"] == len(rows)
        app = create_app(
            settings,
            reopened,
            BinanceClient(
                settings, httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(503)))
            ),
        )
        with TestClient(app) as client:
            sess = session(client, datasets[0]["id"]).json()
            path = f"/api/v1/sessions/{sess['id']}/runs"
            request = {
                "strategy_id": "SMA_CROSS@1",
                "parameters": {"short_window": 2, "long_window": 3},
            }
            with ThreadPoolExecutor(max_workers=8) as pool:
                runs = list(
                    pool.map(
                        lambda _: client.post(
                            path, json=request, headers={"Idempotency-Key": "same"}
                        ).json(),
                        range(16),
                    )
                )
            assert len({r["id"] for r in runs}) == 1
            stale = reopened.claim()
            reopened.db.runs.update_one(
                {"id": stale["id"]}, {"$set": {"lease_until": now() - timedelta(seconds=1)}}
            )
            assert Worker(reopened).once()
            run = client.get("/api/v1/runs/" + runs[0]["id"]).json()
            assert run["status"] == "completed", run
            assert (
                reopened.fail(
                    stale, {"error": {"code": "STALE", "message": "stale", "details": []}}
                )
                == 0
            )
            points = client.get(f"/api/v1/runs/{run['id']}/series?kind=equity").json()["items"]
            assert len(points) == len(rows)
    finally:
        reopened.client.close()


def test_redis_cache_and_mongodb_fallback(real_store, rows):
    settings = real_store.settings
    redis = Redis.from_url(settings.redis_url, decode_responses=True, socket_connect_timeout=1)
    redis.ping()
    from candlelab.infrastructure.cache import CandleCache

    cache = CandleCache(settings, redis)
    key = "candlelab:integration:" + uuid4().hex
    calls = []
    try:

        def loader():
            calls.append(1)
            return {"items": [], "next_cursor": None}

        assert cache.read(key, loader) == cache.read(key, loader)
        assert calls == [1]
        assert 0 < redis.ttl(key) <= settings.cache_ttl
        redis.set(key, "invalid-json")
        assert cache.read(key, loader)["items"] == []
        assert calls == [1, 1]
    finally:
        redis.delete(key)
        redis.close()


def test_all_tutorial_runs_persist_with_real_mongodb(real_store):
    import math
    from candlelab.domain.strategies import catalog
    from candlelab.schemas import RunRequest
    from test_tutorial import candles, auxiliary

    service = Service(real_store.settings, real_store, None)
    data = candles([round(100 + 8 * math.sin(i / 5) + i / 20, 4) for i in range(120)])
    raw = [
        [
            c.open_time_ms,
            str(c.open),
            str(c.high),
            str(c.low),
            str(c.close),
            str(c.base_volume),
            c.close_time_ms,
            "100",
            1,
            "1",
            "1",
            "0",
        ]
        for c in data
    ]
    dataset = service.upload(json.dumps(raw).encode(), "synthetic.json", "BTCUSDT", "1m")
    sess = real_store.create_session(
        dict(
            name="All tutorial strategies",
            dataset_id=dataset["id"],
            initial_cash="1000",
            fee_rate="0.001",
        )
    )
    for item in catalog()[4:]:
        parameters = (
            {"auxiliary_json": json.dumps(auxiliary(data))} if item["required_data"] else {}
        )
        run = service.run(
            sess["id"], RunRequest(strategy_id=item["id"], parameters=parameters), None
        )
        assert Worker(real_store).once()
        result = real_store.run(run["id"])
        assert result["status"] == "completed", (item["id"], result.get("error"))
        assert result["parameters"] == run["parameters"]
        assert real_store.db.series.count_documents({"run_id": run["id"], "kind": "equity"}) == len(
            data
        )
