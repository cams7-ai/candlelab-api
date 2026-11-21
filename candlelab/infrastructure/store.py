"""MongoDB é a fonte de verdade; snapshots imutáveis e leases com fencing."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from pymongo import ASCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

from candlelab.domain.candles import MAX_TIME_MS, Candle, content_hash
from candlelab.domain.errors import AppError, invalid
from candlelab.domain.configuration_usage import configuration_usage
from candlelab.domain.dataset_names import dataset_name


def now():
    return datetime.now(timezone.utc)


def clean(doc):
    return {k: v for k, v in doc.items() if k != "_id"}


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class MongoStore:
    def __init__(self, settings, db=None):
        self.settings = settings
        self.client = (
            None
            if db is not None
            else MongoClient(
                settings.mongo_uri, serverSelectionTimeoutMS=3000, tz_aware=True, connect=False
            )
        )
        self.db = db if db is not None else self.client[settings.mongo_database]

    def indexes(self):
        self.db.datasets.create_index(
            [("source", 1), ("symbol", 1), ("interval", 1), ("content_hash", 1)], unique=True
        )
        self.db.datasets.create_index([("status", 1), ("id", 1)])
        self.db.candles.create_index([("dataset_id", 1), ("open_time_ms", 1)], unique=True)
        self.db.acquisitions.create_index([("dataset_id", 1), ("acquired_at", 1)])
        self.db.sessions.create_index("id", unique=True)
        self.db.strategy_combinations.create_index("id", unique=True)
        self.db.strategy_combinations.create_index("name_key", unique=True)
        self.db.runs.create_index("id", unique=True)
        self.db.runs.create_index([("session_id", 1), ("id", 1)])
        self.db.runs.create_index(
            [("session_id", 1), ("idempotency_key", 1)],
            unique=True,
            partialFilterExpression={"idempotency_key": {"$type": "string"}},
        )
        self.db.runs.create_index([("status", 1), ("lease_until", 1)])
        self.db.series.create_index(
            [("run_id", 1), ("attempt_id", 1), ("kind", 1), ("open_time_ms", 1), ("seq", 1)],
            unique=True,
        )

    def ping(self):
        self.db.command("ping")

    def publish_dataset(self, candles, instrument, interval, provenance):
        query = provenance.get("query", {})
        name = (
            provenance.get("filename") if provenance.get("mode") == "UPLOAD" else None
        ) or dataset_name(
            instrument.symbol,
            interval,
            query.get("startTime", candles[0].open_time_ms),
            query.get("endTime", candles[-1].open_time_ms),
        )
        hashed = content_hash(candles)
        identity = {
            "source": "BINANCE_SPOT",
            "symbol": instrument.symbol,
            "interval": interval,
            "content_hash": hashed,
        }
        dataset_id = digest(identity)
        document = {
            "_id": dataset_id,
            "id": dataset_id,
            **identity,
            "base_asset": instrument.base_asset,
            "quote_asset": instrument.quote_asset,
            "display_symbol": f"{instrument.base_asset}/{instrument.quote_asset}",
            "candle_count": len(candles),
            "first_open_time_ms": candles[0].open_time_ms,
            "last_open_time_ms": candles[-1].open_time_ms,
            "status": "importing",
            "imported_at": now().isoformat(),
        }
        try:
            self.db.datasets.update_one(
                {"_id": dataset_id},
                {"$setOnInsert": document, "$set": {"name": name}},
                upsert=True,
            )
        except DuplicateKeyError:
            pass
        # Immutable content yields deterministic row IDs. Concurrent writers can safely resume
        # identical batches; no writer deletes rows that another writer may have published.
        if self.db.datasets.find_one({"_id": dataset_id})["status"] != "published":
            for offset in range(0, len(candles), self.settings.batch_size):
                docs = [
                    {
                        "_id": f"{dataset_id}:{c.open_time_ms}",
                        "dataset_id": dataset_id,
                        **c.document(),
                    }
                    for c in candles[offset : offset + self.settings.batch_size]
                ]
                from pymongo.errors import BulkWriteError

                try:
                    self.db.candles.insert_many(docs, ordered=False)
                except BulkWriteError as exc:
                    if exc.details.get("writeConcernErrors") or any(
                        e["code"] != 11000 for e in exc.details.get("writeErrors", [])
                    ):
                        raise
            if self.db.candles.count_documents({"dataset_id": dataset_id}) != len(candles):
                raise AppError(
                    "IMPORT_INCOMPLETE", "Importação incompleta; repita para retomar.", status=503
                )
            self.db.datasets.update_one(
                {"_id": dataset_id, "status": "importing"}, {"$set": {"status": "published"}}
            )
        self.db.acquisitions.insert_one(
            {
                "_id": uuid4().hex,
                "dataset_id": dataset_id,
                "acquired_at": now().isoformat(),
                **provenance,
            }
        )
        return self.dataset(dataset_id)

    def dataset(self, dataset_id):
        doc = self.db.datasets.find_one({"id": dataset_id, "status": "published"})
        if not doc:
            raise AppError("NOT_FOUND", "Dataset não encontrado.", status=404)
        return clean(doc)

    def _assert_dataset_unused(self, dataset_id):
        sessions = list(
            self.db.sessions.find({"dataset_id": dataset_id}, {"_id": 0, "id": 1, "name": 1})
        )
        if sessions:
            raise AppError(
                "DATASET_IN_USE",
                "Este conjunto de candles não pode ser excluído: existem sessões vinculadas.",
                details=[
                    {
                        "field": "dataset_id",
                        "reason": "Remova as sessões vinculadas antes de excluir os candles.",
                        "sessions": sessions,
                    }
                ],
                status=409,
            )

    def delete_dataset(self, dataset_id):
        if not self.db.datasets.find_one({"id": dataset_id}):
            raise AppError("NOT_FOUND", "Dataset não encontrado.", status=404)
        self._assert_dataset_unused(dataset_id)
        dataset = self.db.datasets.find_one_and_update(
            {"id": dataset_id}, {"$set": {"status": "deleting"}}
        )
        if not dataset:
            raise AppError("NOT_FOUND", "Dataset não encontrado.", status=404)
        try:
            # Fence new sessions, then recheck sessions that raced with the first check.
            self._assert_dataset_unused(dataset_id)
        except Exception:
            self.db.datasets.update_one(
                {"id": dataset_id, "status": "deleting"},
                {"$set": {"status": dataset["status"]}},
            )
            raise
        self.db.candles.delete_many({"dataset_id": dataset_id})
        self.db.acquisitions.delete_many({"dataset_id": dataset_id})
        self.db.datasets.delete_one({"id": dataset_id, "status": "deleting"})

    def candles(self, dataset_id):
        for doc in self.db.candles.find({"dataset_id": dataset_id}).sort("open_time_ms", ASCENDING):
            yield Candle.from_document(doc)

    def combination(self, combination_id):
        doc = self.db.strategy_combinations.find_one({"id": combination_id})
        if not doc:
            raise AppError("NOT_FOUND", "Combinação não encontrada.", status=404)
        return {k: v for k, v in doc.items() if k not in ("_id", "name_key")}

    def combinations(self):
        return [
            {k: v for k, v in doc.items() if k not in ("_id", "name_key")}
            for doc in self.db.strategy_combinations.find().sort("name_key", ASCENDING)
        ]

    def save_combination(self, data, combination_id=None):
        if combination_id is not None:
            self.combination(combination_id)
        doc = {**data, "id": combination_id or uuid4().hex, "name_key": data["name"].casefold()}
        try:
            if combination_id is None:
                self.db.strategy_combinations.insert_one(doc.copy())
            elif not self.db.strategy_combinations.replace_one(
                {"id": combination_id}, doc.copy()
            ).matched_count:
                raise AppError("NOT_FOUND", "Combinação não encontrada.", status=404)
        except DuplicateKeyError:
            raise AppError(
                "DUPLICATE_NAME", "Já existe uma combinação com esse nome.", status=409
            ) from None
        return {k: v for k, v in doc.items() if k != "name_key"}

    def delete_combination(self, combination_id):
        target = self.combination(combination_id)
        configuration = target["kind"] == "strategy"
        noun = "configuração" if configuration else "combinação"
        usage = configuration_usage(
            target,
            self.db.strategy_combinations.find(
                {"kind": "custom"}, {"id": 1, "name": 1, "members": 1}
            ),
            self.db.runs.find(
                {"combination_snapshot": {"$ne": None}},
                {"id": 1, "combination_snapshot": 1},
            ),
        )
        if any(usage.values()):
            raise AppError(
                "CONFIGURATION_IN_USE" if configuration else "COMBINATION_IN_USE",
                f"Esta {noun} não pode ser excluída: existem vínculos ou cópias "
                "cuja origem não pode ser descartada.",
                details=[
                    {
                        "field": "combination_id",
                        "reason": f"Remova os vínculos antes de excluir a {noun}. "
                        "Cópias sem identificador de origem exigem revisão.",
                        **usage,
                    }
                ],
                status=409,
            )
        if not self.db.strategy_combinations.delete_one({"id": combination_id}).deleted_count:
            raise AppError("NOT_FOUND", "Combinação não encontrada.", status=404)

    def session(self, session_id):
        doc = self.db.sessions.find_one({"id": session_id, "deleting": {"$ne": True}})
        if not doc:
            raise AppError("NOT_FOUND", "Sessão não encontrada.", status=404)
        return clean(doc)

    def _assert_session_unused(self, session_id):
        runs = list(self.db.runs.find({"session_id": session_id}, {"_id": 0, "id": 1, "status": 1}))
        if runs:
            raise AppError(
                "SESSION_IN_USE",
                "Esta sessão não pode ser excluída: existem processamentos vinculados.",
                details=[
                    {
                        "field": "session_id",
                        "reason": "Remova os processamentos antes de excluir a sessão.",
                        "runs": runs,
                    }
                ],
                status=409,
            )

    def delete_session(self, session_id):
        if not self.db.sessions.find_one({"id": session_id}):
            raise AppError("NOT_FOUND", "Sessão não encontrada.", status=404)
        self._assert_session_unused(session_id)
        session = self.db.sessions.find_one_and_update(
            {"id": session_id}, {"$set": {"deleting": True}}
        )
        if not session:
            raise AppError("NOT_FOUND", "Sessão não encontrada.", status=404)
        try:
            # Fence new runs, then catch insertions that raced with the first check.
            self._assert_session_unused(session_id)
        except Exception:
            restore = (
                {"$set": {"deleting": session["deleting"]}}
                if "deleting" in session
                else {"$unset": {"deleting": ""}}
            )
            self.db.sessions.update_one({"id": session_id, "deleting": True}, restore)
            raise
        self.db.sessions.delete_one({"id": session_id, "deleting": True})

    def create_session(self, data):
        self.dataset(data["dataset_id"])
        doc = {"id": uuid4().hex, **data}
        self.db.sessions.insert_one(doc.copy())
        try:
            self.dataset(data["dataset_id"])
        except AppError:
            # Dataset deletion may race with insertion after the initial check.
            if self.db.sessions.find_one({"id": doc["id"]}):
                self.delete_session(doc["id"])
            raise
        return doc

    def run(self, run_id):
        doc = self.db.runs.find_one({"id": run_id})
        if not doc or doc["status"] == "deleting":
            raise AppError("NOT_FOUND", "Processamento não encontrado.", status=404)
        return clean(doc)

    def delete_run(self, run_id):
        run = self.run(run_id)
        if run["status"] not in ("completed", "failed"):
            raise AppError(
                "RUN_ACTIVE", "Aguarde o término do processamento para excluir.", status=409
            )
        # Remove results first so an interrupted deletion can safely be retried.
        self.db.series.delete_many({"run_id": run_id})
        self.db.runs.delete_one({"id": run_id, "status": {"$in": ["completed", "failed"]}})

    def create_run(
        self,
        session_id,
        strategy_id,
        parameters,
        key,
        policy,
        auxiliary_filename=None,
        combination_snapshot=None,
    ):
        session = self.session(session_id)
        dataset = self.dataset(session["dataset_id"])
        request_data = {"strategy_id": strategy_id, "parameters": parameters}
        if auxiliary_filename is not None:
            request_data["auxiliary_filename"] = auxiliary_filename
        if combination_snapshot is not None:
            request_data["combination_snapshot"] = combination_snapshot
        request_hash = digest(request_data)
        if key is not None:
            existing = self.db.runs.find_one({"session_id": session_id, "idempotency_key": key})
            if existing:
                return self._idempotent(existing, request_hash)
        doc = {
            "id": uuid4().hex,
            "session_id": session_id,
            "strategy_id": strategy_id,
            "parameters": parameters,
            "auxiliary_filename": auxiliary_filename,
            "combination_snapshot": combination_snapshot,
            "status": "queued",
            "summary": None,
            "error": None,
            "request_hash": request_hash,
            "session_snapshot": session,
            "dataset_snapshot": {
                k: dataset[k] for k in ("id", "content_hash", "symbol", "interval")
            },
            "policy": policy,
            "created_at": now().isoformat(),
        }
        if key is not None:
            doc["idempotency_key"] = key
        try:
            self.db.runs.insert_one(doc.copy())
        except DuplicateKeyError:
            return self._idempotent(
                self.db.runs.find_one({"session_id": session_id, "idempotency_key": key}),
                request_hash,
            )
        try:
            self.session(session_id)
            self.dataset(session["dataset_id"])
        except AppError:
            # Session deletion may race with insertion after the initial check.
            self.db.runs.delete_one({"id": doc["id"]})
            self.db.series.delete_many({"run_id": doc["id"]})
            raise
        return doc

    @staticmethod
    def _idempotent(existing, request_hash):
        if existing["request_hash"] != request_hash:
            raise AppError(
                "IDEMPOTENCY_CONFLICT",
                "Chave de idempotência usada com outros parâmetros.",
                status=409,
            )
        return clean(existing)

    def claim(self):
        current = now()
        return self.db.runs.find_one_and_update(
            {
                "$or": [
                    {"status": "queued"},
                    {"status": "running", "lease_until": {"$lte": current}},
                ]
            },
            {
                "$set": {
                    "status": "running",
                    "attempt_id": uuid4().hex,
                    "lease_until": current + timedelta(seconds=self.settings.lease_seconds),
                }
            },
            sort=[("id", ASCENDING)],
            return_document=ReturnDocument.AFTER,
        )

    def ownership(self, job):
        return {
            "id": job["id"],
            "status": "running",
            "attempt_id": job["attempt_id"],
            "lease_until": {"$gt": now()},
        }

    def renew(self, job):
        result = self.db.runs.update_one(
            self.ownership(job),
            {"$set": {"lease_until": now() + timedelta(seconds=self.settings.lease_seconds)}},
        )
        if not result.matched_count:
            raise AppError("LEASE_LOST", "Posse da tarefa expirada.", status=409)

    def save_results(self, job, result):
        for kind in ("indicators", "events", "equity", "ledger", "evaluations"):
            rows = result[kind]
            for offset in range(0, len(rows), self.settings.batch_size):
                self.renew(job)
                self.db.series.insert_many(
                    [
                        {
                            "run_id": job["id"],
                            "attempt_id": job["attempt_id"],
                            "kind": kind,
                            "open_time_ms": row.get("open_time_ms", -1),
                            "seq": index,
                            "point": row,
                        }
                        for index, row in enumerate(
                            rows[offset : offset + self.settings.batch_size], start=offset
                        )
                    ]
                )
                try:
                    self.renew(job)
                except AppError:
                    self.db.series.delete_many(
                        {"run_id": job["id"], "attempt_id": job["attempt_id"]}
                    )
                    return False
        updated = self.db.runs.update_one(
            self.ownership(job),
            {
                "$set": {
                    "status": "completed",
                    "error": None,
                    "summary": result["summary"],
                    "diagnostics": result["diagnostics"],
                    "published_attempt_id": job["attempt_id"],
                    "completed_at": now().isoformat(),
                }
            },
        )
        if not updated.matched_count:
            self.db.series.delete_many({"run_id": job["id"], "attempt_id": job["attempt_id"]})
        return bool(updated.matched_count)

    def fail(self, job, error):
        return self.db.runs.update_one(
            self.ownership(job), {"$set": {"status": "failed", "error": error}}
        ).matched_count

    def page(self, collection, query, limit, cursor=None, time_range=None):
        scope = digest({"collection": collection, "query": query, "range": time_range})
        if cursor:
            import base64

            try:
                decoded = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                if decoded["scope"] != scope or not isinstance(decoded["last"], str):
                    raise ValueError()
                after = decoded["last"]
                if collection in ("candles", "series"):
                    stamp, seq = map(int, after.split(":"))
                    if not 0 <= stamp <= MAX_TIME_MS or not 0 <= seq <= 2**63 - 1:
                        raise ValueError()
                    if collection == "candles":
                        query = {"$and": [query, {"open_time_ms": {"$gt": stamp}}]}
                    else:
                        query = {
                            "$and": [
                                query,
                                {
                                    "$or": [
                                        {"open_time_ms": {"$gt": stamp}},
                                        {"open_time_ms": stamp, "seq": {"$gt": seq}},
                                    ]
                                },
                            ]
                        }
                else:
                    if not after or len(after) > 128:
                        raise ValueError()
                    query = {"$and": [query, {"id": {"$gt": after}}]}
            except (ValueError, KeyError, TypeError, UnicodeError):
                raise invalid("cursor", "Cursor inválido para esta consulta.") from None
        if time_range:
            lower, upper = time_range
            times = {}
            if lower is not None:
                times["$gte"] = lower
            if upper is not None:
                times["$lt"] = upper
            if times:
                query = {"$and": [query, {"open_time_ms": times}]}
        order = (
            [("open_time_ms", 1), ("seq", 1)]
            if collection in ("candles", "series")
            else [("id", 1)]
        )
        docs = list(self.db[collection].find(query).sort(order).limit(limit + 1))
        next_cursor = None
        if len(docs) > limit:
            import base64

            last = docs[limit - 1]
            after = (
                f"{last['open_time_ms']}:{last.get('seq', 0)}"
                if collection in ("candles", "series")
                else last["id"]
            )
            next_cursor = base64.urlsafe_b64encode(
                json.dumps({"scope": scope, "last": after}).encode()
            ).decode()
        items = [d["point"] if collection == "series" else clean(d) for d in docs[:limit]]
        return {"items": items, "next_cursor": next_cursor}
