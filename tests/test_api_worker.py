from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal

import pytest

from candlelab.application.service import Service
from candlelab.domain.engine import simulate
from candlelab.domain.errors import AppError
from candlelab.infrastructure.store import now
from candlelab.schemas import RunRequest
from candlelab.worker import Worker
from conftest import session, upload


def prepare(client, rows):
    dataset = upload(client, rows).json()
    created = session(client, dataset["id"])
    assert created.status_code == 201, created.text
    return dataset, created.json()


def test_http_full_flow_and_isolation(client, store, rows):
    dataset, sess = prepare(client, rows)
    path = f"/api/v1/sessions/{sess['id']}/runs"
    request = {"strategy_id": "SMA_CROSS@1", "parameters": {"short_window": 2, "long_window": 3}}
    created = client.post(path, json=request, headers={"Idempotency-Key": "test"})
    assert created.status_code == 202, created.text
    run = created.json()
    assert run["status"] == "queued" and run["summary"] is None
    assert run["dataset_snapshot"]["content_hash"] == dataset["content_hash"]
    assert run["policy"]["quantity_step"] == "0.000001"
    assert (
        client.post(path, json=request, headers={"Idempotency-Key": "test"}).json()["id"]
        == run["id"]
    )
    assert client.get(f"/api/v1/runs/{run['id']}/series?kind=equity").status_code == 409
    assert (
        client.post(
            path, json={"strategy_id": "EMA_CROSS@1"}, headers={"Idempotency-Key": "test"}
        ).status_code
        == 409
    )
    second = client.post(path, json=request).json()
    worker = Worker(store)
    assert worker.once() and worker.once() and not worker.once()
    final = client.get(f"/api/v1/runs/{run['id']}").json()
    assert final["status"] == "completed", final
    assert final["summary"] == client.get(f"/api/v1/runs/{second['id']}").json()["summary"]
    assert final["summary"]["initial_cash"] == "1000"
    assert client.get(f"/api/v1/sessions/{sess['id']}").json() == sess
    for kind in ("indicators", "events", "equity"):
        result = client.get(f"/api/v1/runs/{run['id']}/series", params={"kind": kind, "limit": 2})
        assert result.status_code == 200, result.text
        first = result.json()
        assert len(first["items"]) == 2 and first["next_cursor"]
        second_page = client.get(
            f"/api/v1/runs/{run['id']}/series",
            params={"kind": kind, "cursor": first["next_cursor"], "limit": 2},
        ).json()
        assert first["items"] != second_page["items"]
    assert len(client.get(path).json()["items"]) == 2
    for collection in ("datasets", "sessions"):
        assert len(client.get(f"/api/v1/{collection}").json()["items"]) == 1


@pytest.mark.parametrize(
    "extra",
    [
        {"initial_cash": "0"},
        {"initial_cash": 1000},
        {"initial_cash": "-1"},
        {"fee_rate": "1"},
        {"fee_rate": "-0.01"},
        {"fee_rate": "NaN"},
        {"fee_rate": True},
    ],
)
def test_session_financial_validation(client, rows, extra):
    dataset = upload(client, rows).json()
    assert session(client, dataset["id"], **extra).status_code == 422


def test_errors_and_catalog_openapi(client, rows):
    assert client.get("/api/v1/datasets/unknown").status_code == 404
    assert client.get("/api/v1/sessions/unknown").status_code == 404
    assert client.get("/api/v1/runs/unknown").status_code == 404
    assert session(client, "unknown").status_code == 404
    assert upload(client, rows, symbol="XYZUSDT").status_code == 422
    assert (
        client.post(
            "/api/v1/datasets/binance", json={"symbol": "BTCUSDT", "interval": "5m"}
        ).status_code
        == 503
    )
    catalog = client.get("/api/v1/strategies").json()["items"]
    assert len(catalog) == 22
    assert catalog[0]["parameters_schema"]["properties"]["short_window"]["type"] == "integer"
    assert "5m" in client.get("/api/v1/instruments").json()["items"][0]["intervals"]
    schema = client.get("/openapi.json").json()
    assert len(schema["paths"]) == 12
    assert "202" in schema["paths"]["/api/v1/sessions/{session_id}/runs"]["post"]["responses"]
    assert schema["components"]["schemas"]["BinanceRequest"]["required"] == ["symbol", "interval"]
    assert (
        schema["components"]["schemas"]["Summary"]["properties"]["final_cash"]["type"] == "string"
    )
    for path in ("/api/v1/datasets", "/api/v1/sessions"):
        assert client.get(path, params={"limit": 0}).status_code == 422
    _, sess = prepare(client, rows)
    assert (
        client.post(
            f"/api/v1/sessions/{sess['id']}/runs", json={"strategy_id": "unknown"}
        ).status_code
        == 422
    )


def test_reclaim_fences_stale_worker(client, store, rows):
    _, sess = prepare(client, rows)
    created = client.post(
        f"/api/v1/sessions/{sess['id']}/runs", json={"strategy_id": "SMA_CROSS@1"}
    ).json()
    old = store.claim()
    assert store.claim() is None
    store.db.runs.update_one(
        {"id": old["id"]}, {"$set": {"lease_until": now() - timedelta(seconds=1)}}
    )
    replacement = store.claim()
    assert replacement["attempt_id"] != old["attempt_id"]
    with pytest.raises(AppError, match="expirada"):
        store.renew(old)
    result = simulate(
        store.candles(sess["dataset_id"]),
        old["strategy_id"],
        old["parameters"],
        Decimal(1000),
        Decimal("0.001"),
        Decimal("0.000001"),
    )
    with pytest.raises(AppError):
        store.save_results(old, result)
    assert store.fail(old, {"error": {"code": "OLD", "message": "old", "details": []}}) == 0
    assert store.save_results(replacement, result)
    assert store.run(created["id"])["published_attempt_id"] == replacement["attempt_id"]


def test_worker_failure_and_attempt_results_hidden(client, store, rows, monkeypatch):
    _, sess = prepare(client, rows)
    created = client.post(
        f"/api/v1/sessions/{sess['id']}/runs", json={"strategy_id": "SMA_CROSS@1"}
    ).json()
    original = store.save_results

    def fail(job, result):
        store.db.series.insert_one(
            {
                "run_id": job["id"],
                "attempt_id": job["attempt_id"],
                "kind": "equity",
                "open_time_ms": 0,
                "seq": 0,
                "point": {"open_time_ms": 0, "equity": "1"},
            }
        )
        raise RuntimeError("crash")

    monkeypatch.setattr(store, "save_results", fail)
    Worker(store).once()
    run = client.get(f"/api/v1/runs/{created['id']}").json()
    assert run["status"] == "failed" and run["error"]["error"]["code"] == "RUN_FAILED"
    assert client.get(f"/api/v1/runs/{created['id']}/series?kind=equity").status_code == 409
    # Recovery simulates a crash before a task can report failure.
    store.db.runs.update_one(
        {"id": created["id"]},
        {"$set": {"status": "running", "lease_until": now() - timedelta(seconds=1)}},
    )
    monkeypatch.setattr(store, "save_results", original)
    Worker(store).once()
    recovered = store.run(created["id"])
    assert recovered["status"] == "completed"
    assert (
        client.get(f"/api/v1/runs/{created['id']}/series?kind=equity").json()["items"][0]["equity"]
        == "1000"
    )


def test_atomic_claim_and_concurrent_idempotency(client, store, settings, rows):
    _, sess = prepare(client, rows)
    svc = Service(settings, store, None)
    request = RunRequest(strategy_id="SMA_CROSS@1")
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(lambda _: svc.run(sess["id"], request, "one")["id"], range(16)))
    assert len(set(ids)) == 1
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda _: store.claim(), range(16)))
    assert sum(c is not None for c in claims) == 1


def test_publication_checks_possession_after_writing(client, store, rows, monkeypatch):
    _, sess = prepare(client, rows)
    created = client.post(
        f"/api/v1/sessions/{sess['id']}/runs", json={"strategy_id": "SMA_CROSS@1"}
    ).json()
    job = store.claim()
    result = simulate(
        store.candles(sess["dataset_id"]),
        job["strategy_id"],
        job["parameters"],
        Decimal(1000),
        Decimal("0.001"),
        Decimal("0.000001"),
    )
    original = store.db.series.insert_many

    def revoke_after_last_batch(docs):
        saved = original(docs)
        if docs[0]["kind"] == "evaluations":
            store.db.runs.update_one({"id": job["id"]}, {"$set": {"attempt_id": "new-owner"}})
        return saved

    monkeypatch.setattr(store.db.series, "insert_many", revoke_after_last_batch)
    assert not store.save_results(job, result)
    assert store.run(created["id"])["status"] == "running"
    assert client.get(f"/api/v1/runs/{created['id']}/series?kind=equity").status_code == 409


def test_worker_renews_lease_while_engine_runs(client, store, rows, monkeypatch):
    import threading

    _, sess = prepare(client, rows)
    created = client.post(
        f"/api/v1/sessions/{sess['id']}/runs", json={"strategy_id": "SMA_CROSS@1"}
    ).json()
    store.settings.lease_seconds = 3
    renewed = threading.Event()
    original = store.renew

    def renew(job):
        original(job)
        renewed.set()

    def slow_engine(*args, **kwargs):
        assert renewed.wait(5), "Heartbeat não executado durante o processamento."
        return simulate(*args, **kwargs)

    monkeypatch.setattr(store, "renew", renew)
    monkeypatch.setattr("candlelab.worker.simulate", slow_engine)
    Worker(store).once()
    assert store.run(created["id"])["status"] == "completed"


def test_delete_run_removes_results_and_preserves_session(client, store, rows):
    dataset, sess = prepare(client, rows)
    path = f"/api/v1/sessions/{sess['id']}/runs"
    body = {"strategy_id": "SMA_CROSS@1", "parameters": {"short_window": 2, "long_window": 3}}
    run = client.post(path, json=body).json()
    other = client.post(path, json=body).json()
    endpoint = f"/api/v1/runs/{run['id']}"
    assert client.delete(endpoint).status_code == 409
    assert Worker(store).once()
    assert Worker(store).once()
    assert store.db.series.count_documents({"run_id": run["id"]}) > 0
    assert client.delete(endpoint).status_code == 204
    assert client.get(endpoint).status_code == 404
    assert client.delete(endpoint).status_code == 404
    assert client.get(endpoint + "/series?kind=equity").status_code == 404
    assert store.db.series.count_documents({"run_id": run["id"]}) == 0
    assert [r["id"] for r in client.get(path).json()["items"]] == [other["id"]]
    assert store.db.series.count_documents({"run_id": other["id"]}) > 0
    assert client.get(f"/api/v1/sessions/{sess['id']}").json() == sess
    assert client.get(f"/api/v1/datasets/{dataset['id']}").status_code == 200
    store.db.runs.update_one({"id": other["id"]}, {"$set": {"status": "failed"}})
    assert client.delete(f"/api/v1/runs/{other['id']}").status_code == 204


def test_delete_cors_preflight(client):
    response = client.options(
        "/api/v1/runs/example",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "DELETE",
        },
    )
    assert response.status_code == 200
    assert "DELETE" in response.headers["access-control-allow-methods"]
