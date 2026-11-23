"""Grid compositions retain limits, sizing, risk exits and shared inventory."""

import json
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from candlelab.domain.combinations import CombinedStrategy, CombinationDefinition, CustomParameters
from candlelab.domain.strategy_types import Evaluation
from test_tutorial import candles, run


def definition(grid_id="GRID_TRADING@1", nested=False):
    value = {
        "name": "Grade com filtro ADX",
        "kind": "custom",
        "members": [
            {"strategy_id": grid_id, "parameters": {"lookback": 5, "levels": 5}},
            {"strategy_id": "GRID_REGIME@1", "parameters": {"atr_period": 2, "adx_limit": "100"}},
        ],
    }
    if grid_id == "GRID_ADX_ATR@1":
        value["members"][0]["parameters"].update(atr_period=2, atr_multiple="0.1", adx_limit="100")
    if nested:
        return {
            **value,
            "members": [
                {"combination": value},
                {
                    "strategy_id": "GRID_REGIME@1",
                    "parameters": {"atr_period": 2, "adx_limit": "100"},
                },
            ],
        }
    return value


def instance(value=None):
    return CombinedStrategy(CustomParameters(definition_json=json.dumps(value or definition())))


@pytest.mark.parametrize("grid_id", ["GRID_TRADING@1", "GRID_ADX_ATR@1"])
@pytest.mark.parametrize("nested", [False, True])
def test_grid_limits_sizing_and_fills_match_published_execution(grid_id, nested):
    data = candles([100] * 70)
    value = definition(grid_id, nested)
    result = run("CUSTOM_COMBINATION@1", data, {"definition_json": json.dumps(value)})
    base = definition(grid_id)["members"][0]
    original = run(grid_id, data, base["parameters"])
    fields = ("side", "price", "quantity", "fee", "open_time_ms")

    def fills(output):
        return [
            {key: event.get(key) for key in fields}
            for event in output["events"]
            if event["kind"] == "fill"
        ]

    assert fills(result) and fills(result) == fills(original)
    assert result["summary"] == original["summary"]
    prefix = run("CUSTOM_COMBINATION@1", data[:-5], {"definition_json": json.dumps(value)})
    assert result["equity"][:-5] == prefix["equity"]
    assert result["indicators"][:-5] == prefix["indicators"]


def test_orders_filters_market_exits_and_protective_grid_exit():
    strategy = instance()
    order = {"limit_price": D("99.6"), "installments": 5}
    state = [
        Evaluation({}, True, "BUY", "GRID_BUY", order),
        Evaluation({}, True, None, "FILTER", buy_allowed=True),
    ]
    strategy.strategies = [SimpleNamespace(on_close=lambda _, i=i: state[i]) for i in range(2)]
    assert strategy.on_close(None).order == order
    state[1] = Evaluation({}, True, None, "FILTER", buy_allowed=False)
    assert strategy.on_close(None).side is None
    state[0] = Evaluation({}, True, "SELL", "GRID_TARGET", {"limit_price": D("101")})
    assert strategy.on_close(None).order == {"limit_price": D("101")}
    state[0] = Evaluation({}, True, "SELL", "STOP_CLOSE")
    state[1] = Evaluation({}, False, None, "WARMUP")
    result = strategy.on_close(None)
    assert result.side == "SELL" and result.order is None


def test_other_signal_cannot_bypass_grid_entry_with_any_rule():
    value = definition()
    value["buy_rule"] = "any"
    value["members"][1] = {"strategy_id": "SMA_CROSS@1"}
    strategy = instance(value)
    strategy.strategies = [
        SimpleNamespace(on_close=lambda _: Evaluation({}, True, None, "GRID_DISABLED")),
        SimpleNamespace(on_close=lambda _: Evaluation({}, True, "BUY", "SMA")),
    ]
    assert strategy.on_close(None).side is None


def test_atr_market_exit_does_not_inherit_grid_limit():
    value = definition()
    value["members"].append({"strategy_id": "ATR_EXIT@1"})
    strategy = instance(value)
    results = [
        Evaluation({}, True, "SELL", "GRID_TARGET", {"limit_price": D("101")}),
        Evaluation({}, True, None, "FILTER", buy_allowed=False),
        Evaluation({}, True, "SELL", "ATR_STOP_CLOSE"),
    ]
    strategy.strategies = [SimpleNamespace(on_close=lambda _, i=i: results[i]) for i in range(3)]
    result = strategy.on_close(None)
    assert result.side == "SELL" and result.order is None


def test_api_creates_saved_grid_group_and_worker_executes(client, store, rows):
    from candlelab.worker import Worker
    from conftest import session, upload

    dataset = upload(client, rows).json()
    sess = session(client, dataset["id"]).json()
    response = client.post("/api/v1/strategy-combinations", json=definition(nested=True))
    assert response.status_code == 201, response.text
    response = client.post(
        f"/api/v1/sessions/{sess['id']}/runs", json={"combination_id": response.json()["id"]}
    )
    assert response.status_code == 202, response.text
    assert Worker(store).once()
    result = client.get("/api/v1/runs/" + response.json()["id"]).json()
    assert result["status"] == "completed", result


def test_multiple_grids_rejected_even_across_snapshots(client):
    value = definition(nested=True)
    value["members"].append(
        {
            "combination": {
                "name": "Outra grade",
                "kind": "strategy",
                "strategy_id": "GRID_TRADING@1",
            }
        }
    )
    with pytest.raises(ValidationError, match="somente uma grade"):
        CombinationDefinition.model_validate(value)
    response = client.post("/api/v1/strategy-combinations", json=value)
    assert response.status_code == 422


def test_catalog_marks_grids_combinable(client):
    catalog = {s["id"]: s for s in client.get("/api/v1/strategies").json()["items"]}
    assert catalog["GRID_TRADING@1"]["combinable"]
    assert catalog["GRID_ADX_ATR@1"]["combinable"]
    assert not catalog["DCA@1"]["combinable"]


@pytest.mark.parametrize("nested", [False, True])
def test_protective_grid_stop_overrides_all_rule_and_unready_signal(nested):
    value = definition(nested=nested)
    value["sell_rule"] = "all"
    value["members"].append({"strategy_id": "SMA_CROSS@1"})
    strategy = instance(value)
    grid = strategy.strategies[0]
    if nested:
        grid = grid.strategies[0]
    data = candles([100] * 6)
    for candle in data[:-1]:
        strategy.on_close(candle)
    grid.position, grid.entry = True, D("110")
    # Grid is ready but SMA is still warming up.
    result = strategy.on_close(data[-1])
    assert result.side == "SELL" and result.order is None
