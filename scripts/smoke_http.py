"""Valida um stack real já iniciado. Use --state para conferir após reinício."""

import argparse
import json
import time
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--state", type=Path, default=Path("/tmp/candlelab-smoke-state.json"))
    parser.add_argument("--verify-restart", action="store_true")
    args = parser.parse_args()
    with httpx.Client(base_url=args.url, timeout=15) as client:
        assert client.get("/health").status_code == 200
        if args.verify_restart:
            state = json.loads(args.state.read_text())
            run = client.get("/api/v1/runs/" + state["run_id"]).json()
            assert run["status"] == "completed" and run["summary"] == state["summary"]
            assert client.get("/api/v1/sessions/" + state["session_id"]).status_code == 200
            candles = client.get("/api/v1/datasets/" + state["dataset_id"] + "/candles").json()
            assert len(candles["items"]) == 12
            print("PASS: dados, sessão, execução e resumo preservados após reinício.")
            return
        fixture = Path(__file__).resolve().parents[1] / "tests/fixtures/btcusdt-5m.json"
        response = client.post(
            "/api/v1/datasets",
            data={"symbol": "BTCUSDT", "interval": "5m"},
            files={"file": ("fixture.json", fixture.read_bytes(), "application/json")},
        )
        assert response.status_code == 200, response.text
        dataset = response.json()
        response = client.post(
            "/api/v1/sessions",
            json={
                "name": "Smoke HTTP",
                "dataset_id": dataset["id"],
                "initial_cash": "1000",
                "fee_rate": "0.001",
            },
        )
        assert response.status_code == 201, response.text
        session_id = response.json()["id"]
        path = f"/api/v1/sessions/{session_id}/runs"
        response = client.post(
            path,
            json={
                "strategy_id": "SMA_CROSS@1",
                "parameters": {"short_window": 2, "long_window": 3},
            },
            headers={"Idempotency-Key": "http-smoke"},
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["id"]
        for _ in range(100):
            run = client.get("/api/v1/runs/" + run_id).json()
            if run["status"] in ("completed", "failed"):
                break
            time.sleep(0.1)
        assert run["status"] == "completed", run
        for kind in ("equity", "events", "indicators"):
            response = client.get(f"/api/v1/runs/{run_id}/series", params={"kind": kind})
            assert response.status_code == 200 and response.json()["items"], response.text
        assert client.get("/openapi.json").status_code == 200
        args.state.write_text(
            json.dumps(
                {
                    "dataset_id": dataset["id"],
                    "session_id": session_id,
                    "run_id": run_id,
                    "summary": run["summary"],
                },
                indent=2,
            )
        )
        print("PASS: upload, sessão, worker, polling e três séries por HTTP.")
        print(json.dumps(run["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
