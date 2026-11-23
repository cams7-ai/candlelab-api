"""Validated, immutable-in-run auxiliary snapshots; no implicit forward filling."""

import json
from decimal import Decimal
from typing import Annotated

from pydantic import Field, StrictBool, StrictInt, create_model, field_validator, model_validator

from candlelab.domain.numbers import decimal_input
from candlelab.domain.strategy_types import Parameters


def number(default, title, low="0", high="1000000"):
    return (
        str,
        Field(
            default=default,
            title=title,
            pattern=r"^-?\d+(?:\.\d+)?$",
            max_length=40,
            json_schema_extra={"x-min": low, "x-max": high},
        ),
    )


class TutorialParameters(Parameters):
    @model_validator(mode="after")
    def bounds(self):
        for name, field in type(self).model_fields.items():
            limits = field.json_schema_extra or {}
            if "x-min" in limits:
                value = decimal_input(getattr(self, name), name)
                if not Decimal(limits["x-min"]) <= value <= Decimal(limits["x-max"]):
                    raise ValueError(
                        f"{name}: fora dos limites {limits['x-min']}..{limits['x-max']}."
                    )
        if hasattr(self, "short_window") and self.short_window >= self.long_window:
            raise ValueError("Exija short_window < long_window.")
        if hasattr(self, "rsi_min") and Decimal(self.rsi_min) >= Decimal(self.rsi_max):
            raise ValueError("Exija rsi_min < rsi_max.")
        return self


class AssetObservation(Parameters):
    roc: str
    liquidity: str

    @field_validator("roc", "liquidity")
    @classmethod
    def numeric(cls, value, info):
        d = decimal_input(value, info.field_name)
        if info.field_name == "liquidity" and d < 0:
            raise ValueError("Liquidez negativa.")
        return value


class AuxiliaryRow(Parameters):
    open_time_ms: Annotated[StrictInt, Field(ge=0)]
    bid: str | None = None
    ask: str | None = None
    bid_b: str | None = None
    ask_b: str | None = None
    capacity_base: str | None = None
    maker_buy_filled: StrictBool | None = None
    maker_sell_filled: StrictBool | None = None
    flow: str | None = None
    eth_price: str | None = None
    eth_close: str | None = None
    probability: str | None = None
    trained_until_ms: Annotated[StrictInt, Field(ge=0)] | None = None
    model_id: str | None = Field(default=None, min_length=1, max_length=200)
    universe: dict[str, AssetObservation] | None = None

    @model_validator(mode="after")
    def valid(self):
        for name in (
            "bid",
            "ask",
            "bid_b",
            "ask_b",
            "capacity_base",
            "flow",
            "eth_price",
            "eth_close",
            "probability",
        ):
            value = getattr(self, name)
            if value is None:
                continue
            d = decimal_input(value, name)
            if name == "flow":
                if not -1 <= d <= 1:
                    raise ValueError("flow deve estar em [-1,1].")
            elif name == "probability":
                if not 0 <= d <= 1:
                    raise ValueError("probability deve estar em [0,1].")
            elif d <= 0:
                raise ValueError(f"{name} deve ser positivo.")
        for bid, ask in ((self.bid, self.ask), (self.bid_b, self.ask_b)):
            if bid is not None and ask is not None and Decimal(bid) > Decimal(ask):
                raise ValueError("Exija bid <= ask em cada mercado.")
        if self.trained_until_ms is not None and self.trained_until_ms >= self.open_time_ms:
            raise ValueError("O modelo deve ter sido treinado antes da observação.")
        return self


REQUIREMENTS = {
    "SCALPING": ["bid", "ask", "flow", "capacity_base"],
    "ARBITRAGE": ["bid", "ask", "bid_b", "ask_b", "capacity_base"],
    "MARKET_MAKING": ["bid", "ask", "capacity_base", "maker_buy_filled", "maker_sell_filled"],
    "PAIRS_TRADING": ["eth_price", "eth_close"],
    "MACHINE_LEARNING": ["probability", "trained_until_ms", "model_id"],
    "MOMENTUM_RANKING": ["universe"],
    "RANKING_CONFIRMATION": ["universe"],
}


def rows_from_json(value, kind):
    try:
        raw = json.loads(value)
        if not isinstance(raw, list) or not 1 <= len(raw) <= 100000:
            raise ValueError("Use uma lista JSON não vazia com até 100000 observações.")
        rows = [AuxiliaryRow.model_validate(row) for row in raw]
        times = [row.open_time_ms for row in rows]
        if any(a >= b for a, b in zip(times, times[1:])):
            raise ValueError("Timestamps devem ser únicos e crescentes.")
        for row in rows:
            for key in REQUIREMENTS[kind]:
                if getattr(row, key) is None:
                    raise ValueError(f"Campo auxiliar obrigatório: {key}.")
            if kind in ("MOMENTUM_RANKING", "RANKING_CONFIRMATION") and (
                not row.universe or "BTCUSDT" not in row.universe or len(row.universe) < 2
            ):
                raise ValueError("Ranking exige BTCUSDT e pelo menos outro ativo.")
        return {row.open_time_ms: row.model_dump(exclude_none=True) for row in rows}
    except (ValueError, TypeError) as exc:
        raise ValueError(f"auxiliary_json: {exc}") from None


class AuxiliaryParameters(TutorialParameters):
    auxiliary_json: str = Field(
        default="[]",
        title="Dados auxiliares JSON",
        max_length=8 * 1024 * 1024,
        json_schema_extra={"contentMediaType": "application/json"},
    )


def parameter_model(kind, fields):
    return create_model(
        kind + "Parameters",
        __base__=(AuxiliaryParameters if kind in REQUIREMENTS else TutorialParameters),
        **fields,
    )
