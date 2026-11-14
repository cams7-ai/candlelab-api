import json
from decimal import Decimal as D, localcontext
from pathlib import Path

import pytest

from candlelab.domain.candles import parse_candles
from candlelab.domain.engine import simulate
from candlelab.domain.errors import AppError
from candlelab.domain.numbers import INDICATOR_CONTEXT, MONEY_CONTEXT
from candlelab.domain.strategies import MovingAverage, resolve


PRICES = [100000, 90000, 80000, 100000, 120000, 100000, 80000]


def make_candles(prices=PRICES):
    return parse_candles(
        [
            [
                i * 60000,
                str(p),
                str(p),
                str(p),
                str(p),
                "1",
                i * 60000 + 59999,
                "1",
                1,
                "0",
                "0",
                "0",
            ]
            for i, p in enumerate(prices)
        ],
        "1m",
        1000,
    )


def evaluations(strategy_id, parameters, prices=PRICES):
    spec, p = resolve(strategy_id, parameters)
    strategy = spec.factory(p)
    return [strategy.on_close(c) for c in make_candles(prices)]


@pytest.mark.parametrize("kind", ["SMA_CROSS@1", "EMA_CROSS@1"])
def test_cross_warmup_equality_and_signals(kind):
    result = evaluations(kind, {"short_window": 2, "long_window": 3})
    assert [e.side for e in result] == [None, None, None, None, "BUY", None, "SELL"]
    assert [e.ready for e in result] == [False, False, False, True, True, True, True]
    prefix = "sma" if kind.startswith("SMA") else "ema"
    assert result[0].values[prefix + "_short"] is None
    assert result[1].values[prefix + "_short"] == D(95000)
    assert result[2].values[prefix + "_long"] == D(90000)
    assert result[3].values[prefix + "_long"] == (D(90000) if prefix == "sma" else D(95000))


def test_rsi_wilder_manual_and_edges():
    result = evaluations("RSI_REVERSION@1", {"period": 2})
    assert [e.side for e in result] == [None, None, None, "BUY", None, "SELL", None]
    with localcontext(INDICATOR_CONTEXT):
        assert [e.values["rsi"] for e in result[:4]] == [None, None, D(0), D(200) / 3]
    assert result[5].values["rsi"] == D(40)
    for prices, value in [([5, 5, 5], 50), ([1, 2, 3], 100), ([3, 2, 1], 0)]:
        assert evaluations("RSI_REVERSION@1", {"period": 1}, prices)[-1].values["rsi"] == value


def test_ema_window_one_and_seed():
    with localcontext(INDICATOR_CONTEXT):
        ema = MovingAverage(1, True)
        assert [ema.update(D(p)) for p in [4, 9, 2]] == list(map(D, [4, 9, 2]))


def test_combined_confirmation_and_exit():
    base = {"short_window": 2, "long_window": 3, "rsi_period": 2}
    allowed = evaluations("SMA_RSI_FILTER@1", {**base, "rsi_confirmation_min": "50"})
    blocked = evaluations("SMA_RSI_FILTER@1", {**base, "rsi_confirmation_min": "90"})
    assert allowed[4].side == "BUY"
    assert blocked[4].side is None and blocked[4].reason == "RSI_FILTER_BLOCKED"
    assert allowed[6].side == blocked[6].side == "SELL"
    unready = evaluations("SMA_RSI_FILTER@1", {**base, "rsi_period": 100})
    assert unready[4].side is None and unready[6].side == "SELL"


@pytest.mark.parametrize(
    "kind,params",
    [
        ("bad", {}),
        ("SMA_CROSS@1", {"short_window": True}),
        ("EMA_CROSS@1", {"long_window": 1}),
        ("RSI_REVERSION@1", {"period": 0}),
        ("RSI_REVERSION@1", {"oversold": "70", "overbought": "30"}),
        ("RSI_REVERSION@1", {"oversold": 30}),
        ("SMA_RSI_FILTER@1", {"rsi_confirmation_min": "101"}),
        ("SMA_CROSS@1", {"unexpected": 1}),
        ("SMA_CROSS@1", {"long_window": 10001}),
    ],
)
def test_invalid_strategy_parameters(kind, params):
    with pytest.raises(AppError):
        resolve(kind, params)


def test_execution_next_open_final_signal_and_reconciliation():
    candles = make_candles()
    result = simulate(
        candles,
        "SMA_CROSS@1",
        {"short_window": 2, "long_window": 3},
        D(1000),
        D("0.001"),
        D("0.000001"),
    )
    signals = [e for e in result["events"] if e["kind"] == "signal"]
    fills = [e for e in result["events"] if e["kind"] == "fill"]
    assert signals[0]["open_time_ms"] == candles[4].open_time_ms
    assert signals[0]["occurred_at_ms"] == candles[4].close_time_ms
    assert fills[0]["occurred_at_ms"] == candles[5].open_time_ms
    assert fills[0]["price"] == "100000" and fills[0]["quantity"] == "0.00999"
    assert fills[0]["signal_id"] == signals[0]["id"]
    assert result["events"][-1]["reason"] == "NO_FUTURE_OPEN"
    summary = result["summary"]
    assert summary["final_cash"] == "0.001"
    assert summary["total_fees"] == "0.999"
    assert summary["final_equity"] == "799.201"
    assert summary["unrealized_pnl"] == "-200.799"
    assert summary["realized_pnl"] == "0" and summary["win_rate_pct"] is None
    assert summary["max_drawdown_pct"] == "20.0799"
    assert len(result["ledger"]) == 2
    assert D(summary["final_equity"]) - D(summary["initial_cash"]) == D(
        summary["realized_pnl"]
    ) + D(summary["unrealized_pnl"])


def test_round_trip_fees_ledger_and_profit():
    result = simulate(
        make_candles(), "RSI_REVERSION@1", {"period": 2}, D(1000), D("0.001"), D("0.000001")
    )
    summary = result["summary"]
    assert summary["buy_fills"] == summary["sell_fills"] == summary["closed_trades"] == 1
    assert summary["final_position_qty"] == "0"
    assert summary["unrealized_pnl"] == "0"
    assert D(summary["final_cash"]) - 1000 == D(summary["realized_pnl"])
    assert sum(D(r["cash_delta"]) for r in result["ledger"]) == D(summary["final_cash"])
    assert sum(D(r["fee"]) for r in result["ledger"]) == D(summary["total_fees"])
    assert summary["win_rate_pct"] == "0"


def test_empty_trades_and_zero_quantity():
    result = simulate(make_candles([10] * 30), "SMA_CROSS@1", {}, D(1000), D(0), D(1))
    assert result["events"] == [] and result["summary"]["return_pct"] == "0"
    assert result["summary"]["max_drawdown_pct"] == "0"
    result = simulate(
        make_candles(), "SMA_CROSS@1", {"short_window": 2, "long_window": 3}, D("0.001"), D(0), D(1)
    )
    assert result["events"][1]["reason"] == "ZERO_QUANTITY"
    assert len(result["ledger"]) == 1
    assert simulate(make_candles(), "SMA_CROSS@1", {}, D(1000), D(0), D(1))["diagnostics"] == [
        "INSUFFICIENT_WARMUP"
    ]


def test_causality_context_isolation_and_reproducibility():
    params = {"short_window": 2, "long_window": 3}
    baseline = simulate(make_candles(), "EMA_CROSS@1", params, D(1000), D("0.001"), D("0.000001"))
    with localcontext() as context:
        context.prec = 4
        repeated = simulate(
            make_candles(), "EMA_CROSS@1", params, D(1000), D("0.001"), D("0.000001")
        )
        assert context.prec == 4
    assert repeated == baseline
    changed = simulate(
        make_candles(PRICES[:5] + [999, 999]),
        "EMA_CROSS@1",
        params,
        D(1000),
        D("0.001"),
        D("0.000001"),
    )
    assert baseline["indicators"][:5] == changed["indicators"][:5]
    assert baseline["evaluations"][:5] == changed["evaluations"][:5]


@pytest.mark.parametrize("symbol", ["btc", "eth"])
def test_fixture_financial_reconciliation(symbol):
    rows = json.loads((Path(__file__).parent / f"fixtures/{symbol}usdt-5m.json").read_text())
    for kind, params in [
        ("SMA_CROSS@1", {"short_window": 2, "long_window": 3}),
        ("EMA_CROSS@1", {"short_window": 2, "long_window": 3}),
        ("RSI_REVERSION@1", {"period": 2}),
        ("SMA_RSI_FILTER@1", {"short_window": 2, "long_window": 3, "rsi_period": 2}),
    ]:
        result = simulate(
            parse_candles(rows, "5m", 100), kind, params, D(1000), D("0.001"), D("0.000001")
        )
        with localcontext(MONEY_CONTEXT):
            summary = result["summary"]
            assert D(summary["final_equity"]) - 1000 == D(summary["realized_pnl"]) + D(
                summary["unrealized_pnl"]
            )
