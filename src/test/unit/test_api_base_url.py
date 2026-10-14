"""`_api_base_url()` 的行为契约（Phase 1-3）。

Phase 1-3 把 agent 侧 4 处内联的 `os.getenv("LANGGRAPH_API_URL", "http://localhost:2026")`
统一到 `api/_common._api_base_url()`。两种写法**不等价**，本文件把统一后依赖的两条
语义钉住，防止将来有人「顺手简化」`_api_base_url()` 时把调用方改回旧行为：

1. **空串回落**——`LANGGRAPH_API_URL=""` 必须回落到默认值。旧的 `os.getenv(k, default)`
   写法在 env 存在但为空串时返回 `""`，调用方会拼出 `/threads/x/state` 这种无 scheme
   的相对 URL，httpx 直接 `UnsupportedProtocol`。
2. **去尾斜杠**——`http://host:2026/` 必须归一成 `http://host:2026`，否则拼出
   `http://host:2026//threads/x`。
"""
import pytest

from api._common import _api_base_url


def test_default_when_unset(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("LANGGRAPH_API_URL", raising=False)
    assert _api_base_url() == "http://localhost:2026"


def test_env_value_wins(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LANGGRAPH_API_URL", "http://langgraph-api:2026")
    assert _api_base_url() == "http://langgraph-api:2026"


def test_empty_string_falls_back(monkeypatch: pytest.MonkeyPatch):
    """空串必须回落——这是与旧 `os.getenv(k, default)` 写法的**唯一**行为差异。

    （注意：判据是 `or`，所以纯空白串 `"   "` **不会**回落——这是有意的，
    "空白串不是合法 URL" 由调用方/httpx 报错，不在这里悄悄兜底。）
    """
    monkeypatch.setenv("LANGGRAPH_API_URL", "")
    assert _api_base_url() == "http://localhost:2026"


@pytest.mark.parametrize("trailing", ["http://h:1/", "http://h:1//", "http://h:1///"])
def test_trailing_slashes_stripped(monkeypatch: pytest.MonkeyPatch, trailing: str):
    monkeypatch.setenv("LANGGRAPH_API_URL", trailing)
    assert _api_base_url() == "http://h:1"
