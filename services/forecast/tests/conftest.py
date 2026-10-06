import json
from pathlib import Path
from typing import Any

import pytest

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"


@pytest.fixture
def contract():
    def load(name: str) -> Any:
        return json.loads((CONTRACTS / name).read_text())

    return load
