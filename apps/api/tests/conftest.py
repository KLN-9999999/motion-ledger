from pathlib import Path
import json
import pytest
from motionledger.schemas.procedure import ProcedureSpec

@pytest.fixture

def procedure():
    path=Path(__file__).resolve().parents[3]/"samples"/"procedures"/"flange-assembly.json"
    return ProcedureSpec.model_validate(json.loads(path.read_text(encoding="utf-8")))
