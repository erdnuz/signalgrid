import json
from pathlib import Path
from typing import Any

import pytest

from signalgrid_frontend.store import DataStore

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"


@pytest.fixture
def contract():
    def load(name: str) -> Any:
        return json.loads((CONTRACTS / name).read_text())

    return load


@pytest.fixture
def store() -> DataStore:
    return DataStore(max_points=50)
