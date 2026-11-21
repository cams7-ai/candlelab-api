"""Shared strategy contracts, independent of the registry."""

from dataclasses import dataclass, field
from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field, StrictInt

Window = Annotated[StrictInt, Field(ge=1, le=10000)]


class Parameters(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class Evaluation:
    values: dict
    ready: bool
    side: str | None
    reason: str
    order: dict | None = None
    buy_allowed: bool | None = None


@dataclass(frozen=True)
class StrategySpec:
    name: str
    parameters: type[Parameters]
    factory: object
    category: str = "legacy"
    rules: str = ""
    limitations: str = ""
    required_data: list[str] = field(default_factory=list)
    composition_role: str = "signal"
    components: list[dict] = field(default_factory=list)
