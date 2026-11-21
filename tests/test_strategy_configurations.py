import json
from types import SimpleNamespace

import pytest

from candlelab.domain.combinations import CombinedStrategy, CustomParameters
from candlelab.domain.strategy_types import Evaluation
from candlelab.worker import Worker
from conftest import session, upload
from test_combinations import custom


def configuration(strategy_id="SMA_CROSS@1", **changes):
    return {
        "name": "SMA personalizada",
        "kind": "strategy",
        "strategy_id": strategy_id,
        "parameters": {"short_window": 2, "long_window": 5},
        **changes,
    }


def test_crud_defaults_and_historical_worker_snapshot(client, store, rows):
    path = "/api/v1/strategy-combinations"
    catalog_before = client.get("/api/v1/strategies").json()
    response = client.post(path, json=configuration())
    assert response.status_code == 201, response.text
    saved = response.json()
    assert saved["kind"] == "strategy"
    assert client.get(path).json()["items"] == [saved]
    dataset = upload(client, rows).json()
    sess = session(client, dataset["id"]).json()
    run_response = client.post(
        f"/api/v1/sessions/{sess['id']}/runs", json={"combination_id": saved["id"]}
    )
    assert run_response.status_code == 202, run_response.text
    run = run_response.json()
    assert run["strategy_id"] == "SMA_CROSS@1"
    assert run["parameters"] == {"short_window": 2, "long_window": 5}
    response = client.put(
        path + "/" + saved["id"],
        json=configuration(parameters={"short_window": 3, "long_window": 8}),
    )
    assert response.status_code == 200, response.text
    assert response.json()["parameters"]["short_window"] == 3
    blocked = client.delete(path + "/" + saved["id"])
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["error"]["code"] == "CONFIGURATION_IN_USE"
    assert Worker(store).once()
    historical = client.get("/api/v1/runs/" + run["id"]).json()
    assert historical["status"] == "completed", historical
    assert historical["combination_snapshot"] == saved
    assert historical["parameters"] == run["parameters"]
    assert client.get("/api/v1/strategies").json() == catalog_before
    store.delete_run(run["id"])
    assert client.delete(path + "/" + saved["id"]).status_code == 204


@pytest.mark.parametrize(
    "changes",
    [
        {"strategy_id": "missing"},
        {"strategy_id": "CUSTOM_COMBINATION@1"},
        {"strategy_id": "EMA_ADX_ATR@1", "parameters": {}},
        {"parameters": {"short_window": 9, "long_window": 3}},
        {"parameters": {"short_window": 0}},
        {"members": [{"strategy_id": "EMA_CROSS@1"}]},
        {"buy_rule": "any"},
        {"name": " "},
    ],
)
def test_invalid_configurations_are_rejected(client, changes):
    assert (
        client.post("/api/v1/strategy-combinations", json=configuration(**changes)).status_code
        == 422
    )


def test_decimal_parameters_keep_exact_text_and_duplicate_names_rejected(client):
    definition = configuration(
        "RSI_REVERSION@1", parameters={"oversold": "25.123456789012", "overbought": "70"}
    )
    response = client.post("/api/v1/strategy-combinations", json=definition)
    assert response.status_code == 201, response.text
    assert response.json()["parameters"]["oversold"] == "25.123456789012"
    assert client.post("/api/v1/strategy-combinations", json=definition).status_code == 409


def test_saved_filter_gates_buys_without_becoming_a_signal(client):
    saved = configuration("ADX_CONFIRMATION@1", name="ADX forte", parameters={"adx_limit": "30"})
    response = client.post("/api/v1/strategy-combinations", json=saved)
    assert response.status_code == 201, response.text
    snapshot = {key: value for key, value in response.json().items() if key != "id"}
    definition = custom(
        buy_rule="any", members=[{"strategy_id": "SMA_CROSS@1"}, {"combination": snapshot}]
    )
    assert client.post("/api/v1/strategy-combinations", json=definition).status_code == 201
    engine = CombinedStrategy(CustomParameters(definition_json=json.dumps(definition)))
    engine.strategies = [
        SimpleNamespace(on_close=lambda _: Evaluation({}, True, "BUY", "TEST")),
        SimpleNamespace(on_close=lambda _: Evaluation({}, True, None, "TEST", buy_allowed=False)),
    ]
    assert engine.on_close(None).side is None
    engine.strategies[1] = SimpleNamespace(
        on_close=lambda _: Evaluation({}, True, None, "TEST", buy_allowed=True)
    )
    assert engine.on_close(None).side == "BUY"
    invalid = custom(members=[{"combination": snapshot}, {"strategy_id": "ATR_EXIT@1"}])
    assert client.post("/api/v1/strategy-combinations", json=invalid).status_code == 422


def test_external_configuration_keeps_data_requirements(client, rows):
    from candlelab.domain.candles import parse_candles
    from test_tutorial import auxiliary

    data = parse_candles(rows, "5m", 100000)
    observations = auxiliary(data)
    definition = configuration(
        "MACHINE_LEARNING@1", parameters={"auxiliary_json": json.dumps(observations)}
    )
    path = "/api/v1/strategy-combinations"
    response = client.post(path, json=definition)
    assert response.status_code == 201, response.text
    saved = response.json()
    dataset = upload(client, rows).json()
    sess = session(client, dataset["id"]).json()
    run = client.post(f"/api/v1/sessions/{sess['id']}/runs", json={"combination_id": saved["id"]})
    assert run.status_code == 202, run.text
    snapshot = {key: value for key, value in saved.items() if key != "id"}
    invalid = custom(members=[{"combination": snapshot}, {"strategy_id": "SMA_CROSS@1"}])
    assert client.post(path, json=invalid).status_code == 422
    bad_data = configuration("MACHINE_LEARNING@1", parameters={"auxiliary_json": "[]"})
    assert client.post(path, json=bad_data).status_code == 422
