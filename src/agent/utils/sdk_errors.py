"""langgraph SDK 异常的语义判定。

按**类名**认（`langgraph_sdk.errors.NotFoundError`）而不是 import 那个类：
SDK 版本差异/测试替身都不必跟着改，而且 HTTP 404 在多种封装下同名。
"""


def is_thread_gone(exc: BaseException) -> bool:
    """异常是不是「线程已不存在」（langgraph `DELETE /threads/{id}` 之后的事）。

    调用方要分场景：sync watcher 连续命中就收工；调度器 reconcile 命中就立刻
    收口（否则 `running_thread_id` 挂着，后续定时轮次全被跳过）。
    """
    return any(cls.__name__ == "NotFoundError" for cls in type(exc).__mro__)
