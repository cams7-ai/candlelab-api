"""Grouping, component permissions and snapshot compatibility are execution contracts."""

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from candlelab.domain.combinations import CombinedStrategy, CombinationDefinition, CustomParameters
from candlelab.domain.strategy_types import Evaluation
from candlelab.domain.strategies import REGISTRY, catalog
from conftest import session, upload
from test_combinations import custom
from candlelab.worker import Worker


def group(definition):
    return {"combination": definition}


def strategy(definition):
    return CombinedStrategy(CustomParameters(definition_json=json.dumps(definition)))


def stub(side, ready=True, permission=None):
    return SimpleNamespace(
        on_close=lambda _: Evaluation({}, ready, side, "TEST", buy_allowed=permission)
    )


def test_nested_rules_are_not_flattened():
    # (A OR B) AND C differs from A AND B AND C and from A OR B OR C.
    inner = custom(buy_rule="any", sell_rule="all")
    outer = custom(members=[group(inner), {"strategy_id": "RSI_REVERSION@1"}])
    engine = strategy(outer)
    engine.strategies[0].strategies = [stub("BUY"), stub(None)]
    engine.strategies[1] = stub("BUY")
    assert engine.on_close(None).side == "BUY"
    engine.strategies[1] = stub(None)
    assert engine.on_close(None).side is None
    engine.strategies[0].strategies = [stub("SELL"), stub(None)]
    assert engine.on_close(None).side is None


@pytest.mark.parametrize("buy_rule", ["all", "any"])
def test_filters_gate_buys_without_becoming_sell_votes(buy_rule):
    engine = strategy(
        custom(
            buy_rule=buy_rule,
            sell_rule="all",
            members=[{"strategy_id": "SMA_CROSS@1"}, {"strategy_id": "RSI_CONFIRMATION@1"}],
        )
    )
    engine.strategies = [stub("BUY"), stub(None, permission=False)]
    assert engine.on_close(None).side is None
    engine.strategies[1] = stub(None, permission=True)
    assert engine.on_close(None).side == "BUY"
    engine.strategies = [stub("SELL"), stub(None, permission=False)]
    assert engine.on_close(None).side == "SELL"
    engine.strategies[1] = stub(None, ready=False, permission=True)
    assert not engine.on_close(None).ready


def test_exit_can_sell_but_cannot_supply_a_buy_vote():
    engine = strategy(
        custom(members=[{"strategy_id": "SMA_CROSS@1"}, {"strategy_id": "ATR_EXIT@1"}])
    )
    engine.strategies = [stub("BUY"), stub(None)]
    assert engine.on_close(None).side == "BUY"
    engine.strategies = [stub(None), stub("SELL")]
    assert engine.on_close(None).side == "SELL"


def test_rejects_missing_signal_incompatible_groups_and_bounds():
    with pytest.raises(ValidationError, match="sinal"):
        CombinationDefinition.model_validate(
            custom(members=[{"strategy_id": "ADX_CONFIRMATION@1"}, {"strategy_id": "ATR_EXIT@1"}])
        )
    with pytest.raises(ValidationError):
        CombinationDefinition.model_validate(
            custom(
                members=[
                    group({"name": "DCA", "kind": "strategy", "strategy_id": "DCA@1"}),
                    {"strategy_id": "SMA_CROSS@1"},
                ]
            )
        )
    definition = custom()
    for _ in range(3):
        definition = custom(members=[group(definition), {"strategy_id": "RSI_REVERSION@1"}])
    CombinationDefinition.model_validate(definition)
    with pytest.raises(ValidationError, match="4 níveis"):
        CombinationDefinition.model_validate(
            custom(members=[group(definition), {"strategy_id": "RSI_REVERSION@1"}])
        )
    branches = [group(custom(name=str(i))) for i in range(8)]
    broad = custom(members=[group(custom(name=str(i), members=branches)) for i in range(5)])
    with pytest.raises(ValidationError, match="64 estratégias"):
        CombinationDefinition.model_validate(broad)
    with pytest.raises(ValidationError):
        CombinationDefinition.model_validate(
            custom(
                members=[
                    {"strategy_id": "SMA_CROSS@1", "combination": custom()},
                    {"strategy_id": "RSI_REVERSION@1"},
                ]
            )
        )


def test_published_recipes_resolve_to_reusable_rules():
    for item in catalog():
        if item["category"] == "combination" or item["id"] == "SMA_RSI_FILTER@1":
            assert len(item["components"]) >= 2
            assert all(component["strategy_id"] in REGISTRY for component in item["components"])


def test_nested_snapshot_survives_source_changes_and_blocked_deletion(client, store, rows):
    path = "/api/v1/strategy-combinations"
    origin = client.post(path, json=custom(buy_rule="any", sell_rule="all")).json()
    snapshot = {key: value for key, value in origin.items() if key != "id"}
    child = custom(name="Derivada", members=[group(snapshot), {"strategy_id": "RSI_REVERSION@1"}])
    response = client.post(path, json=child)
    assert response.status_code == 201, response.text
    derived = response.json()
    assert derived["members"][0]["combination"]["sell_rule"] == "all"
    assert client.put(path + "/" + origin["id"], json=custom(name="Changed")).status_code == 200
    assert client.delete(path + "/" + origin["id"]).status_code == 409
    dataset = upload(client, rows).json()
    sess = session(client, dataset["id"]).json()
    response = client.post(
        f"/api/v1/sessions/{sess['id']}/runs", json={"combination_id": derived["id"]}
    )
    assert response.status_code == 202, response.text
    run = response.json()
    assert Worker(store).once()
    result = client.get("/api/v1/runs/" + run["id"]).json()
    assert result["status"] == "completed", result
    assert result["combination_snapshot"] == derived
    assert result["parameters"] == run["parameters"]


@pytest.mark.parametrize(
    "kind", ["EMA_ADX_ATR", "RSI_BOLLINGER", "BREAKOUT_VOLUME_ATR", "MACD_EMA_RSI"]
)
@pytest.mark.parametrize("case", ["oscillation", "trend", "recovery", "breakouts"])
def test_published_recipes_and_individual_compositions_have_identical_trades(kind, case):
    from test_composition_regression import scenarios
    from test_tutorial import run

    data, overrides = scenarios()[case]
    spec = REGISTRY[kind + "@1"]
    parameters = spec.parameters.model_validate(
        {key: value for key, value in overrides.items() if key in spec.parameters.model_fields}
    ).model_dump()
    members = []
    for component in spec.components:
        fields = REGISTRY[component["strategy_id"]].parameters.model_fields
        members.append(
            {
                "strategy_id": component["strategy_id"],
                "parameters": {key: value for key, value in parameters.items() if key in fields},
            }
        )
    definition = custom(members=members)
    published = run(kind + "@1", data, parameters)
    composed = run("CUSTOM_COMBINATION@1", data, {"definition_json": json.dumps(definition)})
    assert composed["summary"] == published["summary"]
    assert composed["equity"] == published["equity"]

    def fills(result):
        return [
            {key: value for key, value in event.items() if key not in ("id", "signal_id", "reason")}
            for event in result["events"]
            if event["kind"] == "fill"
        ]

    assert fills(composed) == fills(published)


def test_shared_fills_reach_every_nested_strategy():
    inner = custom(members=[{"strategy_id": "TREND_FOLLOWING@1"}, {"strategy_id": "MOMENTUM@1"}])
    engine = strategy(custom(members=[group(inner), {"strategy_id": "BOLLINGER_RECOVERY@1"}]))
    leaves = [*engine.strategies[0].strategies, engine.strategies[1]]
    engine.on_fill("BUY", Decimal("100"))
    assert all(leaf.position and leaf.entry == Decimal("100") for leaf in leaves)
    engine.on_fill("SELL", Decimal("110"))
    assert all(not leaf.position and leaf.entry is None for leaf in leaves)
