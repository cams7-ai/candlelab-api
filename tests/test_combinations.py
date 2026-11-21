import json
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from candlelab.domain.combinations import CombinedStrategy, CombinationDefinition, CustomParameters
from candlelab.domain.strategy_types import Evaluation
from candlelab.worker import Worker
from conftest import session, upload


def custom(**extra):
    return {
        "name": "Minha combinação",
        "kind": "custom",
        "buy_rule": "all",
        "sell_rule": "any",
        "members": [
            {"strategy_id": key, "parameters": {"short_window": 2, "long_window": 3}}
            for key in ("SMA_CROSS@1", "EMA_CROSS@1")
        ],
        **extra,
    }


def test_crud_and_preset(client, store):
    path = "/api/v1/strategy-combinations"
    response = client.post(path, json=custom(name="  Minha combinação  "))
    assert response.status_code == 201, response.text
    saved = response.json()
    assert saved["name"] == "Minha combinação" and "_id" not in saved
    assert store.combination(saved["id"])["name"] == saved["name"]
    assert len(client.get(path).json()["items"]) == 1
    assert client.post(path, json=custom(name="MINHA COMBINAÇÃO")).status_code == 409
    preset = client.post(
        path, json={"name": "EMA ADX", "kind": "preset", "strategy_id": "EMA_ADX_ATR@1"}
    )
    assert preset.status_code == 201, preset.text
    assert preset.json()["parameters"]["short_window"] == 8
    changed = client.put(path + "/" + saved["id"], json=custom(name="Editada", buy_rule="any"))
    assert changed.status_code == 200 and changed.json()["buy_rule"] == "any"
    assert client.delete(path + "/" + saved["id"]).status_code == 204
    assert client.delete(path + "/" + saved["id"]).status_code == 404
    assert client.put(path + "/missing", json=custom()).status_code == 404


@pytest.mark.parametrize(
    "changes",
    [
        {"name": "  "},
        {"buy_rule": "majority"},
        {"members": []},
        {"members": [{"strategy_id": "SMA_CROSS@1"}] * 2},
        {"members": [{"strategy_id": "DCA@1"}, {"strategy_id": "SMA_CROSS@1"}]},
        {"members": [{"strategy_id": "CUSTOM_COMBINATION@1"}, {"strategy_id": "SMA_CROSS@1"}]},
        {
            "members": [
                {"strategy_id": "SMA_CROSS@1", "parameters": {"short_window": 8, "long_window": 2}},
                {"strategy_id": "EMA_CROSS@1"},
            ]
        },
        {"members": [{"strategy_id": "SCALPING@1"}, {"strategy_id": "SMA_CROSS@1"}]},
        {"members": [{"strategy_id": "missing"}, {"strategy_id": "SMA_CROSS@1"}]},
        {"strategy_id": "EMA_ADX_ATR@1"},
        {"extra": True},
    ],
)
def test_invalid_definitions(client, changes):
    response = client.post("/api/v1/strategy-combinations", json=custom(**changes))
    assert response.status_code == 422, response.text


def test_catalog_and_preset_validation(client):
    items = client.get("/api/v1/strategies").json()["items"]
    assert len(items) == 34
    by_id = {item["id"]: item for item in items}
    assert by_id["SMA_CROSS@1"]["combinable"]
    assert not by_id["GRID_ADX_ATR@1"]["combinable"]
    assert not by_id["MACHINE_LEARNING@1"]["combinable"]
    for key in ("missing", "SMA_CROSS@1", "CUSTOM_COMBINATION@1"):
        assert (
            client.post(
                "/api/v1/strategy-combinations",
                json={"kind": "preset", "name": "Inválida", "strategy_id": key},
            ).status_code
            == 422
        )


@pytest.mark.parametrize(
    "buy,sell,sides,ready,expected",
    [
        ("all", "any", ["BUY", None], True, None),
        ("any", "any", ["BUY", None], True, "BUY"),
        ("all", "all", ["BUY", "BUY"], True, "BUY"),
        ("all", "all", ["SELL", None], True, None),
        ("any", "all", ["SELL", "SELL"], True, "SELL"),
        ("any", "any", ["BUY", "SELL"], True, "SELL"),
        ("any", "any", ["BUY", "BUY"], False, None),
    ],
)
def test_votes_warmup_and_shared_fills(buy, sell, sides, ready, expected):
    params = CustomParameters(definition_json=json.dumps(custom(buy_rule=buy, sell_rule=sell)))
    strategy = CombinedStrategy(params)
    seen, fills = [], []
    candle = object()

    def component(index):
        def close(value):
            seen.append(value)
            return Evaluation(
                {"sma_short": Decimal(index + 1)}, ready or index == 0, sides[index], "TEST"
            )

        return SimpleNamespace(on_close=close, on_fill=lambda *args: fills.append((index, args)))

    strategy.strategies = [component(0), component(1)]
    result = strategy.on_close(candle)
    assert result.side == expected and result.ready == ready
    assert seen == [candle, candle]
    assert len(result.values) == 2
    strategy.on_fill("BUY", Decimal("100"))
    assert fills == [(0, ("BUY", Decimal("100"))), (1, ("BUY", Decimal("100")))]


@pytest.mark.parametrize(
    "definition",
    [
        custom(buy_rule="any"),
        {
            "name": "Preset",
            "kind": "preset",
            "strategy_id": "EMA_ADX_ATR@1",
            "parameters": {"short_window": 2, "long_window": 3},
        },
    ],
)
def test_run_snapshot_survives_changes_and_blocked_deletion(client, store, rows, definition):
    dataset = upload(client, rows).json()
    sess = session(client, dataset["id"]).json()
    combo = client.post("/api/v1/strategy-combinations", json=definition).json()
    path = f"/api/v1/sessions/{sess['id']}/runs"
    body = {"combination_id": combo["id"]}
    headers = {"Idempotency-Key": "combination-test"}
    response = client.post(path, json=body, headers=headers)
    assert response.status_code == 202, response.text
    run = response.json()
    assert client.post(path, json=body, headers=headers).json()["id"] == run["id"]
    assert run["combination_snapshot"] == combo
    original = run["parameters"].copy()
    assert (
        client.put(
            "/api/v1/strategy-combinations/" + combo["id"], json={**definition, "name": "Changed"}
        ).status_code
        == 200
    )
    assert client.post(path, json=body, headers=headers).status_code == 409
    assert client.delete("/api/v1/strategy-combinations/" + combo["id"]).status_code == 409
    assert Worker(store).once()
    result = client.get("/api/v1/runs/" + run["id"]).json()
    assert result["status"] == "completed", result
    assert result["parameters"] == original
    assert result["combination_snapshot"] == combo
    store.delete_run(run["id"])
    assert client.delete("/api/v1/strategy-combinations/" + combo["id"]).status_code == 204
    assert client.post(path, json=body).status_code == 404
    assert client.post(path, json={**body, "strategy_id": "SMA_CROSS@1"}).status_code == 422
    assert client.post(path, json={**body, "parameters": {"short_window": 2}}).status_code == 422


def test_direct_custom_definition_rejects_recursion():
    with pytest.raises(ValidationError):
        CombinationDefinition.model_validate(
            custom(members=[{"strategy_id": "CUSTOM_COMBINATION@1"}] * 2)
        )


def test_cors_allows_editing(client):
    response = client.options(
        "/api/v1/strategy-combinations/test",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "Content-Type",
        },
    )
    assert response.status_code == 200
    assert "PUT" in response.headers["access-control-allow-methods"]


def test_composition_preserves_causality_fills_and_money(rows):
    from candlelab.domain.candles import parse_candles
    from candlelab.domain.engine import simulate

    candles = parse_candles(rows, "5m", 100000)
    parameters = {"definition_json": json.dumps(custom(buy_rule="any"))}

    def run(data):
        return simulate(
            data,
            "CUSTOM_COMBINATION@1",
            parameters,
            Decimal("1000"),
            Decimal("0.001"),
            Decimal("0.000001"),
        )

    full = run(candles)
    prefix = run(candles[:-2])
    assert full["indicators"][: len(candles) - 2] == prefix["indicators"]
    assert full["equity"][: len(candles) - 2] == prefix["equity"]
    fills = [event for event in full["events"] if event["kind"] == "fill"]
    assert fills
    signals = {event["id"]: event for event in full["events"] if event["kind"] == "signal"}
    for fill in fills:
        signal = signals[fill["signal_id"]]
        assert fill["open_time_ms"] == signal["open_time_ms"] + 300000
        assert fill["occurred_at_ms"] > signal["occurred_at_ms"]
    summary = full["summary"]
    assert Decimal(summary["final_equity"]) - Decimal(summary["initial_cash"]) == Decimal(
        summary["realized_pnl"]
    ) + Decimal(summary["unrealized_pnl"])
    assert Decimal(summary["total_fees"]) > 0
