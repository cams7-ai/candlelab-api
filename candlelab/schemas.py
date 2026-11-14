import re
from datetime import datetime, timezone
from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from candlelab.domain.numbers import DECIMAL_PATTERN, decimal_input, wire

DecimalString = Annotated[str, Field(pattern=DECIMAL_PATTERN)]
Timestamp = Annotated[int, Field(ge=0)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BinanceRequest(Input):
    symbol: str = Field(min_length=1, max_length=30)
    interval: str = Field(min_length=1, max_length=10)
    startTime: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
    endTime: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
    limit: Annotated[StrictInt, Field(ge=1, le=1000)] | None = None

    @field_validator("symbol")
    @classmethod
    def uppercase(cls, value):
        return value.upper()

    @field_validator("startTime", "endTime")
    @classmethod
    def date(cls, value):
        if value is not None:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
                raise ValueError("Use YYYY-MM-DDTHH:mm:ssZ.")
            dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if dt.year < 1970:
                raise ValueError("Data anterior à época Unix.")
        return value

    @model_validator(mode="after")
    def ordered(self):
        if (
            self.startTime is not None
            and self.endTime is not None
            and self.startTime > self.endTime
        ):
            raise ValueError("Exija startTime <= endTime.")
        return self


class SessionRequest(Input):
    name: str = Field(min_length=1, max_length=200)
    dataset_id: str = Field(min_length=1, max_length=128)
    initial_cash: DecimalString
    fee_rate: DecimalString

    @field_validator("initial_cash", "fee_rate")
    @classmethod
    def financial(cls, value, info):
        d = decimal_input(value, info.field_name)
        if (info.field_name == "initial_cash" and d <= 0) or (
            info.field_name == "fee_rate" and not 0 <= d < 1
        ):
            raise ValueError("Caixa deve ser positivo e taxa deve estar em [0,1).")
        return wire(d)


class RunRequest(Input):
    strategy_id: str = Field(min_length=1, max_length=100)
    parameters: dict[str, Any] = Field(default_factory=dict)


class ErrorValue(BaseModel):
    code: str
    message: str
    details: list[Any]


class ErrorResponse(BaseModel):
    error: ErrorValue


class Dataset(BaseModel):
    id: str
    source: Literal["BINANCE_SPOT"]
    symbol: str
    interval: str
    base_asset: str
    quote_asset: str
    display_symbol: str
    candle_count: int
    first_open_time_ms: Timestamp
    last_open_time_ms: Timestamp
    content_hash: str
    imported_at: str


class CandleResponse(BaseModel):
    open_time_ms: Timestamp
    close_time_ms: Timestamp
    open: DecimalString
    high: DecimalString
    low: DecimalString
    close: DecimalString
    base_volume: DecimalString


class Session(SessionRequest):
    id: str


class Summary(BaseModel):
    initial_cash: DecimalString
    final_cash: DecimalString
    final_position_qty: DecimalString
    final_position_value: DecimalString
    initial_equity: DecimalString
    final_equity: DecimalString
    total_fees: DecimalString
    realized_pnl: DecimalString
    unrealized_pnl: DecimalString
    return_pct: DecimalString
    buy_fills: int
    sell_fills: int
    closed_trades: int
    win_rate_pct: DecimalString | None
    max_drawdown_pct: DecimalString


class Run(BaseModel):
    id: str
    session_id: str
    strategy_id: str
    parameters: dict[str, Any]
    status: Literal["queued", "running", "completed", "failed"]
    summary: Summary | None
    error: ErrorResponse | None
    policy: dict[str, str]
    dataset_snapshot: dict[str, str]
    diagnostics: list[str] = []


class IndicatorPoint(BaseModel):
    open_time_ms: Timestamp
    values: dict[str, DecimalString | None]


class EquityPoint(BaseModel):
    open_time_ms: Timestamp
    equity: DecimalString


class EventPoint(BaseModel):
    id: str
    seq: int
    kind: Literal["signal", "fill", "skipped"]
    side: Literal["BUY", "SELL"]
    open_time_ms: Timestamp
    occurred_at_ms: Timestamp
    signal_id: str | None = None
    price: DecimalString | None = None
    quantity: DecimalString | None = None
    fee: DecimalString | None = None
    gross: DecimalString | None = None
    reason: str | None = None


T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    next_cursor: str | None


class InstrumentResponse(BaseModel):
    symbol: str
    base_asset: str
    quote_asset: str
    intervals: list[str]
    display_symbol: str


class StrategyResponse(BaseModel):
    id: str
    name: str
    parameters_schema: dict[str, Any]
    defaults: dict[str, Any]


class InstrumentsResponse(BaseModel):
    items: list[InstrumentResponse]


class StrategiesResponse(BaseModel):
    items: list[StrategyResponse]
