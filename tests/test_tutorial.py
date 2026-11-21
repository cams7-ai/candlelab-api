"""Release 0.2: mathematical fixtures, causal runs and accounting invariants."""

import json
import math
from decimal import Decimal as D, localcontext

import pytest

from candlelab.domain.candles import parse_candles
from candlelab.domain.engine import simulate
from candlelab.domain.errors import AppError
from candlelab.domain.indicators import Bands, MACD, Volatility
from candlelab.domain.numbers import INDICATOR_CONTEXT
from candlelab.domain.strategies import catalog, resolve
from candlelab.worker import Worker
from conftest import session, upload

IDS = [item["id"] for item in catalog() if item["category"] != "legacy"]


def candles(prices, volume=None):
    return parse_candles(
        [
            [
                60000 * (i + 1),
                str(p),
                str(D(str(p)) + 1),
                str(D(str(p)) - 1),
                str(p),
                str(volume[i] if volume else 10 + i % 7),
                60000 * (i + 2) - 1,
                "100",
                1,
                "1",
                "1",
                "0",
            ]
            for i, p in enumerate(prices)
        ],
        "1m",
        1000,
    )


def auxiliary(data, **changes):
    return [
        dict(
            open_time_ms=c.open_time_ms,
            bid=str(c.open - D(".1")),
            ask=str(c.open + D(".1")),
            bid_b=str(c.open + D(1)),
            ask_b=str(c.open + D("1.1")),
            capacity_base="100",
            maker_buy_filled=True,
            maker_sell_filled=True,
            flow="1",
            eth_price="10",
            eth_close="10",
            probability="0.7" if i % 12 < 6 else "0.2",
            trained_until_ms=0,
            model_id="synthetic-test",
            universe={
                "BTCUSDT": {"roc": "5", "liquidity": "2000000"},
                "ETHUSDT": {"roc": "2", "liquidity": "2000000"},
            },
            **{},
        )
        | changes
        for i, c in enumerate(data)
    ]


def run(kind, data, parameters=None, rows=None):
    params = parameters or {}
    if resolve_spec(kind).required_data:
        params = dict(
            params, auxiliary_json=json.dumps(rows if rows is not None else auxiliary(data))
        )
    return simulate(data, kind, params, D(1000), D(".001"), D(".000001"))


def resolve_spec(kind):
    from candlelab.domain.strategies import REGISTRY

    return REGISTRY[kind]


def test_catalog_matches_all_tutorial_sections():
    items = catalog()
    assert len(items) == 34
    assert sum(i["category"] == "individual" for i in items) == 24
    assert sum(i["category"] == "combination" for i in items) == 6
    assert len({i["id"] for i in items}) == 34


def test_bollinger_population_variance_wilder_and_macd():
    with localcontext(INDICATOR_CONTEXT):
        bands = Bands(3, "2")
        assert bands.update(D(1)) == (None, None, None)
        bands.update(D(2))
        mean, low, high = bands.update(D(3))
        assert mean == 2
        assert low == 2 - 2 * (D(2) / 3).sqrt()
        assert high == 2 + 2 * (D(2) / 3).sqrt()
        volatility = Volatility(2)
        observed = [volatility.update(c) for c in candles([10, 12, 14, 16])]
        assert observed == [(None, None), (None, None), (D(3), None), (D(3), D(100))]
        macd = MACD()
        observed = [macd.update(D(10)) for _ in range(35)]
        assert observed[24] == (None, None)
        assert observed[25] == (D(0), None)
        assert observed[33] == (D(0), D(0))


@pytest.mark.parametrize("kind", IDS)
def test_every_strategy_is_deterministic_causal_and_reconciles(kind):
    prices = [round(100 + 8 * math.sin(i / 5) + i / 20, 4) for i in range(120)]
    data = candles(prices)
    first = run(kind, data)
    assert first == run(kind, data)
    changed = candles(prices[:80] + [round(p + 50, 4) for p in prices[80:]])
    future = run(kind, changed)
    assert first["indicators"][:80] == future["indicators"][:80]
    assert first["equity"][:80] == future["equity"][:80]
    assert first["evaluations"][:80] == future["evaluations"][:80]
    summary = first["summary"]
    assert D(summary["final_equity"]) - 1000 == D(summary["realized_pnl"]) + D(
        summary["unrealized_pnl"]
    )
    assert D(summary["final_cash"]) >= 0
    with localcontext() as ctx:
        ctx.prec = 120
        assert sum(D(item["cash_delta"]) for item in first["ledger"]) == D(summary["final_cash"])
        assert sum(D(item["fee"]) for item in first["ledger"]) == D(summary["total_fees"])
    signals = {e["id"]: e for e in first["events"] if e["kind"] == "signal"}
    for fill in (e for e in first["events"] if e["kind"] == "fill"):
        signal = signals[fill["signal_id"]]
        assert fill["open_time_ms"] == signal["open_time_ms"] + 60000
        assert fill["occurred_at_ms"] > signal["occurred_at_ms"]


def test_dca_three_equal_budgets_and_weighted_quantity():
    result = run("DCA@1", candles([100, 100, 200, 200, 50, 50, 50]), {"every_candles": 2})
    fills = [e for e in result["events"] if e["kind"] == "fill"]
    assert len(fills) == 3 and all(e["side"] == "BUY" for e in fills)
    assert [e["price"] for e in fills] == ["100", "200", "50"]
    assert [e["quantity"] for e in fills] == ["3.330003", "1.665001", "6.660006"]
    assert result["summary"]["final_position_qty"] == "11.65501"
    assert result["summary"]["sell_fills"] == 0


def test_arbitrage_atomic_legs_net_costs_and_disappearing_edge():
    data = candles([100] * 4)
    result = run("ARBITRAGE@1", data)
    assert result["summary"]["closed_trades"] == 3
    assert result["summary"]["buy_fills"] == result["summary"]["sell_fills"] == 3
    assert D(result["summary"]["realized_pnl"]) > 0
    assert result["summary"]["final_position_qty"] == "0"
    rows = auxiliary(data)
    rows[1].update(bid_b="99.8", ask_b="100.1")
    result = run("ARBITRAGE@1", data, rows=rows)
    assert any(e.get("reason") == "NET_EDGE_DISAPPEARED" for e in result["events"])
    result = run("ARBITRAGE@1", data, {"transfer_rate": "0.1"})
    assert result["summary"]["closed_trades"] == 0


def test_passive_fills_require_explicit_confirmation():
    data = candles([100] * 6)
    result = run("MARKET_MAKING@1", data, rows=auxiliary(data, maker_buy_filled=False))
    assert result["summary"]["buy_fills"] == 0
    assert any(e.get("reason") == "PASSIVE_ORDER_NOT_FILLED" for e in result["events"])
    result = run("MARKET_MAKING@1", data)
    assert result["summary"]["buy_fills"] == 3
    assert result["summary"]["sell_fills"] == 2


def test_pairs_has_both_assets_and_borrow_costs():
    data = candles([100] * 20 + [80, 80, 100, 100, 100])
    result = run("PAIRS_TRADING@1", data, {"period": 10, "entry_z": "1", "stop_z": "5"})
    fills = [e for e in result["events"] if e["kind"] == "fill"]
    assert {e["asset"] for e in fills} == {"BTC", "ETH"}
    assert [(e["side"], e["asset"]) for e in fills[:4]] == [
        ("BUY", "BTC"),
        ("SELL", "ETH"),
        ("SELL", "BTC"),
        ("BUY", "ETH"),
    ]
    assert any(e["kind"] == "BORROW_FEE" for e in result["ledger"])
    assert result["summary"]["closed_trades"] == 1


def test_breakout_uses_previous_window_and_volume_filter():
    data = candles([100] * 30 + [110, 111, 112], [10] * 30 + [30, 10, 10])
    result = run("BREAKOUT_VOLUME_ATR@1", data, {"lookback": 5, "volume_period": 5})
    signals = [e for e in result["events"] if e["kind"] == "signal"]
    assert signals[0]["side"] == "BUY" and signals[0]["open_time_ms"] == data[30].open_time_ms
    blocked = run(
        "BREAKOUT_VOLUME_ATR@1", data, {"lookback": 5, "volume_period": 5, "volume_multiple": "4"}
    )
    assert blocked["summary"]["buy_fills"] == 0


def test_ml_thresholds_and_no_future_training():
    data = candles([100] * 5)
    rows = auxiliary(data)
    rows[2]["probability"] = "0.2"
    result = run("MACHINE_LEARNING@1", data, rows=rows)
    assert result["summary"]["closed_trades"] == 1
    rows[0]["trained_until_ms"] = rows[0]["open_time_ms"]
    with pytest.raises(AppError, match="treinado"):
        run("MACHINE_LEARNING@1", data, rows=rows)


@pytest.mark.parametrize(
    "kind, params",
    [
        ("MACHINE_LEARNING@1", {}),
        ("PAIRS_TRADING@1", {"auxiliary_json": "{}"}),
        ("TREND_FOLLOWING@1", {"short_window": 21, "long_window": 8}),
        ("DCA@1", {"every_candles": True}),
        ("MOMENTUM@1", {"stop_pct": "0"}),
        ("MACD_EMA_RSI@1", {"rsi_min": "70", "rsi_max": "40"}),
    ],
)
def test_invalid_parameters(kind, params):
    with pytest.raises(AppError):
        resolve(kind, params)


def test_auxiliary_http_validation_and_worker_snapshot(client, store, rows):
    dataset = upload(client, rows).json()
    sess = session(client, dataset["id"]).json()
    path = f"/api/v1/sessions/{sess['id']}/runs"
    response = client.post(path, json={"strategy_id": "MACHINE_LEARNING@1"})
    assert response.status_code == 422
    observations = [
        dict(open_time_ms=r[0], probability="0.7", trained_until_ms=0, model_id="test")
        for r in rows
    ]
    raw = json.dumps(observations)
    bad = client.post(
        path,
        json={
            "strategy_id": "MACHINE_LEARNING@1",
            "parameters": {"auxiliary_json": json.dumps(observations[:-1])},
        },
    )
    assert bad.status_code == 422
    payload = {
        "strategy_id": "MACHINE_LEARNING@1",
        "parameters": {"auxiliary_json": raw},
        "auxiliary_filename": "previsões-btc.json",
    }
    headers = {"Idempotency-Key": "auxiliary-file"}
    response = client.post(path, json=payload, headers=headers)
    assert response.status_code == 202, response.text
    assert response.json()["parameters"]["auxiliary_json"] == raw
    assert response.json()["auxiliary_filename"] == "previsões-btc.json"
    assert client.post(path, json=payload, headers=headers).json()["id"] == response.json()["id"]
    assert (
        client.post(
            path, json={**payload, "auxiliary_filename": "outro.json"}, headers=headers
        ).status_code
        == 409
    )
    assert client.get(path).json()["items"][0]["auxiliary_filename"] == "previsões-btc.json"
    assert response.json()["policy"]["engine_version"] == "CANDLELAB_ENGINE@2"
    Worker(store).once()
    final = client.get(f"/api/v1/runs/{response.json()['id']}").json()
    assert final["status"] == "completed"
    assert final["auxiliary_filename"] == "previsões-btc.json"
    assert client.get("/openapi.json").json()["info"]["version"] == "0.3.0"


def test_hourly_candles_supported():
    from candlelab.config import Settings

    assert "1h" in Settings(_env_file=None).instrument("BTCUSDT").intervals
    data = [[3600000, "100", "101", "99", "100", "1", 7199999, "100", 1, "0", "0", "0"]]
    assert len(parse_candles(data, "1h", 10)) == 1


@pytest.mark.parametrize(
    "kind,prices,parameters",
    [
        (
            "MEAN_REVERSION@1",
            [100] * 30 + [80, 85, 90],
            {"period": 5, "deviations": "1", "adx_limit": "100"},
        ),
        (
            "RSI_BOLLINGER@1",
            [100] * 30 + [80, 85, 90],
            {"period": 5, "deviations": "1", "rsi_period": 2, "recovery": "20"},
        ),
        ("GRID_TRADING@1", [100] * 25, {}),
        ("GRID_ADX_ATR@1", [100] * 40, {"atr_multiple": "0.1"}),
        ("SWING_TRADING@1", [100] * 25 + [100.5, 100.8, 101.1, 100], {"min_reward_risk": "0.1"}),
        ("SCALPING@1", [100] * 25 + [95, 101, 102, 103], {"max_spread_bps": "30"}),
        ("TREND_FOLLOWING@1", [100] * 25 + [99, 98, 97, 98, 100, 105, 110, 115], {}),
        ("EMA_ADX_ATR@1", list(range(100, 160)), {}),
    ],
)
def test_specific_entries_and_actual_fills(kind, prices, parameters):
    from dataclasses import replace

    data = candles(prices)
    if kind == "EMA_ADX_ATR@1":
        data = [replace(c, open=c.open - D(".5")) for c in data]
    result = run(kind, data, parameters)
    assert result["summary"]["buy_fills"] > 0
    if kind in ("GRID_TRADING@1", "GRID_ADX_ATR@1", "SWING_TRADING@1"):
        assert result["summary"]["sell_fills"] > 0
    if kind.startswith("GRID"):
        # Intrabar limits are executed conservatively and never above the BUY limit.
        fills = [e for e in result["events"] if e["kind"] == "fill"]
        assert all(e["occurred_at_ms"] >= e["open_time_ms"] for e in fills)
        assert D(fills[1]["price"]) > D(fills[0]["price"])


def test_auxiliary_duplicates_quotes_and_missing_fields_rejected():
    data = candles([100] * 5)
    observations = auxiliary(data)
    observations[1]["open_time_ms"] = observations[0]["open_time_ms"]
    with pytest.raises(AppError, match="únicos"):
        run("ARBITRAGE@1", data, rows=observations)
    observations = auxiliary(data)
    observations[0]["bid"] = "102"
    with pytest.raises(AppError, match="bid <= ask"):
        run("ARBITRAGE@1", data, rows=observations)
    observations = auxiliary(data)
    del observations[0]["eth_close"]
    with pytest.raises(AppError, match="eth_close"):
        run("PAIRS_TRADING@1", data, rows=observations)
