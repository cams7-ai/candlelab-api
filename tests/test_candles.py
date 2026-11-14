import copy
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from candlelab.domain.candles import content_hash, parse_candles
from candlelab.domain.errors import AppError
from candlelab.application.service import Service
from conftest import upload


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r[0].pop(),
        lambda r: r[0].append("extra"),
        lambda r: r[0].__setitem__(0, True),
        lambda r: r[0].__setitem__(1, 12),
        lambda r: r[0].__setitem__(1, "NaN"),
        lambda r: r[0].__setitem__(1, "Infinity"),
        lambda r: r[0].__setitem__(1, "0"),
        lambda r: r[0].__setitem__(5, "-1"),
        lambda r: r[0].__setitem__(8, False),
        lambda r: r[0].__setitem__(11, 0),
        lambda r: r[0].__setitem__(6, r[0][6] + 1),
        lambda r: r[0].__setitem__(2, "1"),
        lambda r: r[0].__setitem__(0, -1),
        lambda r: r[0].__setitem__(6, 10**18),
        lambda r: r.pop(1),
        lambda r: r.insert(1, r[0]),
        lambda r: r.reverse(),
    ],
)
def test_strict_parser_rejects_invalid_rows(rows, change):
    change(rows)
    with pytest.raises(AppError) as error:
        parse_candles(rows, "5m", 100)
    assert error.value.details[0]["row"] >= 0


def test_hash_normalizes_decimals_and_preserves_reserved(rows):
    normal = content_hash(parse_candles(rows, "5m", 100))
    for row in rows:
        for i in (1, 2, 3, 4, 5, 7, 9, 10):
            row[i] += "0" if "." in row[i] else ".0"
    assert content_hash(parse_candles(rows, "5m", 100)) == normal
    rows[0][11] = "reserved"
    assert content_hash(parse_candles(rows, "5m", 100)) != normal


def test_upload_idempotency_and_conflict_datasets(client, store, rows):
    response = upload(client, rows)
    assert response.status_code == 200, response.text
    first = response.json()
    assert first["symbol"] == "BTCUSDT" and first["candle_count"] == len(rows)
    assert upload(client, rows).json()["id"] == first["id"]
    changed = copy.deepcopy(rows)
    changed[0][11] = "different"
    assert upload(client, changed).json()["id"] != first["id"]
    assert store.db.acquisitions.count_documents({"dataset_id": first["id"]}) == 2
    invalid = upload(client, rows, "1m")
    assert invalid.status_code == 422
    assert store.db.datasets.count_documents({"status": "published"}) == 2


def test_limits_and_json(client, rows):
    svc = client.app.state.service
    svc.settings.max_upload_bytes = 1024
    assert upload(client, rows).status_code == 422
    svc.settings.max_upload_bytes = 100000
    svc.settings.max_candles = 2
    assert upload(client, rows).status_code == 422
    assert (
        client.post(
            "/api/v1/datasets",
            data={"symbol": "BTCUSDT", "interval": "5m"},
            files={"file": ("bad.json", b"[NaN]")},
        ).status_code
        == 422
    )


def test_resume_after_failed_batch_and_hide_incomplete(store, settings, rows, monkeypatch):
    svc = Service(settings, store, None)
    settings.batch_size = 2
    original = store.db.candles.insert_many
    calls = 0

    def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("crash")
        return original(*args, **kwargs)

    monkeypatch.setattr(store.db.candles, "insert_many", interrupted)
    with pytest.raises(RuntimeError):
        svc.upload(json.dumps(rows).encode(), "test.json", "BTCUSDT", "5m")
    assert store.page("datasets", {"status": "published"}, 100)["items"] == []
    partial = store.db.datasets.find_one()
    with pytest.raises(AppError):
        store.dataset(partial["id"])
    monkeypatch.setattr(store.db.candles, "insert_many", original)
    dataset = svc.upload(json.dumps(rows).encode(), "test.json", "BTCUSDT", "5m")
    assert dataset["candle_count"] == len(rows)
    assert store.db.candles.count_documents({}) == len(rows)


def test_concurrent_import(store, settings, rows):
    svc = Service(settings, store, None)

    def run(_):
        return svc.upload(json.dumps(rows).encode(), "test.json", "BTCUSDT", "5m")["id"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(run, range(16)))
    assert len(set(ids)) == 1
    assert store.db.candles.count_documents({}) == len(rows)
    assert store.db.datasets.count_documents({}) == 1


def test_paginated_candles_with_time_filters_and_redis_down(client, rows):
    dataset = upload(client, rows).json()
    path = f"/api/v1/datasets/{dataset['id']}/candles"
    first = client.get(path, params={"limit": 3}).json()
    second = client.get(path, params={"limit": 3, "cursor": first["next_cursor"]}).json()
    assert [r["open_time_ms"] for r in first["items"] + second["items"]] == [r[0] for r in rows[:6]]
    assert (
        client.get(path, params={"from_ms": rows[1][0], "to_ms": rows[3][0]}).json()["items"]
        == first["items"][1:3]
    )
    assert client.get(path, params={"cursor": "bad"}).status_code == 422
    assert client.get(path, params={"from_ms": 10, "to_ms": 1}).status_code == 422
    assert (
        client.get(path, params={"from_ms": 1, "cursor": first["next_cursor"]}).status_code == 422
    )


def test_published_only_after_all_rows(store, settings, rows, monkeypatch):
    original = store.db.candles.insert_many

    def inspect(*args, **kwargs):
        assert store.db.datasets.count_documents({"status": "published"}) == 0
        return original(*args, **kwargs)

    monkeypatch.setattr(store.db.candles, "insert_many", inspect)
    Service(settings, store, None).upload(json.dumps(rows).encode(), "test.json", "BTCUSDT", "5m")
    assert store.db.datasets.count_documents({"status": "published"}) == 1


def test_http_request_body_limit(client):
    maximum = client.app.state.service.settings.max_upload_bytes + 65536
    response = client.post("/api/v1/datasets", content=b"x" * (maximum + 1))
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["field"] == "file"


def test_streaming_request_body_limit(client):
    maximum = client.app.state.service.settings.max_upload_bytes + 65536

    def chunks():
        for _ in range(maximum // 65536 + 2):
            yield b"x" * 65536

    response = client.post(
        "/api/v1/datasets",
        content=chunks(),
        headers={"Content-Type": "multipart/form-data; boundary=test"},
    )
    assert response.status_code == 422
