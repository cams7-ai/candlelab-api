"""Executable rules from the tutorial, with explicit simulation assumptions."""

from collections import deque
from decimal import Decimal as D, localcontext

from candlelab.domain.errors import invalid
from candlelab.domain.indicators import Bands, MACD, Volatility
from candlelab.domain.numbers import INDICATOR_CONTEXT
from candlelab.domain.indicators import MovingAverage, RSI
from candlelab.domain.strategy_types import Evaluation, StrategySpec, Window
from candlelab.domain.tutorial_data import (
    REQUIREMENTS,
    number,
    parameter_model,
    rows_from_json,
)

# Numerical choices absent from the conceptual tutorial are published as editable defaults.
RISK = {"stop_pct": number("3", "Stop no fechamento (%)", "0.01", "100")}
TREND = {"short_window": (Window, 8), "long_window": (Window, 21)}
MOMENTUM = {"roc_period": (Window, 5), "ema_period": (Window, 10), "volume_period": (Window, 20)}
BANDS = {"period": (Window, 20), "deviations": number("2", "Desvios Bollinger", "0.1", "10")}
BREAKOUT = {
    "lookback": (Window, 20),
    "atr_period": (Window, 14),
    "atr_multiple": number("2", "Trailing ATR", "0.1", "20"),
    "min_atr_pct": number("0.1", "ATR mínimo (%)", "0", "100"),
}
GRID = {"lookback": (Window, 20), "levels": (Window, 5)}

DEFINITIONS = [
    (
        "TREND_FOLLOWING",
        "Trend Following",
        TREND,
        "EMA curta cruza acima da longa e preço acima de ambas; saída ao perder EMA longa.",
        "Congestão provoca cruzamentos falsos.",
    ),
    (
        "MOMENTUM",
        "Momentum",
        MOMENTUM,
        "ROC positivo crescente, EMA ascendente e volume acima da média anterior; saída na desaceleração.",
        "Entradas tardias podem sofrer reversão.",
    ),
    (
        "MEAN_REVERSION",
        "Mean Reversion",
        {**BANDS, "adx_period": (Window, 14), "adx_limit": number("25", "ADX máximo", "0", "100")},
        "Retorno à banda inferior com ADX baixo; saída na média.",
        "Tendência de baixa pode persistir.",
    ),
    (
        "GRID_TRADING",
        "Grid Trading",
        GRID,
        "Grade congelada na primeira janela; uma ordem por candle e um lote de inventário por ciclo.",
        "Ordens limitadas simuladas pelo OHLC; sem fila ou fills parciais.",
    ),
    (
        "BREAKOUT",
        "Breakout",
        BREAKOUT,
        "Fechamento acima da máxima anterior com filtro ATR; saída no nível rompido ou trailing ATR.",
        "Gaps e rompimentos falsos.",
    ),
    (
        "SWING_TRADING",
        "Swing Trading",
        {
            "lookback": (Window, 20),
            "min_reward_risk": number("2", "Retorno/risco mínimo", "0.1", "20"),
        },
        "Reação a suporte anterior com SMA20 e retorno/risco mínimo; saída na resistência.",
        "Suportes e resistências são extremos de janela anterior.",
    ),
    (
        "SCALPING",
        "Scalping",
        {
            "max_spread_bps": number("10", "Spread máximo (bps)", "0", "1000"),
            "target_pct": number("0.5", "Alvo (%)", "0.01", "100"),
            "max_hold": (Window, 3),
        },
        "Recuperação da VWAP diária UTC com spread estreito e fluxo positivo; alvo/stop/tempo.",
        "Candles não reproduzem latência; execução usa quotes auxiliares e capacidade.",
    ),
    (
        "ARBITRAGE",
        "Arbitragem",
        {
            "min_edge_bps": number("1", "Margem líquida mínima (bps)", "0", "1000"),
            "transfer_rate": number("0", "Custo proporcional adicional", "0", "0.5"),
        },
        "Compra no ask de A e venda simultânea no bid de B, ou direção inversa, se margem líquida positiva.",
        "Inventário pré-financiado nos mercados; duas pernas atômicas simuladas, sem transferências reais.",
    ),
    (
        "DCA",
        "DCA — compras periódicas",
        {"every_candles": (Window, 24), "start_index": (Window, 1)},
        "Três compras de orçamento igual (caixa inicial/3 incluindo taxa); não força venda.",
        "Acumulação mantém risco de queda; passo pode deixar saldo residual.",
    ),
    (
        "MARKET_MAKING",
        "Market Making",
        {},
        "Cotação passiva no bid/ask observado; próxima observação confirma fill explícito da ordem anterior.",
        "Flags devem corresponder à cotação anterior; fila e seleção adversa dependem da fonte importada.",
    ),
    (
        "PAIRS_TRADING",
        "Pairs Trading",
        {
            "period": (Window, 20),
            "entry_z": number("2", "Z-score de entrada", "0.1", "10"),
            "stop_z": number("4", "Z-score de stop", "0.2", "20"),
            "borrow_rate_per_candle": number("0.00001", "Empréstimo ETH por candle", "0", "0.1"),
        },
        "Z-score causal BTC/ETH: compra BTC e vende ETH a descoberto com nocionais iguais; encerra as duas pernas na normalização/stop.",
        "Simulação com colateral e empréstimo de ETH; não é execução Spot de uma carteira long/short real.",
    ),
    (
        "MACHINE_LEARNING",
        "Machine Learning",
        {
            "buy_probability": number("0.6", "Probabilidade de entrada", "0", "1"),
            "sell_probability": number("0.5", "Probabilidade de saída", "0", "1"),
        },
        "Compra acima do limiar externo; vende abaixo do limiar de saída ou stop.",
        "Modelo e probabilidades são importados; declaração temporal não comprova calibração ou validação.",
    ),
    (
        "EMA_ADX_ATR",
        "Trend Following + ADX + ATR",
        {
            **TREND,
            "atr_period": (Window, 14),
            "atr_multiple": number("2", "Trailing ATR", "0.1", "20"),
            "adx_limit": number("25", "ADX mínimo", "0", "100"),
        },
        "EMA curta acima da longa, ADX forte e confirmação de alta; saída por trailing ATR.",
        "ADX tem atraso; parâmetros precisam de validação fora da amostra.",
    ),
    (
        "RSI_BOLLINGER",
        "RSI + Bollinger Bands",
        {
            **BANDS,
            "rsi_period": (Window, 14),
            "recovery": number("30", "Recuperação RSI", "1", "99"),
        },
        "RSI cruza recuperação e preço retorna à banda inferior no mesmo candle; saída na média.",
        "Sobrevenda não garante reversão.",
    ),
    (
        "BREAKOUT_VOLUME_ATR",
        "Breakout + Volume + ATR",
        {
            **BREAKOUT,
            "volume_period": (Window, 20),
            "volume_multiple": number("1", "Volume relativo mínimo", "0.1", "100"),
        },
        "Rompimento da máxima anterior com volume maior que média anterior; saída no nível/trailing ATR.",
        "Volume também aumenta em rompimentos falsos.",
    ),
    (
        "MACD_EMA_RSI",
        "MACD + EMA + RSI",
        {
            "ema_period": (Window, 21),
            "rsi_period": (Window, 14),
            "rsi_min": number("40", "RSI mínimo", "0", "100"),
            "rsi_max": number("70", "RSI máximo", "0", "100"),
        },
        "MACD12/26 cruza sinal9, preço acima EMA21 e RSI na faixa; saída na perda de força MACD.",
        "Indicadores derivados do preço não são confirmações independentes.",
    ),
    (
        "GRID_ADX_ATR",
        "Grid + ADX + ATR",
        {
            **GRID,
            "atr_period": (Window, 14),
            "adx_limit": number("20", "ADX máximo", "0", "100"),
            "atr_multiple": number("1", "Espaçamento ATR", "0.1", "20"),
        },
        "Grade em regime ADX baixo, espaçamento ATR congelado; desliga em rompimento ou ADX forte.",
        "Uma ordem por candle, um lote por ciclo; ADX atrasado pode falhar.",
    ),
    (
        "MOMENTUM_RANKING",
        "Momentum + Ranking de moedas",
        {
            **MOMENTUM,
            "top_n": (Window, 3),
            "min_liquidity": number("1000000", "Liquidez mínima USDT", "0", "1000000000000"),
            "min_roc": number("0", "ROC mínimo (%)", "-100", "1000000"),
        },
        "Momentum confirmado e BTC elegível no ranking de ROC de universo líquido; sai quando perde seleção.",
        "Universo e retornos comparáveis são importados; viés de sobrevivência deve ser controlado.",
    ),
]


class TutorialStrategy:
    def __init__(self, p, kind):
        self.p, self.kind = p, kind
        self.history = deque(
            maxlen=max(
                getattr(p, "lookback", 20),
                getattr(p, "roc_period", 5) + 1,
                getattr(p, "volume_period", 20),
            )
        )
        self.fast = MovingAverage(getattr(p, "short_window", 8), True)
        self.slow = MovingAverage(getattr(p, "long_window", 21), True)
        self.ema = MovingAverage(getattr(p, "ema_period", 10), True)
        self.sma = MovingAverage(20)
        self.bands = Bands(getattr(p, "period", 20), getattr(p, "deviations", "2"))
        self.vol = Volatility(getattr(p, "atr_period", getattr(p, "adx_period", 14)))
        self.rsi = RSI(getattr(p, "rsi_period", 14))
        self.macd = MACD()
        self.rows = rows_from_json(p.auxiliary_json, kind) if kind in REQUIREMENTS else {}
        self.ratios = deque(maxlen=getattr(p, "period", 20))
        self.previous = {}
        self.entry = self.trail = self.level = self.spacing = self.center = None
        self.position = False
        self.count, self.buy_count, self.held = 0, 0, 0
        self.vwap_total = self.vwap_volume = D(0)
        self.day = None
        self.disabled = False

    def on_fill(self, side, price):
        if side == "BUY":
            self.position, self.entry, self.held = True, price, 0
            self.buy_count += 1
            self.trail = None
        else:
            self.position, self.entry, self.trail = False, None, None

    def row(self, candle):
        if self.kind not in REQUIREMENTS:
            return {}
        if candle.open_time_ms not in self.rows:
            raise invalid("auxiliary_json", f"Observação ausente: {candle.open_time_ms}.")
        return self.rows[candle.open_time_ms]

    def on_close(self, c):
        with localcontext(INDICATOR_CONTEXT):
            return self._close(c)

    def _close(self, c):
        p, kind, old = self.p, self.kind, self.previous
        self.count += 1
        self.held += int(self.position)
        row = self.row(c)
        fast, slow = self.fast.update(c.close), self.slow.update(c.close)
        ema, sma = self.ema.update(c.close), self.sma.update(c.close)
        mean, lower, upper = self.bands.update(c.close)
        atr, adx = self.vol.update(c)
        rsi = self.rsi.update(c.close)
        macd, signal = self.macd.update(c.close)
        previous_c = self.history[-1] if self.history else None
        roc_n = getattr(p, "roc_period", 5)
        roc = (
            (c.close / self.history[-roc_n].close - 1) * 100 if len(self.history) >= roc_n else None
        )
        lookback = getattr(p, "lookback", 20)
        prior = list(self.history)[-lookback:]
        resistance = max(x.high for x in prior) if len(prior) == lookback else None
        support = min(x.low for x in prior) if len(prior) == lookback else None
        volumes = list(self.history)[-getattr(p, "volume_period", 20) :]
        volume_mean = (
            sum(x.base_volume for x in volumes) / len(volumes)
            if len(volumes) == getattr(p, "volume_period", 20)
            else None
        )
        day = c.open_time_ms // 86400000
        if day != self.day:
            self.day, self.vwap_total, self.vwap_volume = day, D(0), D(0)
        self.vwap_total += (c.high + c.low + c.close) / 3 * c.base_volume
        self.vwap_volume += c.base_volume
        vwap = self.vwap_total / self.vwap_volume if self.vwap_volume else None
        z = None
        if kind == "PAIRS_TRADING":
            ratio = c.close / D(row["eth_close"])
            self.ratios.append(ratio)
            if len(self.ratios) == p.period:
                avg = sum(self.ratios) / p.period
                std = (sum((x - avg) ** 2 for x in self.ratios) / p.period).sqrt()
                z = (ratio - avg) / std if std else D(0)
        values, ready, buy, sell, order = {}, False, False, False, None
        reason = "RULE_NOT_MET"
        cross = (
            fast is not None
            and slow is not None
            and old.get("fast") is not None
            and old.get("slow") is not None
            and old["fast"] <= old["slow"]
            and fast > slow
        )
        if kind in ("TREND_FOLLOWING", "EMA_ADX_ATR"):
            values = {"ema_short": fast, "ema_long": slow}
            ready = old.get("slow") is not None
            buy = cross and c.close > max(fast, slow) if ready else False
            sell = slow is not None and c.close < slow
            if kind == "EMA_ADX_ATR":
                values.update(adx=adx, atr=atr)
                ready = ready and adx is not None and atr is not None
                buy = (
                    ready
                    and fast > slow
                    and c.close > fast
                    and c.close > c.open
                    and adx > D(p.adx_limit)
                )
        elif kind in ("MOMENTUM", "MOMENTUM_RANKING"):
            values = {"roc": roc, "ema": ema, "volume_mean": volume_mean}
            ready = all(
                x is not None for x in (roc, ema, old.get("roc"), old.get("ema"), volume_mean)
            )
            buy = (
                ready
                and roc > 0
                and roc > old["roc"]
                and ema > old["ema"]
                and c.base_volume > volume_mean
            )
            sell = ready and (roc < old["roc"] or c.close < ema)
            if kind == "MOMENTUM_RANKING":
                universe = row["universe"]
                ranked = sorted(
                    (
                        symbol
                        for symbol, item in universe.items()
                        if D(item["liquidity"]) >= D(p.min_liquidity)
                    ),
                    key=lambda symbol: (-D(universe[symbol]["roc"]), symbol),
                )
                rank = ranked.index("BTCUSDT") + 1 if "BTCUSDT" in ranked else len(universe) + 1
                eligible = rank <= p.top_n and D(universe["BTCUSDT"]["roc"]) > D(p.min_roc)
                values["rank"] = D(rank)
                buy = buy and eligible
                sell = sell or not eligible
        elif kind in ("MEAN_REVERSION", "RSI_BOLLINGER"):
            values = {"sma": mean, "bollinger_lower": lower, "bollinger_upper": upper}
            ready = lower is not None and old.get("lower") is not None
            recovery = ready and previous_c.close < old["lower"] and c.close >= lower
            buy = recovery
            sell = mean is not None and c.close >= mean
            if kind == "MEAN_REVERSION":
                values["adx"] = adx
                ready = ready and adx is not None
                buy = buy and ready and adx < D(p.adx_limit)
            else:
                values["rsi"] = rsi
                ready = ready and rsi is not None and old.get("rsi") is not None
                buy = buy and ready and old["rsi"] <= D(p.recovery) < rsi
        elif kind in ("BREAKOUT", "BREAKOUT_VOLUME_ATR"):
            values = {"resistance": resistance, "atr": atr}
            ready = resistance is not None and atr is not None
            buy = ready and c.close > resistance and atr / c.close * 100 >= D(p.min_atr_pct)
            sell = self.level is not None and c.close < self.level
            if buy and not self.position:
                self.level = resistance
            if kind == "BREAKOUT_VOLUME_ATR":
                values["volume_mean"] = volume_mean
                ready = ready and volume_mean is not None
                buy = buy and ready and c.base_volume > volume_mean * D(p.volume_multiple)
        elif kind == "SWING_TRADING":
            values = {"support": support, "resistance": resistance, "sma": sma}
            ready = support is not None and sma is not None and previous_c is not None
            risk = c.close - support if ready else D(0)
            buy = (
                ready
                and risk > 0
                and c.low <= support * D("1.01")
                and c.close > previous_c.close
                and c.close > sma
                and (resistance - c.close) / risk >= D(p.min_reward_risk)
            )
            if buy and not self.position:
                self.level = resistance
            sell = (
                self.position and (c.close >= self.level or c.close < support) if ready else False
            )
        elif kind == "SCALPING":
            spread = (D(row["ask"]) / D(row["bid"]) - 1) * 10000
            values = {"vwap": vwap, "spread_bps": spread, "flow": D(row["flow"])}
            ready = vwap is not None and old.get("vwap") is not None and previous_c is not None
            buy = (
                ready
                and previous_c.close <= old["vwap"]
                and c.close > vwap
                and spread <= D(p.max_spread_bps)
                and D(row["flow"]) > 0
            )
            sell = self.position and (
                c.close >= self.entry * (1 + D(p.target_pct) / 100) or self.held >= p.max_hold
            )
        elif kind == "DCA":
            ready = True
            buy = (
                self.buy_count < 3
                and self.count >= p.start_index
                and (self.count - p.start_index) % p.every_candles == 0
            )
            order = {}
            # The engine computes initial_cash / 3 as an exact rational before flooring quantity.
            order["installments"] = 3
        elif kind in ("GRID_TRADING", "GRID_ADX_ATR"):
            ready = support is not None and resistance is not None and resistance > support
            if kind == "GRID_ADX_ATR":
                ready = ready and adx is not None and atr is not None
            if ready and self.center is None:
                self.center = (resistance + support) / 2
                self.spacing = (
                    (resistance - support) / p.levels
                    if kind == "GRID_TRADING"
                    else atr * D(p.atr_multiple)
                )
                self.grid_low, self.grid_high = support, resistance
            if self.center is not None:
                self.disabled |= c.close < self.grid_low or c.close > self.grid_high
                if kind == "GRID_ADX_ATR" and adx is not None:
                    self.disabled |= adx >= D(p.adx_limit)
                values = {
                    "grid_center": self.center,
                    "grid_lower": self.grid_low,
                    "grid_upper": self.grid_high,
                }
                if kind == "GRID_ADX_ATR":
                    values.update(adx=adx, atr=atr)
                if self.disabled:
                    sell, reason = self.position, "GRID_DISABLED"
                elif self.position:
                    sell, order = True, {"limit_price": self.entry + self.spacing}
                else:
                    limit = self.center - self.spacing
                    buy = limit >= self.grid_low and limit > 0
                    order = {"limit_price": limit, "installments": p.levels}
        elif kind == "MACD_EMA_RSI":
            values = {"macd": macd, "macd_signal": signal, "ema": ema, "rsi": rsi}
            ready = all(
                x is not None for x in (macd, signal, old.get("macd"), old.get("signal"), ema, rsi)
            )
            buy = (
                ready
                and old["macd"] <= old["signal"]
                and macd > signal
                and c.close > ema
                and D(p.rsi_min) <= rsi <= D(p.rsi_max)
            )
            sell = ready and (macd < signal or macd < old["macd"])
        elif kind == "ARBITRAGE":
            edge_a = D(row["bid_b"]) / D(row["ask"]) - 1
            edge_b = D(row["bid"]) / D(row["ask_b"]) - 1
            values = {"edge_bps": max(edge_a, edge_b) * 10000}
            ready, buy = True, max(edge_a, edge_b) > D(p.min_edge_bps) / 10000 + D(p.transfer_rate)
            order = {"arbitrage_direction": "A_B" if edge_a >= edge_b else "B_A"}
        elif kind == "MARKET_MAKING":
            values = {"bid": D(row["bid"]), "ask": D(row["ask"])}
            ready, buy, sell = True, not self.position, self.position
            order = {"limit_price": D(row["ask"] if self.position else row["bid"]), "maker": True}
        elif kind == "PAIRS_TRADING":
            values = {"zscore": z, "eth_price": D(row["eth_close"])}
            ready = z is not None
            buy = ready and z < -D(p.entry_z)
            sell = ready and (z >= 0 or z < -D(p.stop_z))
        elif kind == "MACHINE_LEARNING":
            probability = D(row["probability"])
            values = {"probability": probability}
            ready, buy, sell = (
                True,
                probability > D(p.buy_probability),
                probability < D(p.sell_probability),
            )
        if self.position and kind not in ("DCA", "ARBITRAGE"):
            # Stop thresholds use only data available before this close.
            if c.close <= self.entry * (1 - D(p.stop_pct) / 100):
                sell, order, reason = True, None, "STOP_CLOSE"
            if kind in ("BREAKOUT", "BREAKOUT_VOLUME_ATR", "EMA_ADX_ATR") and atr is not None:
                if self.trail is not None and c.close <= self.trail:
                    sell, reason = True, "ATR_STOP_CLOSE"
                candidate = c.close - D(p.atr_multiple) * atr
                self.trail = max(self.trail, candidate) if self.trail is not None else candidate
                values["atr_stop"] = self.trail
        side = (
            "SELL"
            if self.position and sell
            else "BUY"
            if buy and (not self.position or kind == "DCA")
            else None
        )
        if not ready:
            side, reason = None, "WARMUP"
        elif side and reason == "RULE_NOT_MET":
            reason = kind + "_" + side
        self.previous = dict(
            fast=fast,
            slow=slow,
            ema=ema,
            roc=roc,
            lower=lower,
            rsi=rsi,
            macd=macd,
            signal=signal,
            vwap=vwap,
        )
        self.history.append(c)
        return Evaluation(values, ready, side, reason, order)


def register(registry):
    for index, (kind, name, fields, rules, limitations) in enumerate(DEFINITIONS):
        params = parameter_model(kind, {**({} if kind in ("DCA", "ARBITRAGE") else RISK), **fields})
        registry[kind + "@1"] = StrategySpec(
            name,
            params,
            lambda p, k=kind: TutorialStrategy(p, k),
            category="individual" if index < 12 else "combination",
            rules=rules,
            limitations=limitations,
            required_data=REQUIREMENTS.get(kind, []),
        )
