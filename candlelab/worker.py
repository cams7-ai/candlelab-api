import logging
import signal
import threading
from decimal import Decimal

from pymongo.errors import PyMongoError

from candlelab.config import Settings
from candlelab.domain.engine import simulate
from candlelab.domain.errors import AppError
from candlelab.infrastructure.store import MongoStore

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, store):
        self.store = store

    def once(self):
        job = self.store.claim()
        if not job:
            return False
        stopped = threading.Event()
        lost = threading.Event()

        def heartbeat():
            while not stopped.wait(self.store.settings.lease_seconds / 3):
                try:
                    self.store.renew(job)
                except (AppError, PyMongoError):
                    lost.set()
                    return

        def check():
            if lost.is_set():
                raise AppError("LEASE_LOST", "Posse da tarefa perdida.", status=409)

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            session = job["session_snapshot"]
            result = simulate(
                self.store.candles(session["dataset_id"]),
                job["strategy_id"],
                job["parameters"],
                Decimal(session["initial_cash"]),
                Decimal(session["fee_rate"]),
                Decimal(job["policy"]["quantity_step"]),
                check,
            )
            check()
            self.store.save_results(job, result)
        except AppError as exc:
            if exc.code != "LEASE_LOST":
                self.store.fail(job, exc.body())
        except Exception:
            logger.exception("Falha na execução %s", job["id"])
            # Conditional fencing prevents an old attempt from failing a newer task.
            try:
                self.store.fail(
                    job,
                    {
                        "error": {
                            "code": "RUN_FAILED",
                            "message": "Falha ao processar execução. Consulte os logs do worker.",
                            "details": [],
                        }
                    },
                )
            except PyMongoError:
                logger.exception(
                    "MongoDB indisponível; tarefa será recuperada após expirar a posse."
                )
        finally:
            stopped.set()
            thread.join(timeout=2)
        return True


def main():
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    store = MongoStore(settings)
    store.indexes()
    worker = Worker(store)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    while not stop.is_set():
        try:
            processed = worker.once()
            if not processed:
                stop.wait(settings.poll_seconds)
        except PyMongoError:
            logger.exception("MongoDB indisponível; nova tentativa em breve.")
            stop.wait(settings.poll_seconds)
    store.client.close()


if __name__ == "__main__":
    main()
