"""Validated saved definitions and composition of compatible Spot signals."""

import json
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from candlelab.domain.strategy_types import Parameters, Evaluation, StrategySpec

CUSTOM_ID = "CUSTOM_COMBINATION@1"
COMBINABLE_IDS = {
    "GRID_TRADING@1",
    "GRID_ADX_ATR@1",
    "SMA_CROSS@1",
    "EMA_CROSS@1",
    "RSI_REVERSION@1",
    "SMA_RSI_FILTER@1",
    "TREND_FOLLOWING@1",
    "MOMENTUM@1",
    "MEAN_REVERSION@1",
    "BREAKOUT@1",
    "SWING_TRADING@1",
    "EMA_ADX_ATR@1",
    "RSI_BOLLINGER@1",
    "BREAKOUT_VOLUME_ATR@1",
    "MACD_EMA_RSI@1",
}


def combinable(strategy_id, spec):
    from candlelab.domain.tutorial import COMPONENT_FIELDS

    return (
        strategy_id in COMBINABLE_IDS or strategy_id.removesuffix("@1") in COMPONENT_FIELDS
    ) and not spec.required_data


class Member(Parameters):
    strategy_id: str = Field(default="", max_length=100)
    parameters: dict[str, Any] = Field(default_factory=dict)
    combination: "CombinationDefinition | None" = None

    @model_validator(mode="after")
    def exclusive(self):
        if self.combination is not None:
            if self.strategy_id or self.parameters:
                raise ValueError("Um grupo recebe somente o snapshot da combinação.")
        elif not self.strategy_id:
            raise ValueError("Informe a estratégia ou a combinação.")
        return self


class CombinationDefinition(Parameters):
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["preset", "custom", "strategy"]
    strategy_id: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    members: list[Member] = Field(default_factory=list, max_length=8)
    buy_rule: Literal["all", "any"] = "all"
    sell_rule: Literal["all", "any"] = "any"

    @field_validator("name", mode="before")
    @classmethod
    def trim_name(cls, value):
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_definition(self):
        from candlelab.domain.strategies import REGISTRY, resolve

        if self.kind != "custom":
            if self.members or self.buy_rule != "all" or self.sell_rule != "any":
                raise ValueError("Configuração publicada não aceita membros ou regras livres.")
            spec = REGISTRY.get(self.strategy_id)
            if not spec or self.strategy_id == CUSTOM_ID:
                raise ValueError("Selecione uma estratégia publicada no catálogo.")
            if self.kind == "preset" and spec.category != "combination":
                raise ValueError("Selecione uma combinação publicada no catálogo.")
            if self.kind == "strategy" and spec.category == "combination":
                raise ValueError("Use o tipo preset para uma combinação publicada.")
            _, parsed = resolve(self.strategy_id, self.parameters)
            self.parameters = parsed.model_dump()
        else:
            if self.strategy_id is not None or self.parameters:
                raise ValueError("Combinação livre recebe membros, sem estratégia base.")
            if not 2 <= len(self.members) <= 8:
                raise ValueError("Selecione entre 2 e 8 estratégias distintas.")
            ids = [
                member.strategy_id or json.dumps(member.combination.model_dump(), sort_keys=True)
                for member in self.members
            ]
            if len(set(ids)) != len(ids):
                raise ValueError("Estratégias repetidas na combinação.")
            for member in self.members:
                if member.combination is not None:
                    validate_tree(member.combination)
                    continue
                spec = REGISTRY.get(member.strategy_id)
                if not spec or not combinable(member.strategy_id, spec):
                    raise ValueError("Estratégia incompatível com composição Spot OHLCV.")
                _, parsed = resolve(member.strategy_id, member.parameters)
                member.parameters = parsed.model_dump()
            validate_tree(self)
            if grid_count(self) > 1:
                raise ValueError("Use somente uma grade por combinação, incluindo grupos internos.")
            if not grid_count(self) and any(
                (
                    member.combination.strategy_id
                    if member.combination is not None
                    else member.strategy_id
                )
                == "GRID_REGIME@1"
                and (
                    member.combination.parameters
                    if member.combination is not None
                    else member.parameters
                ).get("regime_action", "filter")
                == "disable_grid"
                for member in self.members
            ):
                raise ValueError("A ação Desativar grade exige uma grade na mesma composição.")
            if not has_signal(self):
                raise ValueError(
                    "Inclua pelo menos uma estratégia de sinal, além de filtros e saídas."
                )
        return self


def validate_tree(definition, depth=1, counter=None):
    """Embedded snapshots cannot reference IDs or form live dependency cycles."""
    from candlelab.domain.strategies import REGISTRY

    counter = counter if counter is not None else [0]
    if depth > 4:
        raise ValueError("Combinações aceitam até 4 níveis de agrupamento.")
    if definition.kind != "custom":
        spec = REGISTRY.get(definition.strategy_id)
        if depth > 1 and (not spec or not combinable(definition.strategy_id, spec)):
            raise ValueError("Combinação publicada incompatível com composição Spot OHLCV.")
        counter[0] += 1
    else:
        for member in definition.members:
            if member.combination is not None:
                validate_tree(member.combination, depth + 1, counter)
            else:
                counter[0] += 1
    if counter[0] > 64:
        raise ValueError("Combinações aceitam até 64 estratégias no total.")


GRID_IDS = {"GRID_TRADING@1", "GRID_ADX_ATR@1"}


def grid_count(definition):
    if definition.kind != "custom":
        return int(definition.strategy_id in GRID_IDS)
    return sum(
        grid_count(member.combination)
        if member.combination is not None
        else int(member.strategy_id in GRID_IDS)
        for member in definition.members
    )


def grid_member(member):
    return (
        grid_count(member.combination) > 0
        if member.combination is not None
        else member.strategy_id in GRID_IDS
    )


def has_signal(definition):
    from candlelab.domain.strategies import REGISTRY

    if definition.kind != "custom":
        return REGISTRY[definition.strategy_id].composition_role == "signal"
    return any(member_role(member) == "signal" for member in definition.members)


def member_role(member):
    from candlelab.domain.strategies import REGISTRY

    if member.combination is not None:
        definition = member.combination
        return (
            "signal"
            if definition.kind == "custom"
            else REGISTRY[definition.strategy_id].composition_role
        )
    return REGISTRY[member.strategy_id].composition_role


# Resolve the forward annotation after both models exist.
Member.model_rebuild()


class CombinationResponse(CombinationDefinition):
    id: str


class CustomParameters(Parameters):
    definition_json: str = Field(max_length=100000)

    @field_validator("definition_json")
    @classmethod
    def definition(cls, value):
        definition = CombinationDefinition.model_validate_json(value)
        if definition.kind != "custom":
            raise ValueError("O executor livre exige definição custom.")
        return json.dumps(definition.model_dump(), sort_keys=True, separators=(",", ":"))


class CombinedStrategy:
    def __init__(self, parameters):
        from candlelab.domain.strategies import resolve

        self.definition = CombinationDefinition.model_validate_json(parameters.definition_json)
        self.strategies = []
        for member in self.definition.members:
            if member.combination is not None:
                definition = member.combination
                if definition.kind == "custom":
                    self.strategies.append(
                        CombinedStrategy(
                            CustomParameters(definition_json=definition.model_dump_json())
                        )
                    )
                    continue
                strategy_id, parameters = definition.strategy_id, definition.parameters
            else:
                strategy_id, parameters = member.strategy_id, member.parameters
            spec, parsed = resolve(strategy_id, parameters)
            self.strategies.append(spec.factory(parsed))

        # Filters are evaluated before signals. A disabling regime is bound to
        # the single grid in this group (which can be inside a nested group).
        grid = self.grid_strategy()
        regimes = [
            strategy
            for strategy in self.strategies
            if getattr(strategy, "kind", None) == "GRID_REGIME"
            and strategy.p.regime_action == "disable_grid"
        ]
        if regimes and grid is None:
            raise ValueError("A ação Desativar grade exige uma grade na mesma composição.")
        if grid is not None:
            grid.grid_regimes.extend(regimes)

    def grid_strategy(self):
        for strategy in self.strategies:
            if isinstance(strategy, CombinedStrategy):
                grid = strategy.grid_strategy()
                if grid is not None:
                    return grid
            elif getattr(strategy, "kind", None) in ("GRID_TRADING", "GRID_ADX_ATR"):
                return strategy
        return None

    def on_fill(self, side, price):
        for strategy in self.strategies:
            if hasattr(strategy, "on_fill"):
                strategy.on_fill(side, price)

    def on_close(self, candle):
        # Disabling filters must publish the current candle before a nested grid
        # initializes its range or generates a limit order. Keep result ordering
        # unchanged for member prefixes and voting.
        roles = [member_role(member) for member in self.definition.members]
        evaluations = [None] * len(self.strategies)
        for filter_pass in (True, False):
            for index, (strategy, role) in enumerate(zip(self.strategies, roles)):
                if (role == "filter") == filter_pass:
                    evaluations[index] = strategy.on_close(candle)
        values = {
            f"member_{index + 1}__{key}": value
            for index, result in enumerate(evaluations)
            for key, value in result.values.items()
        }
        grid = next(
            (
                result
                for member, result in zip(self.definition.members, evaluations)
                if grid_member(member)
            ),
            None,
        )
        # A disabled grid or its stop must close the inventory even while another
        # component warms up, and must never inherit a stale take-profit limit.
        if grid is not None and grid.ready and grid.side == "SELL" and grid.order is None:
            return Evaluation(values, True, "SELL", grid.reason)
        if not all(result.ready for result in evaluations):
            return Evaluation(values, False, None, "COMBINATION_WARMUP")
        permitted = all(
            result.buy_allowed for result, role in zip(evaluations, roles) if role == "filter"
        )
        # Filters gate entry regardless of all/any, and never fabricate an exit vote.
        # Existing compositions contain only signal members and retain their old semantics.
        for side, rule in (("SELL", self.definition.sell_rule), ("BUY", self.definition.buy_rule)):
            votes = [
                result.side == side
                for result, role in zip(evaluations, roles)
                if role == "signal" or (side == "SELL" and role == "exit")
            ]
            matched = bool(votes) and (all(votes) if rule == "all" else any(votes))
            if side == "BUY" and grid is not None:
                matched = matched and grid.side == "BUY"
            if matched and (side == "SELL" or permitted):
                order = None
                if grid is not None and grid.side == side:
                    # An additional market exit takes precedence over grid profit
                    # limits. Entry sizing and limits always belong to the grid.
                    market_exit = side == "SELL" and any(
                        result.side == "SELL" and result.order is None for result in evaluations
                    )
                    if not market_exit:
                        order = grid.order
                return Evaluation(values, True, side, f"COMBINATION_{rule.upper()}_{side}", order)
        return Evaluation(values, True, None, "COMBINATION_NO_CONSENSUS")


CUSTOM_SPEC = StrategySpec(
    "Combinação livre",
    CustomParameters,
    CombinedStrategy,
    category="combination",
    rules="Sinais no mesmo candle, carteira compartilhada, venda prioritária em conflitos.",
)
