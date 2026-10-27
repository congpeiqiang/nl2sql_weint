"""默认数据库（is_default）单测 —— 方案①「空值跟随默认库/首个库」的后端一半。

背景（2026-10-23 用户拍板）：前端数据库下拉此前只在 localStorage 里记住用户
最后一次选择；从没选过（或换了浏览器）时 db_name=""，`tool_filter` 不裁剪 ⇒
模型同时看到所有库的 wrenai 工具。现在要求：管理员可**显式指定**一个默认库，
前端在「没有本地选择 / 本地选择已不可见」时回落到 默认库 → 可见列表第一个。

本文件锁三件事：
1. 单默认不变量 —— 置某库为默认时清掉其它库的默认标（不允许两个默认）；
2. 非默认写入不清默认 —— 编辑另一个库不能把默认库弄丢；
3. 缺键不倒灌 —— 老客户端（请求体里没有 is_default）编辑库时**保留**原值，
   否则每次保存连接信息都会静默取消默认。
"""
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcp_server.db_mcp_server.db.core.db_config_store import DBConfig, DbConfigStore


def _cfg(name: str, **kw) -> DBConfig:
    return DBConfig(
        name=name,
        db_type="mysql",
        host="h",
        port=3306,
        database=name,
        user="u",
        **kw,
    )


def _store(tmp_path) -> DbConfigStore:
    return DbConfigStore(path=str(tmp_path / "db_config.json"), secret="test-secret")


def _defaults(store: DbConfigStore) -> list[str]:
    return [d["name"] for d in store.list_configs(masked=True) if d.get("is_default")]


class TestStoreDefault:
    def test_upsert_default_clears_others(self, tmp_path):
        s = _store(tmp_path)
        s.upsert(_cfg("A", is_default=True))
        s.upsert(_cfg("B", is_default=True))
        assert _defaults(s) == ["B"]

    def test_upsert_non_default_keeps_existing_default(self, tmp_path):
        s = _store(tmp_path)
        s.upsert(_cfg("A", is_default=True))
        s.upsert(_cfg("B"))
        assert _defaults(s) == ["A"]

    def test_unset_default_leaves_none(self, tmp_path):
        s = _store(tmp_path)
        s.upsert(_cfg("A", is_default=True))
        s.upsert(_cfg("A", is_default=False))
        assert _defaults(s) == []

    def test_legacy_entry_without_key_is_not_default(self, tmp_path):
        s = _store(tmp_path)
        # 旧格式落盘（无 is_default 键）：升级后必须读成 False，而不是炸/漏字段
        s._write_raw(
            {
                "version": 1,
                "databases": [
                    {
                        "name": "old",
                        "db_type": "mysql",
                        "host": "h",
                        "port": 3306,
                        "database": "old",
                        "user": "u",
                        "password": "",
                    }
                ],
            }
        )
        rows = s.list_configs(masked=True)
        assert rows[0]["is_default"] is False
        assert _defaults(s) == []


class _InjectUser:
    """裸 Starlette app 上加 request.state.user（跳过认证中间件，测的是路由逻辑）。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope.setdefault("state", {})["user"] = {"user_id": "root", "is_admin": True}
        await self.app(scope, receive, send)


@pytest.fixture
def api(tmp_path, monkeypatch):
    """临时 store + 仅挂 db_config 路由的 app（不起 lifespan / MCP 子进程）。"""
    import api.db_config as db_api
    import mcp_server.db_mcp_server.db.core.db_config_store as store_mod

    store = _store(tmp_path)
    monkeypatch.setattr(db_api, "get_store", lambda: store)
    # grants._all_configured_dbs 在函数内 import get_store ⇒ 也得指向临时 store，
    # 否则 admin 的 visible_dbs 会读真实工作区配置、把测试库全过滤掉
    monkeypatch.setattr(store_mod, "get_store", lambda: store)

    async def _fake_ensure(name, force=True):
        return {"db_name": name, "status": "ok"}

    monkeypatch.setattr(db_api, "_ensure_mcp_tools", _fake_ensure)

    app = Starlette(routes=db_api.routes)
    app.add_middleware(_InjectUser)
    with TestClient(app) as c:
        yield c, store


def _payload(name: str, **extra) -> dict:
    body = {"name": name, "db_type": "mysql", "host": "h", "port": 3306, "database": name, "user": "u"}
    body.update(extra)
    return body


class TestApiIsDefault:
    def test_list_exposes_is_default(self, api):
        client, _ = api
        assert client.post("/api/db-configs", json=_payload("A", is_default=True)).status_code == 200
        items = client.get("/api/db-configs").json()["databases"]
        assert [d["name"] for d in items] == ["A"]
        assert items[0]["is_default"] is True

    def test_old_client_edit_keeps_default(self, api):
        """缺键 ≠ 取消默认：老前端保存连接信息不得静默清掉默认库。"""
        client, _ = api
        client.post("/api/db-configs", json=_payload("A", is_default=True))
        # 不带 is_default 的整条覆盖（老客户端行为）
        assert client.post("/api/db-configs", json=_payload("A", host="h2")).status_code == 200
        items = client.get("/api/db-configs").json()["databases"]
        assert items[0]["is_default"] is True
        assert items[0]["host"] == "h2"

    def test_old_client_new_db_does_not_steal_default(self, api):
        client, _ = api
        client.post("/api/db-configs", json=_payload("A", is_default=True))
        client.post("/api/db-configs", json=_payload("B"))  # 无 is_default 键的新库
        items = {d["name"]: d for d in client.get("/api/db-configs").json()["databases"]}
        assert items["A"]["is_default"] is True
        assert items["B"]["is_default"] is False

    def test_switch_default_through_api(self, api):
        client, _ = api
        client.post("/api/db-configs", json=_payload("A", is_default=True))
        client.post("/api/db-configs", json=_payload("B", is_default=True))
        items = {d["name"]: d for d in client.get("/api/db-configs").json()["databases"]}
        assert items["A"]["is_default"] is False
        assert items["B"]["is_default"] is True

    def test_explicit_false_unsets_default(self, api):
        client, _ = api
        client.post("/api/db-configs", json=_payload("A", is_default=True))
        assert client.post("/api/db-configs", json=_payload("A", is_default=False)).status_code == 200
        items = client.get("/api/db-configs").json()["databases"]
        assert items[0]["is_default"] is False
