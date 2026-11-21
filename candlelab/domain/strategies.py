"""Registro de estratégias; indicadores incrementais sem acesso à carteira."""

from decimal import Decimal, localcontext
from typing import Protocol

from pydantic import Field, model_validator, field_validator

from candlelab.domain.components import allows_rsi
from candlelab.domain.errors import invalid
from candlelab.domain.indicators import MovingAverage, RSI
from candlelab.domain.strategy_types import Parameters, Window, Evaluation, StrategySpec
from candlelab.domain.numbers import INDICATOR_CONTEXT, decimal_input, wire


class CrossParameters(Parameters):
    short_window: Window = 9
    long_window: Window = 21

    @model_validator(mode="after")
    def ordered(self):
        if self.short_window >= self.long_window:
            raise ValueError("Exija short_window < long_window.")
        return self


class RSIParameters(Parameters):
    period: Window = 14
    oversold: str = Field(
        default="30",
        pattern=r"^[0-9]+(?:\.[0-9]+)?$",
        max_length=40,
        description="Decimal: 0 < oversold < overbought < 100.",
        json_schema_extra={"x-decimal-exclusiveMinimum": "0", "x-decimal-exclusiveMaximum": "100"},
    )
    overbought: str = Field(
        default="70",
        pattern=r"^[0-9]+(?:\.[0-9]+)?$",
        max_length=40,
        description="Decimal: oversold < overbought < 100.",
        json_schema_extra={"x-decimal-exclusiveMinimum": "0", "x-decimal-exclusiveMaximum": "100"},
    )

    @field_validator("oversold", "overbought")
    @classmethod
    def normalized_threshold(cls, value, info):
        return wire(decimal_input(value, info.field_name))

    @model_validator(mode="after")
    def ordered(self):
        low, high = (
            decimal_input(self.oversold, "oversold"),
            decimal_input(self.overbought, "overbought"),
        )
        if not 0 < low < high < 100:
            raise ValueError("Exija 0 < oversold < overbought < 100.")
        return self


class FilterParameters(CrossParameters):
    rsi_period: Window = 14
    rsi_confirmation_min: str = Field(
        default="50",
        pattern=r"^[0-9]+(?:\.[0-9]+)?$",
        max_length=40,
        description="Decimal em [0,100].",
        json_schema_extra={"x-decimal-minimum": "0", "x-decimal-maximum": "100"},
    )

    @field_validator("rsi_confirmation_min")
    @classmethod
    def confirmation(cls, value):
        if not 0 <= decimal_input(value, "rsi_confirmation_min") <= 100:
            raise ValueError("Confirmação deve estar em [0,100].")
        return wire(decimal_input(value, "rsi_confirmation_min"))


class IncrementalStrategy(Protocol):
    def on_close(self, candle) -> Evaluation: ...


class CrossStrategy:
    def __init__(self, parameters, exponential=False, filtered=False):
        self.parameters = parameters
        self.short = MovingAverage(parameters.short_window, exponential)
        self.long = MovingAverage(parameters.long_window, exponential)
        self.rsi = RSI(parameters.rsi_period) if filtered else None
        self.prefix = "ema" if exponential else "sma"
        self.previous = None

    def on_close(self, candle):
        with localcontext(INDICATOR_CONTEXT):
            short, long = self.short.update(candle.close), self.long.update(candle.close)
            rsi = self.rsi.update(candle.close) if self.rsi else None
            values = {self.prefix + "_short": short, self.prefix + "_long": long}
            if self.rsi:
                values["rsi"] = rsi
            comparison = None if long is None else (short > long) - (short < long)
            previous, self.previous = self.previous, comparison
            side, reason = None, "NO_CROSS"
            ready = comparison is not None and previous is not None
            if not ready:
                reason = "WARMUP"
            elif previous <= 0 < comparison:
                side, reason = "BUY", "CROSS_UP"
                if self.rsi and not allows_rsi(rsi, self.parameters.rsi_confirmation_min):
                    side, reason = None, "RSI_FILTER_BLOCKED"
            elif previous >= 0 > comparison:
                side, reason = "SELL", "CROSS_DOWN"
            return Evaluation(values, ready, side, reason)


class RSIStrategy:
    def __init__(self, parameters):
        self.parameters = parameters
        self.rsi = RSI(parameters.period)
        self.previous = None

    def on_close(self, candle):
        with localcontext(INDICATOR_CONTEXT):
            current = self.rsi.update(candle.close)
            previous, self.previous = self.previous, current
            ready = previous is not None and current is not None
            side, reason = None, "NO_CROSS" if ready else "WARMUP"
            if ready and previous <= Decimal(self.parameters.oversold) < current:
                side, reason = "BUY", "EXIT_OVERSOLD"
            elif ready and previous >= Decimal(self.parameters.overbought) > current:
                side, reason = "SELL", "EXIT_OVERBOUGHT"
            return Evaluation({"rsi": current}, ready, side, reason)


REGISTRY = {
    "SMA_CROSS@1": StrategySpec("Cruzamento SMA", CrossParameters, CrossStrategy),
    "EMA_CROSS@1": StrategySpec(
        "Cruzamento EMA", CrossParameters, lambda p: CrossStrategy(p, exponential=True)
    ),
    "RSI_REVERSION@1": StrategySpec("Reversão RSI", RSIParameters, RSIStrategy),
    "SMA_RSI_FILTER@1": StrategySpec(
        "SMA com confirmação RSI",
        FilterParameters,
        lambda p: CrossStrategy(p, filtered=True),
        components=[
            {"strategy_id": "SMA_CROSS@1", "role": "signal"},
            {"strategy_id": "RSI_CONFIRMATION@1", "role": "filter"},
        ],
    ),
}


def resolve(strategy_id, parameters):
    if strategy_id not in REGISTRY:
        raise invalid("strategy_id", "Estratégia/versão desconhecida.")
    from pydantic import ValidationError

    spec = REGISTRY[strategy_id]
    try:
        parsed = spec.parameters.model_validate(parameters)
        if spec.required_data:
            from candlelab.domain.tutorial_data import rows_from_json

            rows_from_json(parsed.auxiliary_json, strategy_id.split("@")[0])
        if strategy_id == "MACHINE_LEARNING@1" and Decimal(parsed.sell_probability) >= Decimal(
            parsed.buy_probability
        ):
            raise ValueError("Exija sell_probability < buy_probability.")
        if strategy_id == "PAIRS_TRADING@1" and Decimal(parsed.stop_z) <= Decimal(parsed.entry_z):
            raise ValueError("Exija entry_z < stop_z.")
        return spec, parsed
    except (ValidationError, ValueError) as exc:
        raise invalid("parameters", str(exc)) from None


def catalog():
    from candlelab.domain.combinations import CUSTOM_ID, combinable

    return [
        {
            "id": key,
            "name": spec.name,
            "parameters_schema": spec.parameters.model_json_schema(),
            "defaults": spec.parameters().model_dump(),
            "category": spec.category,
            "rules": spec.rules,
            "limitations": spec.limitations,
            "required_data": spec.required_data,
            "combinable": combinable(key, spec),
            "composition_role": spec.composition_role,
            "components": spec.components,
        }
        for key, spec in REGISTRY.items()
        if key != CUSTOM_ID
    ]


from candlelab.domain.tutorial import register  # noqa: E402

register(REGISTRY)

from candlelab.domain.combinations import CUSTOM_ID, CUSTOM_SPEC  # noqa: E402

REGISTRY[CUSTOM_ID] = CUSTOM_SPEC
