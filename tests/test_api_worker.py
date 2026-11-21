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
    assert len(catalog) == 34
    assert catalog[0]["parameters_schema"]["properties"]["short_window"]["type"] == "integer"
    assert "5m" in client.get("/api/v1/instruments").json()["items"][0]["intervals"]
    schema = client.get("/openapi.json").json()
    assert len(schema["paths"]) == 14
    assert "/api/v1/strategy-combinations" in schema["paths"]
    assert "/api/v1/strategy-combinations/{combination_id}" in schema["paths"]
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


def test_delete_session_blocks_all_run_states(client, store, rows):
    dataset, sess = prepare(client, rows)
    other_session = session(client, dataset["id"]).json()
    body = {"strategy_id": "SMA_CROSS@1", "parameters": {"short_window": 2, "long_window": 3}}
    path = f"/api/v1/sessions/{sess['id']}"
    completed = client.post(path + "/runs", json=body).json()
    assert Worker(store).once()
    running = client.post(path + "/runs", json=body).json()
    old_job = store.claim()
    queued = client.post(path + "/runs", json=body).json()
    failed = client.post(path + "/runs", json=body).json()
    store.db.runs.update_one({"id": failed["id"]}, {"$set": {"status": "failed"}})
    response = client.delete(path)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "SESSION_IN_USE"
    assert {run["id"] for run in response.json()["error"]["details"][0]["runs"]} == {
        run["id"] for run in (completed, running, queued, failed)
    }
    assert client.get(path).status_code == 200
    assert store.db.series.count_documents({"run_id": completed["id"]}) > 0
    for run in (completed, running, queued, failed):
        assert client.get(f"/api/v1/runs/{run['id']}").status_code == 200
    store.renew(old_job)
    for run in (completed, running, queued, failed):
        store.db.runs.update_one({"id": run["id"]}, {"$set": {"status": "failed"}})
        assert client.delete(f"/api/v1/runs/{run['id']}").status_code == 204
    assert client.delete(path).status_code == 204
    assert client.get(path).status_code == 404
    assert client.delete(path).status_code == 404
    assert client.get(f"/api/v1/sessions/{other_session['id']}").status_code == 200
    assert client.get(f"/api/v1/datasets/{dataset['id']}").status_code == 200


def test_delete_session_cannot_remove_inflight_results(client, store, rows, monkeypatch):
    _, sess = prepare(client, rows)
    run = client.post(
        f"/api/v1/sessions/{sess['id']}/runs", json={"strategy_id": "SMA_CROSS@1"}
    ).json()
    original = store.db.series.insert_many

    def insert_after_blocked_deletion(docs):
        with pytest.raises(AppError) as exc:
            store.delete_session(sess["id"])
        assert exc.value.status == 409
        assert exc.value.code == "SESSION_IN_USE"
        return original(docs)

    monkeypatch.setattr(store.db.series, "insert_many", insert_after_blocked_deletion)
    assert Worker(store).once()
    assert store.db.series.count_documents({"run_id": run["id"]}) > 0
    assert store.run(run["id"])["status"] == "completed"
    assert store.session(sess["id"])["id"] == sess["id"]


def test_delete_session_racing_run_creation(client, store, rows, monkeypatch):
    _, sess = prepare(client, rows)
    original = store.db.runs.insert_one

    def insert_after_deletion(doc):
        store.delete_session(sess["id"])
        return original(doc)

    monkeypatch.setattr(store.db.runs, "insert_one", insert_after_deletion)
    assert (
        client.post(
            f"/api/v1/sessions/{sess['id']}/runs", json={"strategy_id": "SMA_CROSS@1"}
        ).status_code
        == 404
    )
    assert store.db.runs.count_documents({"session_id": sess["id"]}) == 0


def test_delete_session_retries_partial_cleanup(client, store, rows, monkeypatch):
    from pymongo.errors import AutoReconnect

    dataset, sess = prepare(client, rows)

    def unavailable(*args, **kwargs):
        raise AutoReconnect()

    with monkeypatch.context() as patch:
        patch.setattr(store.db.sessions, "delete_one", unavailable)
        with pytest.raises(AutoReconnect):
            store.delete_session(sess["id"])
    assert store.db.sessions.find_one({"id": sess["id"]})["deleting"] is True
    store.delete_session(sess["id"])
    assert store.db.sessions.find_one({"id": sess["id"]}) is None
    assert store.dataset(dataset["id"])["status"] == "published"


def test_delete_dataset_blocks_linked_sessions_and_preserves_runs(client, store, rows):
    dataset, sess = prepare(client, rows)
    second_session = session(client, dataset["id"]).json()
    other_dataset = upload(client, rows, symbol="ETHUSDT").json()
    other_session = session(client, other_dataset["id"]).json()
    body = {"strategy_id": "SMA_CROSS@1", "parameters": {"short_window": 2, "long_window": 3}}
    completed = client.post(f"/api/v1/sessions/{sess['id']}/runs", json=body).json()
    assert Worker(store).once()
    running = client.post(f"/api/v1/sessions/{second_session['id']}/runs", json=body).json()
    job = store.claim()
    queued = client.post(f"/api/v1/sessions/{sess['id']}/runs", json=body).json()
    endpoint = f"/api/v1/datasets/{dataset['id']}"
    response = client.delete(endpoint)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "DATASET_IN_USE"
    assert {item["id"] for item in response.json()["error"]["details"][0]["sessions"]} == {
        sess["id"],
        second_session["id"],
    }
    assert client.get(endpoint).status_code == 200
    assert client.get(endpoint + "/candles").json()["items"]
    for linked_session in (sess, second_session):
        assert client.get(f"/api/v1/sessions/{linked_session['id']}").status_code == 200
    for run in (completed, running, queued):
        assert client.get(f"/api/v1/runs/{run['id']}").status_code == 200
    store.renew(job)
    assert store.db.candles.count_documents({"dataset_id": dataset["id"]}) > 0
    assert store.db.acquisitions.count_documents({"dataset_id": dataset["id"]}) > 0
    for linked_session in (sess, second_session):
        for run in store.db.runs.find({"session_id": linked_session["id"]}):
            store.db.runs.update_one({"id": run["id"]}, {"$set": {"status": "failed"}})
            assert client.delete(f"/api/v1/runs/{run['id']}").status_code == 204
        assert client.delete(f"/api/v1/sessions/{linked_session['id']}").status_code == 204
    assert client.delete(endpoint).status_code == 204
    assert client.get(endpoint).status_code == 404
    assert client.get(endpoint + "/candles").status_code == 404
    assert client.delete(endpoint).status_code == 404
    assert client.get(f"/api/v1/sessions/{other_session['id']}").status_code == 200
    assert client.get(f"/api/v1/datasets/{other_dataset['id']}/candles").json()["items"]
    assert store.db.candles.count_documents({"dataset_id": dataset["id"]}) == 0
    assert store.db.acquisitions.count_documents({"dataset_id": dataset["id"]}) == 0
    assert upload(client, rows).json()["id"] == dataset["id"]


def test_delete_dataset_racing_session_creation(client, store, rows, monkeypatch):
    dataset = upload(client, rows).json()
    original = store.db.sessions.insert_one

    def insert_after_deletion(doc):
        store.delete_dataset(dataset["id"])
        return original(doc)

    monkeypatch.setattr(store.db.sessions, "insert_one", insert_after_deletion)
    assert session(client, dataset["id"]).status_code == 404
    assert store.db.sessions.count_documents({"dataset_id": dataset["id"]}) == 0


def test_delete_dataset_retries_partial_cleanup(client, store, rows, monkeypatch):
    from pymongo.errors import AutoReconnect

    dataset, sess = prepare(client, rows)
    run = client.post(
        f"/api/v1/sessions/{sess['id']}/runs", json={"strategy_id": "SMA_CROSS@1"}
    ).json()
    assert Worker(store).once()
    assert client.delete(f"/api/v1/runs/{run['id']}").status_code == 204
    assert client.delete(f"/api/v1/sessions/{sess['id']}").status_code == 204

    def unavailable(query):
        raise AutoReconnect()

    with monkeypatch.context() as patch:
        patch.setattr(store.db.candles, "delete_many", unavailable)
        with pytest.raises(AutoReconnect):
            store.delete_dataset(dataset["id"])
    assert store.db.datasets.find_one({"id": dataset["id"]})["status"] == "deleting"
    assert client.get("/api/v1/datasets").json()["items"] == []
    store.delete_dataset(dataset["id"])
    assert store.db.datasets.find_one({"id": dataset["id"]}) is None
    assert store.db.sessions.find_one({"id": sess["id"]}) is None
    assert store.db.runs.find_one({"id": run["id"]}) is None
    assert store.db.series.count_documents({"run_id": run["id"]}) == 0
    assert store.db.candles.count_documents({"dataset_id": dataset["id"]}) == 0
    assert store.db.acquisitions.count_documents({"dataset_id": dataset["id"]}) == 0


def test_delete_dataset_rechecks_sessions_created_during_validation(
    client, store, rows, monkeypatch
):
    dataset = upload(client, rows).json()
    original = store.db.datasets.find_one_and_update

    def mark_with_concurrent_session(*args, **kwargs):
        result = original(*args, **kwargs)
        store.db.sessions.insert_one(
            {
                "id": "racing-session",
                "name": "Criada durante a exclusão",
                "dataset_id": dataset["id"],
            }
        )
        return result

    monkeypatch.setattr(store.db.datasets, "find_one_and_update", mark_with_concurrent_session)
    response = client.delete(f"/api/v1/datasets/{dataset['id']}")
    assert response.status_code == 409, response.text
    assert store.dataset(dataset["id"])["status"] == "published"
    assert store.db.sessions.find_one({"id": "racing-session"}) is not None
    assert store.db.candles.count_documents({"dataset_id": dataset["id"]}) > 0


@pytest.mark.parametrize("failure_on", [1, 2])
def test_delete_dataset_keeps_data_when_session_lookup_fails(
    client, store, rows, monkeypatch, failure_on
):
    from pymongo.errors import AutoReconnect

    dataset = upload(client, rows).json()
    original = store.db.sessions.find
    calls = 0

    def unavailable(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == failure_on:
            raise AutoReconnect()
        return original(*args, **kwargs)

    monkeypatch.setattr(store.db.sessions, "find", unavailable)
    response = client.delete(f"/api/v1/datasets/{dataset['id']}")
    assert response.status_code == 503, response.text
    assert store.dataset(dataset["id"])["status"] == "published"
    assert store.db.candles.count_documents({"dataset_id": dataset["id"]}) > 0


def test_delete_session_rechecks_concurrent_run_and_restores_session(
    client, store, rows, monkeypatch
):
    _, sess = prepare(client, rows)
    original = store.db.sessions.find_one_and_update

    def mark_with_run(*args, **kwargs):
        result = original(*args, **kwargs)
        store.db.runs.insert_one({"id": "racing-run", "session_id": sess["id"], "status": "queued"})
        return result

    monkeypatch.setattr(store.db.sessions, "find_one_and_update", mark_with_run)
    response = client.delete(f"/api/v1/sessions/{sess['id']}")
    assert response.status_code == 409, response.text
    assert store.session(sess["id"]) == sess
    assert store.db.runs.find_one({"id": "racing-run"}) is not None


@pytest.mark.parametrize("failure_on", [1, 2])
def test_delete_session_preserves_session_on_lookup_failure(
    client, store, rows, monkeypatch, failure_on
):
    from pymongo.errors import AutoReconnect

    _, sess = prepare(client, rows)
    original = store.db.runs.find
    calls = 0

    def unavailable(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == failure_on:
            raise AutoReconnect()
        return original(*args, **kwargs)

    monkeypatch.setattr(store.db.runs, "find", unavailable)
    response = client.delete(f"/api/v1/sessions/{sess['id']}")
    assert response.status_code == 503, response.text
    assert store.session(sess["id"]) == sess
