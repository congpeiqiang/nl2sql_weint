"""Unit test fixtures."""
import os
import sys
from pathlib import Path

# Ensure src/ is in sys.path
_SRC = Path(__file__).resolve().parent.parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Test environment
os.environ.setdefault("DEPLOY_ENV", "test")
os.environ.setdefault("NL2SQL_AUTH_DISABLED", "1")

import pytest


@pytest.fixture
def tmp_agent_data(tmp_path):
    """Temporary AGENT_DATA_ROOT for test isolation."""
    data_root = tmp_path / "agent_data"
    data_root.mkdir()
    (data_root / "shared").mkdir()
    (data_root / "workspace").mkdir()
    (data_root / "auth").mkdir()
    os.environ["AGENT_DATA_ROOT"] = str(data_root)
    yield data_root
    if "AGENT_DATA_ROOT" in os.environ:
        del os.environ["AGENT_DATA_ROOT"]
