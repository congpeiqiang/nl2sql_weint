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
def tmp_agent_data(tmp_path, monkeypatch):
    """Temporary AGENT_DATA_ROOT for test isolation."""
    data_root = tmp_path / "agent_data"
    data_root.mkdir()
    (data_root / "shared").mkdir()
    (data_root / "workspace").mkdir()
    (data_root / "auth").mkdir()
    os.environ["AGENT_DATA_ROOT"] = str(data_root)
    # 只设 env 不够：workspace_manager 的 data_root 在 **import 期冻结**（模块级
    # `_DATA_ROOT`/`_SHARED_RESOURCES_DIR`，`.env` 的真实 AGENT_DATA_ROOT 会被
    # load_dotenv 带进来），而 sqlite_paths.data_root() 优先走它 ⇒ 全量套件里任何
    # 更早 import 它的模块（如 test_api_smoke）都会让本 fixture 的 env 覆盖被静默
    # 忽略、store 逃逸到真实数据根（2026-10-21 实证：D:\nl2sql_data 被测试任务
    # 污染）。钉死解析函数，隔离与 import 顺序无关。
    import agent.utils.sqlite_paths as sqlite_paths

    monkeypatch.setattr(sqlite_paths, "data_root", lambda: data_root)
    yield data_root
    if "AGENT_DATA_ROOT" in os.environ:
        del os.environ["AGENT_DATA_ROOT"]
