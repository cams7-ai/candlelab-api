import os
from pathlib import Path

import pytest

from conftest import upload
from candlelab.domain.candles import decode_json

DEFAULT = (
    Path(__file__).parent
    / "fixtures/candles-btcusdt-spot-5m_2026-08-19T0000Z_a_2026-08-22T0000Z.json"
)


def test_supplied_865_candles(client, store):
    path = Path(os.getenv("CANDLELAB_ACCEPTANCE_FILE", str(DEFAULT)))
    if not path.is_file():
        pytest.skip("Forneça o anexo via CANDLELAB_ACCEPTANCE_FILE ou tests/fixtures/.")
    response = upload(client, decode_json(path.read_bytes()))
    assert response.status_code == 200, response.text
    doc = response.json()
    assert doc["candle_count"] == 865
    assert doc["first_open_time_ms"] == 1787097600000
    assert doc["last_open_time_ms"] == 1787356800000
    last = list(store.candles(doc["id"]))[-1]
    assert last.close_time_ms == 1787357099999
    assert upload(client, decode_json(path.read_bytes()), interval="1m").status_code == 422
    assert store.db.datasets.count_documents({"status": "published"}) == 1
