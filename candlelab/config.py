from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from candlelab.domain.candles import INTERVALS
from candlelab.domain.numbers import decimal_input


class Instrument(BaseModel):
    symbol: str
    base_asset: str
    quote_asset: str = "USDT"
    intervals: list[str] = ["1m", "5m"]
    quantity_step: str = "0.000001"
    version: str = "SIMULATED_STEP_V1"

    @model_validator(mode="after")
    def valid(self):
        if (
            self.quote_asset != "USDT"
            or self.symbol != self.base_asset + "USDT"
            or not self.base_asset.isalpha()
            or not self.base_asset.isupper()
            or not self.intervals
            or any(i not in INTERVALS for i in self.intervals)
            or decimal_input(self.quantity_step, "quantity_step") <= 0
        ):
            raise ValueError("Instrumento Spot USDT inválido.")
        return self


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="CANDLELAB_", extra="ignore")
    mongo_uri: str = "mongodb://localhost:27017"
    mongo_database: str = "candlelab"
    redis_url: str = "redis://localhost:6379/0"
    cors_origins: list[str] = ["http://localhost:3000", "http://localhost:5173"]
    instruments: list[Instrument] = [
        Instrument(symbol="BTCUSDT", base_asset="BTC"),
        Instrument(symbol="ETHUSDT", base_asset="ETH"),
    ]
    max_upload_bytes: int = Field(default=32 * 1024 * 1024, ge=1024, le=128 * 1024 * 1024)
    max_candles: int = Field(default=100_000, ge=1, le=1_000_000)
    batch_size: int = Field(default=500, ge=1, le=2000)
    cache_ttl: int = Field(default=300, ge=1)
    lease_seconds: int = Field(default=60, ge=3)
    poll_seconds: float = Field(default=1, ge=0.01)
    binance_timeout: float = Field(default=10, gt=0)
    binance_retries: int = Field(default=3, ge=0, le=5)
    max_retry_after: float = Field(default=30, ge=0, le=300)

    @model_validator(mode="after")
    def unique_symbols(self):
        if len({i.symbol for i in self.instruments}) != len(self.instruments):
            raise ValueError("Símbolos duplicados no catálogo.")
        return self

    def instrument(self, symbol, interval=None):
        from candlelab.domain.errors import invalid

        for item in self.instruments:
            if item.symbol == symbol.upper():
                if interval is not None and interval not in item.intervals:
                    raise invalid("interval", "Intervalo não disponível no catálogo.")
                return item
        raise invalid("symbol", "Símbolo não disponível no catálogo.")
