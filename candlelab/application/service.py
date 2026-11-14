import hashlib

from candlelab.domain.candles import decode_json, parse_candles
from candlelab.domain.engine import ENGINE_VERSION, EXECUTION_POLICY
from candlelab.domain.errors import invalid
from candlelab.domain.numbers import NUMERIC_POLICY
from candlelab.domain.strategies import resolve


class Service:
    def __init__(self, settings, store, binance):
        self.settings, self.store, self.binance = settings, store, binance

    def upload(self, raw, filename, symbol, interval):
        instrument = self.settings.instrument(symbol, interval)
        if len(raw) > self.settings.max_upload_bytes:
            raise invalid("file", "Tamanho máximo de upload excedido.")
        candles = parse_candles(decode_json(raw), interval, self.settings.max_candles)
        provenance = {
            "mode": "UPLOAD",
            "filename": filename,
            "file_sha256": hashlib.sha256(raw).hexdigest(),
        }
        return self.store.publish_dataset(candles, instrument, interval, provenance)

    def acquire(self, request):
        instrument = self.settings.instrument(request.symbol, request.interval)
        candles, provenance = self.binance.acquire(request)
        return self.store.publish_dataset(candles, instrument, request.interval, provenance)

    def run(self, session_id, request, key):
        _, parameters = resolve(request.strategy_id, request.parameters)
        session = self.store.session(session_id)
        dataset = self.store.dataset(session["dataset_id"])
        instrument = self.settings.instrument(dataset["symbol"], dataset["interval"])
        policy = {
            "engine_version": ENGINE_VERSION,
            "execution_policy": EXECUTION_POLICY,
            "numeric_policy": NUMERIC_POLICY,
            "quantity_step": instrument.quantity_step,
            "instrument_version": instrument.version,
            "slippage": "0",
        }
        return self.store.create_run(
            session_id, request.strategy_id, parameters.model_dump(), key, policy
        )
