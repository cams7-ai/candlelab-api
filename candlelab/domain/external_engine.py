"""Atomic two-market arbitrage and collateralized BTC-long/ETH-short simulation."""

from decimal import Decimal as D, localcontext
from fractions import Fraction

from candlelab.domain.errors import invalid
from candlelab.domain.numbers import MONEY_CONTEXT, NUMERIC_POLICY, wire


def floor_qty(budget, price, step):
    units = Fraction(budget) / Fraction(price) / Fraction(step)
    return (units.numerator // units.denominator) * step


def simulate_external(candles, strategy, initial_cash, fee_rate, step, heartbeat):
    cash, btc, eth, cost, short_credit = initial_cash, D(0), D(0), D(0), D(0)
    fees = realized = trade_borrow = D(0)
    peak, drawdown = initial_cash, Fraction(0)
    buys = sells = trades = wins = 0
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
    last = None

    def event(kind, side, c, at=None, **extra):
        item = dict(
            id=f"event-{len(events) + 1}",
            seq=len(events) + 1,
            kind=kind,
            side=side,
            open_time_ms=c.open_time_ms,
            occurred_at_ms=at if at is not None else c.open_time_ms,
            **extra,
        )
        events.append(item)
        return item

    def fill(side, price, quantity, c, signal_id, asset="BTC", extra_fee=D(0)):
        nonlocal cash, fees, buys, sells
        gross = quantity * price
        fee = gross * fee_rate + extra_fee
        delta = -gross - fee if side == "BUY" else gross - fee
        cash += delta
        fees += fee
        buys += side == "BUY"
        sells += side == "SELL"
        item = event(
            "fill",
            side,
            c,
            signal_id=signal_id,
            price=wire(price),
            quantity=wire(quantity),
            fee=wire(fee),
            gross=wire(gross),
            asset=asset,
        )
        ledger.append(
            dict(
                seq=len(ledger),
                kind="FILL",
                fill_id=item["id"],
                signal_id=signal_id,
                asset=asset,
                cash_delta=wire(delta),
                asset_delta=wire(quantity if side == "BUY" else -quantity),
                cash_after=wire(cash),
                fee=wire(fee),
            )
        )
        return gross, fee

    with localcontext(MONEY_CONTEXT):
        for count, c in enumerate(candles, 1):
            last = c
            if heartbeat and count % 100 == 0:
                heartbeat()
            row = strategy.row(c)
            eth_price = D(row.get("eth_price", "1"))
            if pending:
                side, signal_id = pending["side"], pending["id"]
                skip = None
                if strategy.kind == "ARBITRAGE":
                    direction = pending["order"]["arbitrage_direction"]
                    ask = D(row["ask"] if direction == "A_B" else row["ask_b"])
                    bid = D(row["bid_b"] if direction == "A_B" else row["bid"])
                    transfer = D(strategy.p.transfer_rate)
                    net = bid * (1 - fee_rate) - ask * (1 + fee_rate + transfer)
                    if net / ask * 10000 <= D(strategy.p.min_edge_bps):
                        skip = "NET_EDGE_DISAPPEARED"
                    else:
                        budget = Fraction(cash) / (1 + Fraction(fee_rate) + Fraction(transfer))
                        quantity = min(
                            floor_qty(budget, ask, step),
                            floor_qty(D(row["capacity_base"]), D(1), step),
                        )
                        if not quantity:
                            skip = "ZERO_QUANTITY"
                        else:
                            before = cash
                            fill(
                                "BUY",
                                ask,
                                quantity,
                                c,
                                signal_id,
                                extra_fee=quantity * ask * transfer,
                            )
                            fill("SELL", bid, quantity, c, signal_id)
                            pnl = cash - before
                            realized += pnl
                            trades += 1
                            wins += pnl > 0
                elif side == "BUY" and not btc:
                    # Half equity funds BTC; the other half is collateral for ETH short.
                    budget = Fraction(cash) / (2 * (1 + Fraction(fee_rate)))
                    quantity = floor_qty(budget, c.open, step)
                    short_qty = floor_qty(quantity * c.open, eth_price, D("0.000001"))
                    if not quantity or not short_qty:
                        skip = "ZERO_QUANTITY"
                    else:
                        gross, fee = fill("BUY", c.open, quantity, c, signal_id)
                        cost = gross + fee
                        gross, fee = fill("SELL", eth_price, short_qty, c, signal_id, "ETH")
                        short_credit = gross - fee
                        btc, eth = quantity, short_qty
                        strategy.on_fill("BUY", c.open)
                elif side == "SELL" and btc:
                    gross, fee = fill("SELL", c.open, btc, c, signal_id)
                    pnl = gross - fee - cost
                    gross, fee = fill("BUY", eth_price, eth, c, signal_id, "ETH")
                    pnl += short_credit - gross - fee
                    realized += pnl
                    trades += 1
                    wins += pnl - trade_borrow > 0
                    trade_borrow = D(0)
                    btc = eth = cost = short_credit = D(0)
                    strategy.on_fill("SELL", c.open)
                else:
                    skip = "NO_POSITION" if side == "SELL" else "POSITION_ALREADY_OPEN"
                if skip:
                    event("skipped", side, c, signal_id=signal_id, reason=skip)
                pending = None
            if eth:
                borrow = eth * D(row["eth_close"]) * D(strategy.p.borrow_rate_per_candle)
                cash -= borrow
                fees += borrow
                trade_borrow += borrow
                realized -= borrow
                ledger.append(
                    dict(
                        seq=len(ledger),
                        kind="BORROW_FEE",
                        cash_delta=wire(-borrow),
                        asset_delta="0",
                        fee=wire(borrow),
                        cash_after=wire(cash),
                    )
                )
            evaluation = strategy.on_close(c)
            observed_ready |= evaluation.ready
            values = dict(evaluation.values)
            if strategy.kind == "PAIRS_TRADING":
                values.update(btc_position=btc, eth_short_position=eth)
            indicators.append(
                dict(open_time_ms=c.open_time_ms, values={k: wire(v) for k, v in values.items()})
            )
            side = evaluation.side
            # Collateral trigger is observed at close and also follows next-open execution.
            value = cash + btc * c.close - eth * D(row.get("eth_close", "1"))
            margin = btc and value < (cost + short_credit) / 4
            if margin:
                side = "SELL"
            reason = "MARGIN_CLOSE" if margin else evaluation.reason
            evaluations.append(
                dict(
                    open_time_ms=c.open_time_ms,
                    occurred_at_ms=c.close_time_ms,
                    ready=evaluation.ready,
                    side=side,
                    reason=reason,
                )
            )
            if side:
                signal = event("signal", side, c, at=c.close_time_ms, reason=reason)
                evaluations[-1]["signal_id"] = signal["id"]
                pending = {**signal, "order": evaluation.order}
            equity.append(dict(open_time_ms=c.open_time_ms, equity=wire(value)))
            peak = max(peak, value)
            drawdown = max(drawdown, Fraction(peak - value) / Fraction(peak))
            if cash < 0 or value < 0 or value > D("1e48"):
                raise invalid(
                    "financial",
                    "Limite de colateral/magnitude excedido na carteira de duas pernas.",
                )
        if last is None:
            raise invalid("dataset", "Dataset vazio.")
        if pending:
            event(
                "skipped",
                pending["side"],
                last,
                at=last.close_time_ms,
                signal_id=pending["id"],
                reason="NO_FUTURE_OPEN",
            )
        position_value = btc * last.close - eth * D(strategy.row(last).get("eth_close", "1"))
        final = cash + position_value
        unrealized = position_value - cost + short_credit
        if final - initial_cash != realized + unrealized:
            raise invalid("financial", "Falha de reconciliação da carteira de duas pernas.")
        summary = dict(
            initial_cash=wire(initial_cash),
            final_cash=wire(cash),
            final_position_qty=wire(btc),
            final_position_value=wire(position_value),
            initial_equity=wire(initial_cash),
            final_equity=wire(final),
            total_fees=wire(fees),
            realized_pnl=wire(realized),
            unrealized_pnl=wire(unrealized),
            return_pct=wire((final / initial_cash - 1) * 100),
            buy_fills=buys,
            sell_fills=sells,
            closed_trades=trades,
            win_rate_pct=wire(D(wins) / trades * 100) if trades else None,
            max_drawdown_pct=wire(D(drawdown.numerator) / D(drawdown.denominator) * 100),
        )
    return dict(
        summary=summary,
        indicators=indicators,
        equity=equity,
        events=events,
        ledger=ledger,
        evaluations=evaluations,
        diagnostics=[] if observed_ready else ["INSUFFICIENT_WARMUP"],
        numeric_policy=NUMERIC_POLICY,
    )
