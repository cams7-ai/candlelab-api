"""Nomes legíveis para conjuntos de candles, com datas em UTC."""

from datetime import datetime, timezone


def dataset_name(symbol, interval, start_ms, end_ms):
    def stamp(ms):
        return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H%MZ")

    return f"{symbol.lower()}_{interval}_{stamp(start_ms)}_{stamp(end_ms)}"
