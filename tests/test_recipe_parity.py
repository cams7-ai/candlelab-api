"""Audit parameter coverage and financial parity of published recipes/components."""

import json
import random
from dataclasses import replace
from decimal import Decimal as D

import pytest
from pydantic import ValidationError

from candlelab.domain.combinations import CombinationDefinition
from candlelab.domain.strategies import REGISTRY, catalog
from test_composition_regression import scenarios
from test_tutorial import auxiliary, candles, run

RECIPES = ["EMA_ADX_ATR", "RSI_BOLLINGER", "BREAKOUT_VOLUME_ATR", "MACD_EMA_RSI", "GRID_ADX_ATR"]


def composed_definition(kind, parameters):
    members = []
    for component in REGISTRY[kind + "@1"].components:
        strategy_id = component["strategy_id"]
        fields = REGISTRY[strategy_id].parameters.model_fields
        values = {key: value for key, value in parameters.items() if key in fields}
        values.update(component.get("parameter_overrides", {}))
        members.append({"strategy_id": strategy_id, "parameters": values})
    return {"name": kind, "kind": "custom", "members": members}


def assert_parity(kind, data, overrides):
    spec = REGISTRY[kind + "@1"]
    parameters = spec.parameters.model_validate(
        {key: value for key, value in overrides.items() if key in spec.parameters.model_fields}
    ).model_dump()
    definition = composed_definition(kind, parameters)
    published = run(kind + "@1", data, parameters)
    composed = run("CUSTOM_COMBINATION@1", data, {"definition_json": json.dumps(definition)})
    assert composed["equity"] == published["equity"]
    assert composed["summary"] == published["summary"]

    def fills(result):
        return [
            {key: value for key, value in event.items() if key not in ("id", "signal_id", "reason")}
            for event in result["events"]
            if event["kind"] == "fill"
        ]

    assert fills(composed) == fills(published)
    return composed, definition


@pytest.mark.parametrize("kind", RECIPES)
@pytest.mark.parametrize("case", list(scenarios()))
def test_recipe_financial_parity_including_grid(kind, case):
    data, overrides = scenarios()[case]
    assert_parity(kind, data, overrides)


@pytest.mark.parametrize("kind", [*RECIPES, "SMA_RSI_FILTER"])
@pytest.mark.parametrize("seed", range(6))
def test_recipe_parity_with_nondefault_parameters_and_market_paths(kind, seed):
    rng = random.Random(seed)
    price = 100
    prices = []
    for _ in range(160):
        price = max(15, price + rng.uniform(-3, 3))
        prices.append(round(price, 4))
    data = [replace(c, open=c.open - D(".25")) for c in candles(prices)]
    assert_parity(
        kind,
        data,
        {
            "stop_pct": "1.7",
            "short_window": 2,
            "long_window": 5,
            "atr_period": 3,
            "atr_multiple": "0.35",
            "adx_limit": "40",
            "rsi_period": 3,
            "recovery": "40",
            "period": 5,
            "deviations": "1.2",
            "lookback": 7,
            "levels": 4,
            "volume_period": 3,
            "volume_multiple": "0.7",
            "rsi_min": "20",
            "rsi_max": "80",
            "ema_period": 4,
        },
    )


@pytest.mark.parametrize("kind", [*RECIPES, "MOMENTUM_RANKING", "SMA_RSI_FILTER"])
def test_every_published_parameter_exists_in_its_individual_components(kind):
    spec = REGISTRY[kind + "@1"]
    fields = {
        key
        for member in spec.components
        for key in REGISTRY[member["strategy_id"]].parameters.model_fields
    }
    assert set(spec.parameters.model_fields) <= fields


def test_ranking_filter_is_combinable_but_published_recipe_is_not():
    items = {item["id"]: item for item in catalog()}
    assert items["RANKING_CONFIRMATION@1"]["required_data"] == ["universe"]
    assert items["RANKING_CONFIRMATION@1"]["combinable"]
    assert not items["MOMENTUM_RANKING@1"]["combinable"]
    data = candles([100] * 30)
    definition = composed_definition(
        "MOMENTUM_RANKING", {"auxiliary_json": json.dumps(auxiliary(data))}
    )
    CombinationDefinition.model_validate(definition)


def test_atr_spacing_changes_limits_and_is_causal():
    params = {"lookback": 5, "atr_period": 3, "adx_limit": "100", "atr_multiple": "0.1"}
    data = candles([100] * 50)
    composed, definition = assert_parity("GRID_ADX_ATR", data, params)
    fills = [event for event in composed["events"] if event["kind"] == "fill"]
    assert fills and D(fills[0]["price"]) == D("99.8")
    prefix = run("CUSTOM_COMBINATION@1", data[:-4], {"definition_json": json.dumps(definition)})
    assert composed["equity"][:-4] == prefix["equity"]
    assert composed["indicators"][:-4] == prefix["indicators"]


def test_adx_disables_grid_permanently_and_exits_at_market():
    from candlelab.domain.combinations import CombinedStrategy, CustomParameters
    from candlelab.domain.strategy_types import Evaluation

    parameters = (
        REGISTRY["GRID_ADX_ATR@1"]
        .parameters(lookback=5, atr_period=3, atr_multiple="0.1")
        .model_dump()
    )
    definition = composed_definition("GRID_ADX_ATR", parameters)
    strategy = CombinedStrategy(CustomParameters(definition_json=json.dumps(definition)))
    data = candles([100] * 15)
    for candle in data[:10]:
        strategy.on_close(candle)
    grid, regime = strategy.strategies
    strategy.on_fill("BUY", D("99.8"))
    # A high ADX must disable the grid even with a non-triggered percentage stop.
    original = regime.on_close

    def regime_result(candle, allowed):
        original(candle)
        regime.last_evaluation = Evaluation({}, True, None, "TEST", buy_allowed=allowed)
        return regime.last_evaluation

    regime.on_close = lambda candle: regime_result(candle, False)
    result = strategy.on_close(data[10])
    assert result.side == "SELL" and result.order is None
    assert grid.disabled
    strategy.on_fill("SELL", D("100"))
    regime.on_close = lambda candle: regime_result(candle, True)
    assert strategy.on_close(data[11]).side is None
    assert grid.disabled


def test_grid_legacy_defaults_and_invalid_modes():
    spec = REGISTRY["GRID_TRADING@1"]
    assert spec.parameters().spacing_mode == "range"
    assert REGISTRY["GRID_REGIME@1"].parameters().regime_action == "filter"
    with pytest.raises(ValidationError):
        spec.parameters(spacing_mode="unsupported")
    with pytest.raises(ValidationError):
        REGISTRY["GRID_REGIME@1"].parameters(regime_action="unsupported")
    value = composed_definition("GRID_ADX_ATR", {})
    value["members"][0] = {"strategy_id": "SMA_CROSS@1"}
    with pytest.raises(ValidationError, match="exige uma grade"):
        CombinationDefinition.model_validate(value)


def test_saved_atr_grid_options_survive_api_and_worker(client, store, rows):
    from conftest import session, upload
    from candlelab.worker import Worker

    definition = composed_definition(
        "GRID_ADX_ATR", {"lookback": 5, "atr_period": 3, "atr_multiple": "0.25", "adx_limit": "100"}
    )
    response = client.post("/api/v1/strategy-combinations", json=definition)
    assert response.status_code == 201, response.text
    saved = response.json()
    assert saved["members"][0]["parameters"]["spacing_mode"] == "atr"
    assert saved["members"][1]["parameters"]["regime_action"] == "disable_grid"
    dataset = upload(client, rows).json()
    sess = session(client, dataset["id"]).json()
    response = client.post(
        f"/api/v1/sessions/{sess['id']}/runs", json={"combination_id": saved["id"]}
    )
    assert response.status_code == 202, response.text
    assert Worker(store).once()
    result = client.get("/api/v1/runs/" + response.json()["id"]).json()
    assert result["status"] == "completed", result
    effective = json.loads(result["parameters"]["definition_json"])
    assert effective["members"] == saved["members"]
    assert result["combination_snapshot"] == saved


def test_disabling_filter_can_control_a_grid_inside_a_saved_group():
    from candlelab.domain.combinations import CombinedStrategy, CustomParameters

    parameters = (
        REGISTRY["GRID_ADX_ATR@1"]
        .parameters(atr_period=3, lookback=5, adx_limit="100", atr_multiple="0.1")
        .model_dump()
    )
    base = composed_definition("GRID_ADX_ATR", parameters)
    # Keep the disabling regime outside the group containing the grid.
    inner = {
        **base,
        "members": [
            base["members"][0],
            {"strategy_id": "GRID_REGIME@1", "parameters": {"atr_period": 3, "adx_limit": "100"}},
        ],
    }
    outer = {**base, "members": [{"combination": inner}, base["members"][1]]}
    data = candles([100] * 40)
    published = run("GRID_ADX_ATR@1", data, parameters)
    composed = run("CUSTOM_COMBINATION@1", data, {"definition_json": json.dumps(outer)})
    assert composed["summary"] == published["summary"]
    assert composed["equity"] == published["equity"]
    strategy = CombinedStrategy(CustomParameters(definition_json=json.dumps(outer)))
    assert strategy.grid_strategy().grid_regimes == [strategy.strategies[1]]
