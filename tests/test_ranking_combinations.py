"""Ranking combinations validate auxiliary snapshots before queueing and gate entries."""

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from candlelab.domain.combinations import CombinedStrategy, CustomParameters
from candlelab.domain.strategy_types import Evaluation
from candlelab.worker import Worker
from conftest import session, upload
from test_tutorial import auxiliary, candles


def definition(observations, nested=False):
    result = {
        "name": "SMA com ranking",
        "kind": "custom",
        "buy_rule": "any",
        "members": [
            {"strategy_id": "SMA_CROSS@1", "parameters": {"short_window": 2, "long_window": 3}},
            {
                "strategy_id": "RANKING_CONFIRMATION@1",
                "parameters": {
                    "auxiliary_json": json.dumps(observations),
                    "top_n": 1,
                },
            },
        ],
    }
    if nested:
        return {
            "name": "Grupo",
            "kind": "custom",
            "members": [
                {"combination": result},
                {"strategy_id": "EMA_CROSS@1"},
            ],
        }
    return result


def observations(rows):
    return [
        {
            "open_time_ms": row[0],
            "universe": {
                "BTCUSDT": {"roc": "5", "liquidity": "2000000"},
                "ETHUSDT": {"roc": "2", "liquidity": "2000000"},
            },
        }
        for row in rows
    ]


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("saved", [False, True])
def test_ranking_combination_runs_with_immutable_auxiliary_snapshot(
    client, store, rows, nested, saved
):
    dataset = upload(client, rows).json()
    sess = session(client, dataset["id"]).json()
    combo = definition(observations(rows), nested)
    if saved:
        response = client.post("/api/v1/strategy-combinations", json=combo)
        assert response.status_code == 201, response.text
        body = {"combination_id": response.json()["id"]}
    else:
        body = {
            "strategy_id": "CUSTOM_COMBINATION@1",
            "parameters": {
                "definition_json": json.dumps(combo),
            },
        }
    response = client.post(
        f"/api/v1/sessions/{sess['id']}/runs", json=body, headers={"Idempotency-Key": "ranking"}
    )
    assert response.status_code == 202, response.text
    assert response.json()["policy"]["strategy_data_policy"] == "AUXILIARY_OPEN_SNAPSHOT@1"
    assert Worker(store).once()
    result = store.run(response.json()["id"])
    assert result["status"] == "completed", result


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("issue", ["missing", "extra", "other_symbol"])
def test_invalid_ranking_dataset_is_rejected_before_queueing(client, rows, nested, issue):
    dataset = upload(
        client, rows, symbol="ethusdt" if issue == "other_symbol" else "btcusdt"
    ).json()
    sess = session(client, dataset["id"]).json()
    data = observations(rows)
    if issue == "missing":
        data.pop()
    elif issue == "extra":
        data.append({**data[-1], "open_time_ms": data[-1]["open_time_ms"] + 300000})
    response = client.post("/api/v1/strategy-combinations", json=definition(data, nested))
    assert response.status_code == 201, response.text
    response = client.post(
        f"/api/v1/sessions/{sess['id']}/runs",
        json={"combination_id": response.json()["id"]},
        headers={"Idempotency-Key": "invalid-ranking"},
    )
    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    "universe",
    [
        {},
        {"ETHUSDT": {"roc": "2", "liquidity": "100"}},
        {"BTCUSDT": {"roc": "2", "liquidity": "100"}},
    ],
)
def test_invalid_ranking_universe_cannot_be_saved(client, universe):
    response = client.post(
        "/api/v1/strategy-combinations",
        json=definition(
            [
                {"open_time_ms": 60000, "universe": universe},
            ]
        ),
    )
    assert response.status_code == 422, response.text


def test_ranking_gates_any_buy_without_creating_sell_votes():
    data = candles([100, 100, 100])
    rows = auxiliary(data)
    rows[1]["universe"]["BTCUSDT"]["roc"] = "1"
    rows[2]["universe"]["BTCUSDT"]["roc"] = "1"
    strategy = CombinedStrategy(CustomParameters(definition_json=json.dumps(definition(rows))))
    signals = iter(["BUY", "BUY", "SELL"])
    strategy.strategies[0] = SimpleNamespace(
        on_close=lambda c: Evaluation({}, True, next(signals), "TEST")
    )
    results = [strategy.on_close(c) for c in data]
    assert [r.side for r in results] == ["BUY", None, "SELL"]
    assert [r.values["member_2__rank"] for r in results] == [Decimal(1), Decimal(2), Decimal(2)]


@pytest.mark.parametrize("nested", [False, True])
def test_auxiliary_filename_survives_saved_combination_and_run_snapshot(client, rows, nested):
    dataset = upload(client, rows).json()
    sess = session(client, dataset["id"]).json()
    payload = definition(observations(rows), nested)
    leaf = payload["members"][0]["combination"] if nested else payload
    leaf["members"][1]["auxiliary_filename"] = "ranking-importado.json"
    response = client.post("/api/v1/strategy-combinations", json=payload)
    assert response.status_code == 201, response.text
    saved = response.json()
    response = client.post(
        f"/api/v1/sessions/{sess['id']}/runs",
        json={"combination_id": saved["id"]},
        headers={"Idempotency-Key": "ranking-filename"},
    )
    assert response.status_code == 202, response.text
    run = response.json()
    assert run["combination_snapshot"] == saved
    snapshot = json.loads(run["parameters"]["definition_json"])
    leaf = snapshot["members"][0]["combination"] if nested else snapshot
    assert leaf["members"][1]["auxiliary_filename"] == "ranking-importado.json"
    assert "auxiliary_filename" not in leaf["members"][1]["parameters"]
    leaf["members"][1]["auxiliary_filename"] = "novo.json"
    assert run["combination_snapshot"] == saved


def test_saved_strategy_copies_auxiliary_filename_to_run(client, rows):
    dataset = upload(client, rows).json()
    sess = session(client, dataset["id"]).json()
    payload = {
        "name": "Ranking com arquivo",
        "kind": "strategy",
        "strategy_id": "RANKING_CONFIRMATION@1",
        "parameters": {"auxiliary_json": json.dumps(observations(rows))},
        "auxiliary_filename": "ranking-salvo.json",
    }
    response = client.post("/api/v1/strategy-combinations", json=payload)
    assert response.status_code == 201, response.text
    response = client.post(
        f"/api/v1/sessions/{sess['id']}/runs",
        json={"combination_id": response.json()["id"]},
        headers={"Idempotency-Key": "saved-ranking-filename"},
    )
    assert response.status_code == 202, response.text
    assert response.json()["auxiliary_filename"] == "ranking-salvo.json"
