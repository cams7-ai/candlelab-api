import hashlib
import json
from dataclasses import asdict, dataclass
from decimal import Decimal

from candlelab.domain.errors import invalid
from candlelab.domain.numbers import decimal_input, wire

INTERVALS = {"1m": 60_000, "5m": 300_000, "1h": 3_600_000}
MAX_TIME_MS = 253402300799999
DECIMAL_FIELDS = {
    1: "open",
    2: "high",
    3: "low",
    4: "close",
    5: "base_volume",
    7: "quote_volume",
    9: "taker_base_volume",
    10: "taker_quote_volume",
}


@dataclass(frozen=True)
class Candle:
    open_time_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    base_volume: Decimal
    close_time_ms: int
    quote_volume: Decimal
    trade_count: int
    taker_base_volume: Decimal
    taker_quote_volume: Decimal
    reserved: str

    def document(self):
        return {k: wire(v) if isinstance(v, Decimal) else v for k, v in asdict(self).items()}

    @classmethod
    def from_document(cls, doc):
        return cls(
            **{
                k: Decimal(doc[k]) if k in DECIMAL_FIELDS.values() else doc[k]
                for k in cls.__dataclass_fields__
            }
        )


def decode_json(raw):
    try:
        return json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise invalid("file", "JSON inválido.") from None


def parse_candles(rows, interval, maximum):
    if interval not in INTERVALS:
        raise invalid("interval", "Intervalo não suportado.")
    if not isinstance(rows, list) or not rows or len(rows) > maximum:
        raise invalid("file", f"Esperado array com 1 a {maximum} candles.")
    duration = INTERVALS[interval]
    candles = []
    for index, row in enumerate(rows):
        if not isinstance(row, list) or len(row) != 12:
            raise invalid("row", "Candle deve conter exatamente 12 posições.", index)
        for pos in (0, 6, 8):
            if (
                type(row[pos]) is not int
                or row[pos] < 0
                or row[pos] > (MAX_TIME_MS if pos != 8 else 2**63 - 1)
            ):
                raise invalid(str(pos), "Esperado inteiro não negativo válido.", index)
        if not isinstance(row[11], str) or len(row[11]) > 256:
            raise invalid(
                "reserved", "Campo reservado deve ser string de até 256 caracteres.", index
            )
        values = {}
        for pos, field in DECIMAL_FIELDS.items():
            try:
                values[field] = decimal_input(row[pos], field)
            except Exception as exc:
                if hasattr(exc, "details"):
                    exc.details[0]["row"] = index
                raise
            if values[field] < 0 or (pos in (1, 2, 3, 4) and values[field] == 0):
                raise invalid(field, "Preço deve ser positivo e volume não negativo.", index)
        if row[0] % duration or row[6] != row[0] + duration - 1:
            raise invalid(
                "interval", "Timestamps não correspondem ao intervalo alinhado UTC.", index
            )
        if candles and row[0] != candles[-1].open_time_ms + duration:
            reason = (
                "Duplicata ou abertura fora de ordem."
                if row[0] <= candles[-1].open_time_ms
                else "Lacuna entre candles."
            )
            raise invalid("open_time_ms", reason, index)
        if not (
            values["low"]
            <= min(values["open"], values["close"])
            <= max(values["open"], values["close"])
            <= values["high"]
        ):
            raise invalid("OHLC", "Relação entre preços OHLC inválida.", index)
        candles.append(
            Candle(
                open_time_ms=row[0],
                close_time_ms=row[6],
                trade_count=row[8],
                reserved=row[11],
                **values,
            )
        )
    return candles


def content_hash(candles):
    normalized = json.dumps(
        [c.document() for c in candles], sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(normalized.encode()).hexdigest()
