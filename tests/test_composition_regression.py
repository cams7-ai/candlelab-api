"""Golden digests captured from the pre-refactor engine (all existing strategies)."""

import hashlib
import json
import math
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path

import pytest

from test_tutorial import candles, run


def scenarios():
    return {
        "oscillation": (
            candles([round(100 + 8 * math.sin(i / 5) + i / 20, 4) for i in range(140)]),
            {},
        ),
        "trend": (
            [
                replace(c, open=c.open - D(".5"))
                for c in candles(list(range(100, 190)) + list(range(190, 95, -2)))
            ],
            {},
        ),
        "recovery": (
            candles([100] * 40 + [80, 85, 90, 95, 100, 110, 100, 85, 100] * 8),
            {"period": 5, "deviations": "1", "rsi_period": 2, "recovery": "20"},
        ),
        "flat_grid": (candles([100] * 90), {"atr_multiple": "0.1"}),
        "breakouts": (
            candles(
                [100] * 35 + [110, 111, 112, 108, 105, 100, 95, 100] * 10,
                [10] * 35 + [30, 10, 10, 20, 10, 30, 10, 20] * 10,
            ),
            {"lookback": 5, "volume_period": 3},
        ),
    }


def digest(strategy_id, case):
    from candlelab.domain.strategies import REGISTRY

    data, overrides = scenarios()[case]
    fields = REGISTRY[strategy_id].parameters.model_fields
    parameters = {key: value for key, value in overrides.items() if key in fields}
    result = run(strategy_id, data, parameters)
    return hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()


BASELINE = Path(__file__).parent / "fixtures/composition_baseline.json"


@pytest.mark.parametrize("key,expected", json.loads(BASELINE.read_text()).items())
def test_existing_execution_is_identical(key, expected):
    strategy_id, case = key.split("/")
    assert digest(strategy_id, case) == expected
