from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, File, Form, Header, Query, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pymongo.errors import PyMongoError
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from candlelab.application.service import Service
from candlelab.config import Settings
from candlelab.domain.candles import MAX_TIME_MS
from candlelab.domain.errors import AppError, invalid
from candlelab.domain.strategies import catalog
from candlelab.domain.combinations import CombinationDefinition, CombinationResponse
from candlelab.infrastructure.binance import BinanceClient
from candlelab.infrastructure.cache import CandleCache
from candlelab.infrastructure.store import MongoStore, digest
from candlelab.schemas import (
    InstrumentsResponse,
    StrategiesResponse,
    BinanceRequest,
    CandleResponse,
    Dataset,
    EquityPoint,
    ErrorResponse,
    EventPoint,
    IndicatorPoint,
    Page,
    Run,
    RunRequest,
    Session,
    SessionRequest,
)


class BodyLimit:
    def __init__(self, app, maximum):
        self.app, self.maximum = app, maximum

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        length = dict(scope.get("headers", [])).get(b"content-length")
        try:
            if length is not None and int(length) > self.maximum:
                raise invalid("file", "Tamanho máximo da requisição excedido.")
        except ValueError:
            return await JSONResponse(invalid("body", "Content-Length inválido.").body(), 422)(
                scope, receive, send
            )
        except AppError as exc:
            return await JSONResponse(exc.body(), exc.status)(scope, receive, send)
        consumed = 0

        async def bounded_receive():
            nonlocal consumed
            message = await receive()
            consumed += len(message.get("body", b""))
            if consumed > self.maximum:
                raise HTTPException(
                    status_code=422, detail="Tamanho máximo da requisição excedido."
                )
            return message

        try:
            await self.app(scope, bounded_receive, send)
        except AppError as exc:
            await JSONResponse(exc.body(), exc.status)(scope, receive, send)


class Pagination:
    def __init__(
        self,
        cursor: Annotated[str | None, Query(max_length=2048)] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    ):
        self.cursor, self.limit = cursor, limit


class TimeRange:
    def __init__(
        self,
        from_ms: Annotated[int | None, Query(ge=0, le=MAX_TIME_MS)] = None,
        to_ms: Annotated[int | None, Query(ge=0, le=MAX_TIME_MS)] = None,
    ):
        if from_ms is not None and to_ms is not None and from_ms > to_ms:
            raise invalid("from_ms", "Exija from_ms <= to_ms.")
        self.value = (from_ms, to_ms)


def create_app(settings=None, store=None, binance=None, cache=None):
    settings = settings or Settings()
    owns_store = store is None
    owns_binance = binance is None
    store = store if store is not None else MongoStore(settings)
    binance = binance if binance is not None else BinanceClient(settings)
    cache = cache if cache is not None else CandleCache(settings)
    service = Service(settings, store, binance)

    @asynccontextmanager
    async def lifespan(app):
        await run_in_threadpool(store.indexes)
        yield
        if owns_store:
            store.client.close()
        if owns_binance:
            binance.client.close()

    app = FastAPI(
        title="CandleLab API",
        version="0.3.0",
        lifespan=lifespan,
        responses={status: {"model": ErrorResponse} for status in (404, 409, 422, 502, 503)},
    )
    app.state.store = store
    app.state.service = service
    app.add_middleware(BodyLimit, maximum=settings.max_upload_bytes + 65536)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Content-Type", "Idempotency-Key"],
    )

    @app.exception_handler(AppError)
    async def app_error(request, exc):
        return JSONResponse(exc.body(), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        details = [
            {"field": ".".join(map(str, e["loc"])), "reason": e["msg"]} for e in exc.errors()
        ]
        return JSONResponse(
            {
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Requisição inválida.",
                    "details": details,
                }
            },
            422,
        )

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        if exc.status_code == 422:
            return JSONResponse(invalid("body", str(exc.detail)).body(), 422)
        return JSONResponse(
            {"error": {"code": "HTTP_ERROR", "message": str(exc.detail), "details": []}},
            exc.status_code,
        )

    @app.exception_handler(PyMongoError)
    async def mongo_error(request, exc):
        return JSONResponse(
            {
                "error": {
                    "code": "STORAGE_UNAVAILABLE",
                    "message": "MongoDB indisponível. Tente novamente.",
                    "details": [],
                }
            },
            503,
        )

    @app.get("/health")
    def health():
        store.ping()
        return {"status": "ok"}

    prefix = "/api/v1"

    @app.get(prefix + "/instruments", response_model=InstrumentsResponse)
    def instruments():
        return {
            "items": [
                {
                    "symbol": i.symbol,
                    "base_asset": i.base_asset,
                    "quote_asset": i.quote_asset,
                    "intervals": i.intervals,
                    "display_symbol": f"{i.base_asset}/{i.quote_asset}",
                }
                for i in settings.instruments
            ]
        }

    @app.get(prefix + "/strategies", response_model=StrategiesResponse)
    def strategies():
        return {"items": catalog()}

    @app.get(prefix + "/strategy-combinations", response_model=Page[CombinationResponse])
    def combinations():
        return {"items": store.combinations(), "next_cursor": None}

    @app.post(
        prefix + "/strategy-combinations", response_model=CombinationResponse, status_code=201
    )
    def create_combination(request: CombinationDefinition):
        return store.save_combination(request.model_dump())

    @app.put(prefix + "/strategy-combinations/{combination_id}", response_model=CombinationResponse)
    def update_combination(combination_id: str, request: CombinationDefinition):
        return store.save_combination(request.model_dump(), combination_id)

    @app.delete(prefix + "/strategy-combinations/{combination_id}", status_code=204)
    def delete_combination(combination_id: str):
        store.delete_combination(combination_id)
        return Response(status_code=204)

    @app.post(prefix + "/datasets", response_model=Dataset)
    async def upload(
        file: Annotated[UploadFile, File()],
        symbol: Annotated[str, Form(min_length=1, max_length=30)],
        interval: Annotated[str, Form(min_length=1, max_length=10)],
    ):
        try:
            raw = await file.read(settings.max_upload_bytes + 1)
            return await run_in_threadpool(service.upload, raw, file.filename, symbol, interval)
        finally:
            await file.close()

    @app.post(prefix + "/datasets/binance", response_model=Dataset)
    def acquire(request: BinanceRequest):
        return service.acquire(request)

    @app.get(prefix + "/datasets", response_model=Page[Dataset])
    def datasets(page: Annotated[Pagination, Depends()]):
        return store.page("datasets", {"status": "published"}, page.limit, page.cursor)

    @app.delete(prefix + "/datasets/{dataset_id}", status_code=204)
    def delete_dataset(dataset_id: str):
        store.delete_dataset(dataset_id)
        return Response(status_code=204)

    @app.get(prefix + "/datasets/{dataset_id}", response_model=Dataset)
    def dataset(dataset_id: str):
        return store.dataset(dataset_id)

    @app.get(prefix + "/datasets/{dataset_id}/candles", response_model=Page[CandleResponse])
    def candles(
        dataset_id: str,
        page: Annotated[Pagination, Depends()],
        times: Annotated[TimeRange, Depends()],
    ):
        store.dataset(dataset_id)
        key = "candles:v1:" + digest(
            {"id": dataset_id, "cursor": page.cursor, "limit": page.limit, "time": times.value}
        )

        # Project public fields before caching: IDs from Mongo never enter the cache.
        def load():
            result = store.page(
                "candles", {"dataset_id": dataset_id}, page.limit, page.cursor, times.value
            )
            result["items"] = [
                CandleResponse.model_validate(c).model_dump() for c in result["items"]
            ]
            return result

        return cache.read(key, load)

    @app.post(prefix + "/sessions", status_code=201, response_model=Session)
    def create_session(request: SessionRequest):
        return store.create_session(request.model_dump())

    @app.get(prefix + "/sessions", response_model=Page[Session])
    def sessions(page: Annotated[Pagination, Depends()]):
        return store.page("sessions", {"deleting": {"$ne": True}}, page.limit, page.cursor)

    @app.delete(prefix + "/sessions/{session_id}", status_code=204)
    def delete_session(session_id: str):
        store.delete_session(session_id)
        return Response(status_code=204)

    @app.get(prefix + "/sessions/{session_id}", response_model=Session)
    def session(session_id: str):
        return store.session(session_id)

    @app.post(prefix + "/sessions/{session_id}/runs", status_code=202, response_model=Run)
    def create_run(
        session_id: str,
        request: RunRequest,
        idempotency_key: Annotated[str | None, Header(min_length=1, max_length=200)] = None,
    ):
        return service.run(session_id, request, idempotency_key)

    @app.get(prefix + "/sessions/{session_id}/runs", response_model=Page[Run])
    def runs(session_id: str, page: Annotated[Pagination, Depends()]):
        store.session(session_id)
        return store.page("runs", {"session_id": session_id}, page.limit, page.cursor)

    @app.delete(prefix + "/runs/{run_id}", status_code=204)
    def delete_run(run_id: str):
        store.delete_run(run_id)
        return Response(status_code=204)

    @app.get(prefix + "/runs/{run_id}", response_model=Run)
    def run(run_id: str):
        return store.run(run_id)

    @app.get(
        prefix + "/runs/{run_id}/series",
        response_model=Page[IndicatorPoint | EquityPoint | EventPoint],
    )
    def series(
        run_id: str,
        kind: Literal["indicators", "events", "equity"],
        page: Annotated[Pagination, Depends()],
        times: Annotated[TimeRange, Depends()],
    ):
        run = store.run(run_id)
        if run["status"] != "completed":
            raise AppError(
                "SERIES_UNAVAILABLE", "Séries disponíveis somente após conclusão.", status=409
            )
        return store.page(
            "series",
            {"run_id": run_id, "attempt_id": run["published_attempt_id"], "kind": kind},
            page.limit,
            page.cursor,
            times.value,
        )

    return app


app = create_app()
