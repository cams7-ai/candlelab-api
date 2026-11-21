"""Reusable rules evaluated in order against a shared, causal strategy context.

Published recipes retain their historical warmup, state updates and risk ordering.
Filters grant entry permission; they are not independent buy/sell votes.
"""

from dataclasses import dataclass, field
from decimal import Decimal as D


@dataclass
class RuleContext:
    data: dict
    values: dict = field(default_factory=dict)
    ready: bool = False
    buy: bool = False
    sell: bool = False
    order: dict | None = None
    reason: str = "RULE_NOT_MET"

    def __getattr__(self, key):
        return self.data[key]


def allows_rsi(value, minimum):
    return value is not None and value >= D(minimum)


def rsi_confirmation(f):
    f.values["rsi"] = f.rsi
    f.ready = f.ready and f.rsi is not None
    f.buy = f.buy and f.ready and allows_rsi(f.rsi, f.p.rsi_confirmation_min)


def ema_direction(f):
    f.values = {"ema_short": f.fast, "ema_long": f.slow}
    f.ready = f.old.get("slow") is not None
    f.buy = f.ready and f.fast > f.slow and f.c.close > f.fast and f.c.close > f.c.open
    f.sell = f.slow is not None and f.c.close < f.slow


def adx_confirmation(f):
    f.values.update(adx=f.adx, atr=f.atr)
    f.ready = f.ready and f.adx is not None and f.atr is not None
    f.buy = f.buy and f.ready and f.adx > D(f.p.adx_limit)


def bollinger_recovery(f):
    f.values = {"sma": f.mean, "bollinger_lower": f.lower, "bollinger_upper": f.upper}
    f.ready = f.lower is not None and f.old.get("lower") is not None
    f.buy = f.ready and f.previous_c.close < f.old["lower"] and f.c.close >= f.lower
    f.sell = f.mean is not None and f.c.close >= f.mean


def rsi_recovery(f):
    f.values["rsi"] = f.rsi
    f.ready = f.ready and f.rsi is not None and f.old.get("rsi") is not None
    f.buy = f.buy and f.ready and f.old["rsi"] <= D(f.p.recovery) < f.rsi


def breakout(f):
    f.values = {"resistance": f.resistance, "atr": f.atr}
    f.ready = f.resistance is not None and f.atr is not None
    f.buy = f.ready and f.c.close > f.resistance and f.atr / f.c.close * 100 >= D(f.p.min_atr_pct)
    f.sell = f.state.level is not None and f.c.close < f.state.level
    # This update intentionally precedes volume confirmation, as in the original engine.
    if f.buy and not f.state.position:
        f.state.level = f.resistance


def volume_confirmation(f):
    f.values["volume_mean"] = f.volume_mean
    f.ready = f.ready and f.volume_mean is not None
    f.buy = f.buy and f.ready and f.c.base_volume > f.volume_mean * D(f.p.volume_multiple)


def macd_signal(f):
    f.values = {"macd": f.macd, "macd_signal": f.signal}
    f.ready = all(x is not None for x in (f.macd, f.signal, f.old.get("macd"), f.old.get("signal")))
    f.buy = f.ready and f.old["macd"] <= f.old["signal"] and f.macd > f.signal
    f.sell = f.ready and (f.macd < f.signal or f.macd < f.old["macd"])


def ema_confirmation(f):
    f.values["ema"] = f.ema
    f.ready = f.ready and f.ema is not None
    f.buy = f.buy and f.ready and f.c.close > f.ema


def rsi_range(f):
    f.values["rsi"] = f.rsi
    f.ready = f.ready and f.rsi is not None
    f.buy = f.buy and f.ready and D(f.p.rsi_min) <= f.rsi <= D(f.p.rsi_max)
    # Historically MACD exits wait for the complete recipe to warm up.
    f.sell = f.sell and f.ready


def momentum(f):
    f.values = {"roc": f.roc, "ema": f.ema, "volume_mean": f.volume_mean}
    f.ready = all(
        x is not None for x in (f.roc, f.ema, f.old.get("roc"), f.old.get("ema"), f.volume_mean)
    )
    f.buy = (
        f.ready
        and f.roc > 0
        and f.roc > f.old["roc"]
        and f.ema > f.old["ema"]
        and f.c.base_volume > f.volume_mean
    )
    f.sell = f.ready and (f.roc < f.old["roc"] or f.c.close < f.ema)


def ranking_confirmation(f):
    universe = f.row["universe"]
    ranked = sorted(
        (
            symbol
            for symbol, item in universe.items()
            if D(item["liquidity"]) >= D(f.p.min_liquidity)
        ),
        key=lambda symbol: (-D(universe[symbol]["roc"]), symbol),
    )
    rank = ranked.index("BTCUSDT") + 1 if "BTCUSDT" in ranked else len(universe) + 1
    eligible = rank <= f.p.top_n and D(universe["BTCUSDT"]["roc"]) > D(f.p.min_roc)
    f.values["rank"] = D(rank)
    f.buy = f.buy and eligible
    f.sell = f.sell or not eligible


def grid(f):
    s = f.state
    f.ready = f.support is not None and f.resistance is not None and f.resistance > f.support
    enhanced = f.kind == "GRID_ADX_ATR"
    if enhanced:
        f.ready = f.ready and f.adx is not None and f.atr is not None
    if f.ready and s.center is None:
        s.center = (f.resistance + f.support) / 2
        s.spacing = (
            f.atr * D(f.p.atr_multiple) if enhanced else (f.resistance - f.support) / f.p.levels
        )
        s.grid_low, s.grid_high = f.support, f.resistance
    if s.center is not None:
        s.disabled |= f.c.close < s.grid_low or f.c.close > s.grid_high


def grid_regime(f):
    if f.kind == "GRID_REGIME":
        f.ready = f.adx is not None and f.atr is not None
        f.buy = f.ready and f.adx < D(f.p.adx_limit)
        return
    if f.state.center is not None and f.adx is not None:
        f.state.disabled |= f.adx >= D(f.p.adx_limit)


def grid_orders(f):
    s = f.state
    if s.center is None:
        return
    f.values = {"grid_center": s.center, "grid_lower": s.grid_low, "grid_upper": s.grid_high}
    if f.kind == "GRID_ADX_ATR":
        f.values.update(adx=f.adx, atr=f.atr)
    if s.disabled:
        f.sell, f.reason = s.position, "GRID_DISABLED"
    elif s.position:
        f.sell, f.order = True, {"limit_price": s.entry + s.spacing}
    else:
        limit = s.center - s.spacing
        f.buy = limit >= s.grid_low and limit > 0
        f.order = {"limit_price": limit, "installments": f.p.levels}


def atr_exit(f):
    s = f.state
    if s.position and f.atr is not None:
        if s.trail is not None and f.c.close <= s.trail:
            f.sell, f.reason = True, "ATR_STOP_CLOSE"
        candidate = f.c.close - D(f.p.atr_multiple) * f.atr
        s.trail = max(s.trail, candidate) if s.trail is not None else candidate
        f.values["atr_stop"] = s.trail


# IDs describe actual rules, rather than raw indicator values.
RULES = {
    "RSI_CONFIRMATION": rsi_confirmation,
    "EMA_DIRECTION": ema_direction,
    "ADX_CONFIRMATION": adx_confirmation,
    "BOLLINGER_RECOVERY": bollinger_recovery,
    "RSI_RECOVERY": rsi_recovery,
    "BREAKOUT": breakout,
    "VOLUME_CONFIRMATION": volume_confirmation,
    "MACD_SIGNAL": macd_signal,
    "EMA_CONFIRMATION": ema_confirmation,
    "RSI_RANGE": rsi_range,
    "MOMENTUM": momentum,
    "RANKING_CONFIRMATION": ranking_confirmation,
    "GRID_TRADING": grid,
    "GRID_REGIME": grid_regime,
    "GRID_ORDERS": grid_orders,
    "ATR_EXIT": atr_exit,
}
RECIPES = {
    "EMA_ADX_ATR": ("EMA_DIRECTION", "ADX_CONFIRMATION", "ATR_EXIT"),
    "RSI_BOLLINGER": ("BOLLINGER_RECOVERY", "RSI_RECOVERY"),
    "BREAKOUT_VOLUME_ATR": ("BREAKOUT", "VOLUME_CONFIRMATION", "ATR_EXIT"),
    "MACD_EMA_RSI": ("MACD_SIGNAL", "EMA_CONFIRMATION", "RSI_RANGE"),
    "GRID_ADX_ATR": ("GRID_TRADING", "GRID_REGIME", "GRID_ORDERS"),
    "MOMENTUM_RANKING": ("MOMENTUM", "RANKING_CONFIRMATION"),
}


def evaluate_rules(kind, context):
    for component in RECIPES.get(kind, (kind,)):
        if component != "ATR_EXIT":
            RULES[component](context)
    if kind == "GRID_ADX_ATR":
        grid_orders(context)
