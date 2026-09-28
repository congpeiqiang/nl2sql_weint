"""出站 URL 的 SSRF 守卫（P3-8，2026-09-25 拍板实施）。

## 为什么要它

`POST /api/model-configs/test` 与 `/probe-capabilities` 的 `base_url` **可以由请求体直接给**，
服务端随后用 `urllib.request.urlopen` 去请求它（`api/model_config.py` 的三个 `_probe_*`），
并把 `message`/`models` **回显**给调用方 ⇒ 任何登录用户都能借服务端做内网探测。
而这两个端点只 `require_user`（模型配置自 P1 起是**按用户独立 store**，见
`model_config.py:_get_store`——每个用户配自己的模型是产品行为，不能靠"限管理员"解决）。

## 口径：只堵「服务器能到、而配置者本人到不了」的地址

| 范围 | 处置 | 为什么 |
|---|---|---|
| 环回 `127.0.0.0/8`、`::1`、`0.0.0.0`、`::` | **拒** | 容器自身的服务（`127.0.0.1:2026` 的 API、`:3010` 的 Langfuse、`:8080`）只有服务端自己到得了 ⇒ 这是唯一"真正新增"出来的探测能力 |
| 链路本地/云元数据 `169.254.0.0/16`、`fe80::/10` | **拒** | `169.254.169.254` 是云元数据端点（凭据泄漏面），没有任何正常用法指向它 |
| 私网 `10/8`、`172.16/12`、`192.168/16` | **放行** | ⚠️ **本仓的模型网关与数据库就在私网**（如 `http://192.168.25.13:8100/v1`）⇒ 拒私网 = 把正常功能一起拒掉。用户自己就能从本机访问这些地址，SSRF 在这里没有增量价值 |
| 其它（含公网） | 放行 | 出站到公网模型网关是本功能的正事 |

**常见错误修法**：把整组端点改成 `require_admin`（会打断「每人配自己的模型」），
或"拒绝私网/RFC1918"（会打断本项目自己的部署形态）。两条都**不要**。

## 为什么在**解析后**校验 IP

字符串校验（"host 里有没有 127.0.0.1"）能被 **DNS 重绑定**绕过：先让域名解析到公网、
再让它解析到 `127.0.0.1`。所以先 `getaddrinfo` 拿到全部 IP，逐个判。

⚠️ **如实记的残余**：本守卫是「先解析、再交给 `urlopen`（它会**自己再解析一次**）」，
所以**理论上**仍存在 TOCTOU 窗口。要彻底关掉得把连接钉在已校验的 IP 上（换 HTTP 客户端），
代价与收益不匹配 —— 它要求攻击者可控 DNS 且卡在两次解析之间。

## 本地开发

`NL2SQL_SSRF_ALLOW_LOOPBACK=1`（1/true/yes/on）放行环回与 `0.0.0.0`（本机跑 ollama/网关时用）。
**元数据地址仍然拒**（没有"本机开发需要 169.254.169.254"这回事）。
"""
from __future__ import annotations

import ipaddress
import logging
import os
import socket
from urllib.parse import urlsplit

_logger = logging.getLogger(__name__)

_ALLOW_LOOPBACK_ENV = "NL2SQL_SSRF_ALLOW_LOOPBACK"


class UnsafeOutboundURLError(ValueError):
    """出站目标被守卫拒绝（消息可直接回给用户）。"""


def _allow_loopback() -> bool:
    """`NL2SQL_SSRF_ALLOW_LOOPBACK` 为真值时放行环回（本机开发用）。"""
    return (os.environ.get(_ALLOW_LOOPBACK_ENV) or "").strip().lower() in ("1", "true", "yes", "on")


def blocked_reason(ip: str) -> str:
    """IP 被拒的原因；空串 = 放行。

    只判环回/未指定/链路本地（后两者不含私网，见模块 docstring 的口径表）。
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ""  # 不是 IP（理论上不会走到：调用方已解析过）
    # IPv4-mapped IPv6（::ffff:127.0.0.1）要按其内嵌的 v4 判，否则 is_loopback 为 False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped  # type: ignore[assignment]
    if addr.is_loopback or addr.is_unspecified:
        return "" if _allow_loopback() else "环回/未指定地址（本机自身服务）"
    if addr.is_link_local:
        return "链路本地/云元数据地址"
    return ""


def check_outbound_url(url: str, *, what: str = "目标地址") -> None:
    """校验出站 URL；不安全则抛 `UnsafeOutboundURLError`（消息可直接展示给用户）。

    - 只允许 http/https（挡 `file://` 之类的本地读取面）；
    - 有主机名就拿 `getaddrinfo` 解析出**所有** IP 逐个判（防 DNS 重绑定）；
    - **解析失败放行**（fail-open）：解析不了就连不上，`urlopen` 会给出更具体的报错，
      守卫的职责只是"别让危险目标通过"，不是"替 DNS 报错"。
    """
    raw = (url or "").strip()
    if not raw:
        raise UnsafeOutboundURLError(f"{what}为空")
    try:
        parts = urlsplit(raw)
    except ValueError as e:  # 非法 URL（如 host 里有空格）
        raise UnsafeOutboundURLError(f"{what}不是合法 URL：{e}") from e
    if parts.scheme not in ("http", "https"):
        raise UnsafeOutboundURLError(f"{what}只支持 http/https，收到：{parts.scheme or '(空)'}")
    host = parts.hostname
    if not host:
        raise UnsafeOutboundURLError(f"{what}缺少主机名")
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except OSError as e:
        _logger.debug("[net_guard] 解析 %s 失败（放行，交给 urlopen 报错）: %s", host, e)
        return
    for info in infos:
        ip = str(info[4][0])
        reason = blocked_reason(ip)
        if reason:
            raise UnsafeOutboundURLError(
                f"已拒绝请求：{what}（{host} → {ip}）属于{reason}；"
                f"若这是本机开发（如本机网关/ollama），设 {_ALLOW_LOOPBACK_ENV}=1 放行环回"
            )
