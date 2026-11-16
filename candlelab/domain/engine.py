"""Motor Spot em memória. Nenhuma dependência de transporte ou persistência."""

from decimal import Decimal, Inexact, localcontext
from fractions import Fraction

from candlelab.domain.errors import invalid
from candlelab.domain.numbers import MONEY_CONTEXT, NUMERIC_POLICY, wire
from candlelab.domain.strategies import resolve

ENGINE_VERSION = "CANDLELAB_ENGINE@1"
EXECUTION_POLICY = "NEXT_OPEN_ALL_IN_KEEP_OPEN@1"


def simulate(
    candles, strategy_id, parameters, initial_cash, fee_rate, quantity_step, heartbeat=None
):
    spec, params = resolve(strategy_id, parameters)
    strategy = spec.factory(params)
    if strategy_id in ("ARBITRAGE@1", "PAIRS_TRADING@1"):
        from candlelab.domain.external_engine import simulate_external

        return simulate_external(
            candles, strategy, initial_cash, fee_rate, quantity_step, heartbeat
        )
    cash, qty, cost, fees, realized = initial_cash, Decimal(0), Decimal(0), Decimal(0), Decimal(0)
    peak, max_drawdown = initial_cash, Fraction(0)
    buys = sells = wins = 0
    pending = None
    events, indicators, equity, evaluations = [], [], [], []
    ledger = [
        {
            "seq": 0,
            "kind": "INITIAL_BALANCE",
            "cash_delta": wire(cash),
            "asset_delta": "0",
            "cash_after": wire(cash),
            "position_after": "0",
            "fee": "0",
        }
    ]
    observed_ready = False
    count = 0
    last = None

    def event(kind, side, anchor, at, **extra):
        seq = len(events) + 1
        item = {
            "id": f"event-{seq}",
            "seq": seq,
            "kind": kind,
            "side": side,
            "open_time_ms": anchor,
            "occurred_at_ms": at,
            **extra,
        }
        events.append(item)
        return item

    with localcontext(MONEY_CONTEXT) as context:
        context.traps[Inexact] = True
        for candle in candles:
            last = candle
            count += 1
            if heartbeat and count % 100 == 0:
                heartbeat()
            if pending:
                side, signal_id = pending["side"], pending["id"]
                reason = (
                    "POSITION_ALREADY_OPEN"
                    if side == "BUY" and qty and strategy_id != "DCA@1"
                    else ("NO_POSITION" if side == "SELL" and not qty else None)
                )
                new_qty = qty
                order = pending.get("order") or {}
                price = candle.open
                if order.get("limit_price") is not None:
                    limit = order["limit_price"]
                    if order.get("maker"):
                        row = strategy.row(candle)
                        if not row["maker_buy_filled" if side == "BUY" else "maker_sell_filled"]:
                            reason = "PASSIVE_ORDER_NOT_FILLED"
                        price = limit
                    elif (side == "BUY" and candle.low > limit) or (
                        side == "SELL" and candle.high < limit
                    ):
                        reason = "LIMIT_NOT_TOUCHED"
                    else:
                        price = (
                            min(candle.open, limit) if side == "BUY" else max(candle.open, limit)
                        )
                if strategy_id == "SCALPING@1" or (
                    strategy_id == "MARKET_MAKING@1" and not order.get("maker")
                ):
                    row = strategy.row(candle)
                    price = Decimal(row["ask" if side == "BUY" else "bid"])
                if not reason and side == "BUY":
                    # Exact rational floor prevents a division rounding across the step boundary.
                    budget = (
                        min(Fraction(cash), Fraction(initial_cash) / order["installments"])
                        if order.get("installments")
                        else Fraction(cash)
                    )
                    units = budget / (
                        Fraction(price) * (1 + Fraction(fee_rate)) * Fraction(quantity_step)
                    )
                    new_qty = (units.numerator // units.denominator) * quantity_step
                    if new_qty == 0:
                        reason = "ZERO_QUANTITY"
                if not reason and strategy_id in ("SCALPING@1", "MARKET_MAKING@1"):
                    capacity = Decimal(strategy.row(candle)["capacity_base"])
                    if side == "BUY":
                        new_qty = min(new_qty, (capacity // quantity_step) * quantity_step)
                        if new_qty == 0:
                            reason = "ZERO_QUANTITY"
                    elif new_qty > capacity:
                        reason = "INSUFFICIENT_LIQUIDITY"
                if reason:
                    event(
                        "skipped",
                        side,
                        pending["open_time_ms"],
                        candle.open_time_ms,
                        signal_id=signal_id,
                        reason=reason,
                    )
                else:
                    gross = new_qty * price
                    fee = gross * fee_rate
                    delta = -(gross + fee) if side == "BUY" else gross - fee
                    before_qty = qty
                    cash += delta
                    fees += fee
                    if side == "BUY":
                        qty, cost = qty + new_qty, cost + gross + fee
                        buys += 1
                    else:
                        pnl = gross - fee - cost
                        realized += pnl
                        wins += pnl > 0
                        qty = cost = Decimal(0)
                        sells += 1
                    if cash < 0 or qty < 0:
                        raise invalid("financial", "Saldo inválido na simulação.")
                    if cash > Decimal("1e48") or qty > Decimal("1e60"):
                        raise invalid("financial", "Limite de magnitude financeira excedido.")
                    fill = event(
                        "fill",
                        side,
                        candle.open_time_ms,
                        candle.close_time_ms
                        if order.get("limit_price") is not None
                        and not order.get("maker")
                        and price != candle.open
                        else candle.open_time_ms,
                        signal_id=signal_id,
                        price=wire(price),
                        quantity=wire(new_qty),
                        fee=wire(fee),
                        gross=wire(gross),
                    )
                    ledger.append(
                        {
                            "seq": len(ledger),
                            "kind": "FILL",
                            "fill_id": fill["id"],
                            "signal_id": signal_id,
                            "cash_delta": wire(delta),
                            "asset_delta": wire(qty - before_qty),
                            "fee": wire(fee),
                            "cash_after": wire(cash),
                            "position_after": wire(qty),
                            "cost_basis_after": wire(cost),
                        }
                    )
                    if hasattr(strategy, "on_fill"):
                        strategy.on_fill(side, price)
                pending = None
            evaluation = strategy.on_close(candle)
            observed_ready |= evaluation.ready
            indicators.append(
                {
                    "open_time_ms": candle.open_time_ms,
                    "values": {k: wire(v) for k, v in evaluation.values.items()},
                }
            )
            evaluations.append(
                {
                    "open_time_ms": candle.open_time_ms,
                    "occurred_at_ms": candle.close_time_ms,
                    "ready": evaluation.ready,
                    "reason": evaluation.reason,
                    "side": evaluation.side,
                }
            )
            if evaluation.side:
                pending = event(
                    "signal",
                    evaluation.side,
                    candle.open_time_ms,
                    candle.close_time_ms,
                    reason=evaluation.reason,
                    evaluation_index=count - 1,
                )
                evaluations[-1]["signal_id"] = pending["id"]
                pending = {**pending, "order": evaluation.order}
            value = cash + qty * candle.close
            if value > Decimal("1e48"):
                raise invalid("financial", "Patrimônio excede o limite de 10^48 USDT.")
            equity.append({"open_time_ms": candle.open_time_ms, "equity": wire(value)})
            peak = max(peak, value)
            max_drawdown = max(max_drawdown, Fraction(peak - value) / Fraction(peak))
        if last is None:
            raise invalid("dataset", "Dataset vazio.")
        if pending:
            event(
                "skipped",
                pending["side"],
                pending["open_time_ms"],
                last.close_time_ms,
                signal_id=pending["id"],
                reason="NO_FUTURE_OPEN",
            )
        position_value = qty * last.close
        final_equity = cash + position_value
        unrealized = position_value - cost
        if final_equity - initial_cash != realized + unrealized:
            raise invalid("financial", "Falha de reconciliação patrimonial.")
        context.traps[Inexact] = False
        summary = {
            "initial_cash": wire(initial_cash),
            "final_cash": wire(cash),
            "final_position_qty": wire(qty),
            "final_position_value": wire(position_value),
            "initial_equity": wire(initial_cash),
            "final_equity": wire(final_equity),
            "total_fees": wire(fees),
            "realized_pnl": wire(realized),
            "unrealized_pnl": wire(unrealized),
            "return_pct": wire((final_equity - initial_cash) / initial_cash * 100),
            "buy_fills": buys,
            "sell_fills": sells,
            "closed_trades": sells,
            "win_rate_pct": wire(Decimal(wins) / sells * 100) if sells else None,
            "max_drawdown_pct": wire(
                Decimal(max_drawdown.numerator) / Decimal(max_drawdown.denominator) * 100
            ),
        }
    return {
        "summary": summary,
        "indicators": indicators,
        "equity": equity,
        "events": sorted(events, key=lambda e: (e["open_time_ms"], e["seq"])),
        "ledger": ledger,
        "evaluations": evaluations,
        "diagnostics": [] if observed_ready else ["INSUFFICIENT_WARMUP"],
        "numeric_policy": NUMERIC_POLICY,
    }
