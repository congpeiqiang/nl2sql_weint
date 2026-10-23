"""聊天工具 `_brief` 透出固化状态（T9）。"""


def test_brief_exposes_pin_status():
    from agent.tools.manage_scheduled_task import _brief

    out = _brief(
        {
            "id": "t",
            "name": "n",
            "pin_status": "invalid",
            "pin_invalid_reason": "columns_changed: missing=[b]",
        }
    )
    assert out["pin_status"] == "invalid"
    assert "columns_changed" in out["pin_invalid_reason"]


def test_brief_truncates_pin_invalid_reason():
    """失效原因可能带整段 SQL/列清单 ⇒ 与 `last_error` 同口径截到 200。"""
    from agent.tools.manage_scheduled_task import _brief

    out = _brief({"id": "t", "name": "n", "pin_invalid_reason": "x" * 500})
    assert len(out["pin_invalid_reason"]) == 200


def test_brief_pin_fields_default_empty():
    """老任务行（迁移前建的）没有这两列 ⇒ 空串，不是 None。"""
    from agent.tools.manage_scheduled_task import _brief

    out = _brief({"id": "t", "name": "n"})
    assert out["pin_status"] == ""
    assert out["pin_invalid_reason"] == ""
