import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from candlelab.domain.candles import INTERVALS, parse_candles
from candlelab.domain.errors import AppError, invalid

BINANCE_URL = "https://data-api.binance.vision/api/v3/klines"


def utc_ms(value):
    return (
        int(datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())
        * 1000
    )


class BinanceClient:
    def __init__(self, settings, client=None, sleep=time.sleep, now=None):
        self.settings = settings
        self.client = client or httpx.Client(
            timeout=settings.binance_timeout, follow_redirects=False
        )
        self.sleep = sleep
        self.now = now or (lambda: int(time.time() * 1000))

    def page(self, query):
        for attempt in range(self.settings.binance_retries + 1):
            try:
                response = self.client.get(BINANCE_URL, params=query)
            except httpx.RequestError:
                if attempt == self.settings.binance_retries:
                    raise AppError(
                        "BINANCE_UNAVAILABLE", "Binance indisponível.", status=503
                    ) from None
                self.sleep(min(2**attempt, self.settings.max_retry_after))
                continue
            if response.status_code in (418, 429) or response.status_code >= 500:
                if attempt == self.settings.binance_retries:
                    raise AppError(
                        "BINANCE_UNAVAILABLE", "Limite ou indisponibilidade da Binance.", status=503
                    )
                retry = response.headers.get("Retry-After")
                try:
                    delay = float(retry)
                except (ValueError, TypeError):
                    try:
                        delay = (
                            parsedate_to_datetime(retry) - datetime.now(timezone.utc)
                        ).total_seconds()
                    except (ValueError, TypeError):
                        delay = 2**attempt
                # Do not retry before Retry-After if it exceeds our bounded wait budget.
                if delay > self.settings.max_retry_after:
                    raise AppError(
                        "BINANCE_UNAVAILABLE",
                        "Aguarde para consultar a Binance novamente.",
                        [{"retry_after_seconds": delay}],
                        status=503,
                    )
                self.sleep(max(0, delay))
                continue
            if response.status_code != 200:
                raise AppError("BINANCE_RESPONSE_ERROR", "Binance rejeitou a consulta.", status=502)
            try:
                rows = response.json()
            except ValueError:
                raise AppError(
                    "BINANCE_INVALID_RESPONSE", "Resposta da Binance não é JSON.", status=502
                ) from None
            if not isinstance(rows, list):
                raise AppError("BINANCE_INVALID_RESPONSE", "Esperado array de klines.", status=502)
            return rows
        raise AssertionError("Unreachable")

    def acquire(self, request):
        query = {"symbol": request.symbol, "interval": request.interval}
        for key in ("startTime", "endTime"):
            if getattr(request, key) is not None:
                query[key] = utc_ms(getattr(request, key))
        if request.limit is not None:
            query["limit"] = request.limit
        effective = query.copy()
        complete_range = "startTime" in query and "endTime" in query
        cutoff = self.now()
        all_rows = []
        duration = INTERVALS[request.interval]
        while True:
            rows = self.page(query)
            if not rows:
                break
            if len(rows) > query.get("limit", 500):
                raise AppError(
                    "BINANCE_INVALID_RESPONSE", "Página excede o limite solicitado.", status=502
                )
            # Validate every row (including open candles) before using its timestamps.
            try:
                candles = parse_candles(rows, request.interval, self.settings.max_candles)
            except AppError as exc:
                raise AppError("BINANCE_INVALID_RESPONSE", exc.message, exc.details, 502) from None
            for candle in candles:
                if ("startTime" in query and candle.open_time_ms < query["startTime"]) or (
                    "endTime" in query and candle.open_time_ms > query["endTime"]
                ):
                    raise AppError(
                        "BINANCE_INVALID_RESPONSE", "Candle fora da janela solicitada.", status=502
                    )
            if all_rows and candles[0].open_time_ms != all_rows[-1][0] + duration:
                raise AppError(
                    "BINANCE_INVALID_RESPONSE", "Lacuna ou duplicata entre páginas.", status=502
                )
            all_rows.extend(
                row for row, candle in zip(rows, candles) if candle.close_time_ms < cutoff
            )
            if len(all_rows) > self.settings.max_candles:
                raise invalid("limit", "Limite total de aquisição excedido.")
            if not complete_range:
                break
            following = candles[-1].open_time_ms + duration
            if following <= query["startTime"]:
                raise AppError("BINANCE_INVALID_RESPONSE", "Paginação não avançou.", status=502)
            if following > query["endTime"] or candles[-1].close_time_ms >= cutoff:
                break
            query["startTime"] = following
        if not all_rows:
            raise invalid("period", "Período sem candles fechados.")
        try:
            normalized = parse_candles(all_rows, request.interval, self.settings.max_candles)
        except AppError as exc:
            raise AppError("BINANCE_INVALID_RESPONSE", exc.message, exc.details, 502) from None
        return normalized, {"mode": "BINANCE_API", "query": effective, "closed_before_ms": cutoff}
