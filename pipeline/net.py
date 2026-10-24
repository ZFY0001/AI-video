# -*- coding: utf-8 -*-
"""统一的外部调用出口：**本机地址必须绕过系统代理**。

为什么需要这个模块（实测踩过的坑）
------------------------------------
Windows 上开了系统代理时（注册表 ``Internet Settings`` 的 ``ProxyEnable=1``、
``ProxyServer=http://127.0.0.1:12450``，且 ``ProxyOverride`` 为空），
``urllib.request.urlopen`` 会把**对 127.0.0.1 的请求也发给代理**，
代理回一个 502。表现是：

    出图 comfy  ○ 未响应（HTTPError）

用户看到这句话会去反复重启 ComfyUI、查工作流模板、查显存——**而真正的原因是
代理把本地请求劫持了**。实测证据（同一台机器、同一个 URL）：

    走系统代理      -> HTTPError 502        ← 有东西替你响应了（代理）
    设 NO_PROXY 后  -> URLError 10061 拒绝连接  ← 直连，才反映出真实状态

受影响的**不止 ComfyUI**：任何 ``LLM_BASE_URL=http://127.0.0.1:...``
（Ollama / vLLM / llama.cpp）都会中同一个坑。

策略
----
- **本机地址**（127.0.0.0/8、localhost、::1、0.0.0.0）：用不带代理的 opener 直连
- **其它地址**（云端 API）：走系统默认，保留代理（那些确实需要代理才能出去）
"""
from __future__ import annotations

import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

#: 视为"本机"的主机名
LOOPBACK_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "::1", "0.0.0.0"}

#: 不带任何代理的 opener。只用于本机地址。
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def host_of(target: Any) -> str:
    """从 URL 字符串或 ``urllib.request.Request`` 里取出主机名（小写，无端口）。"""
    url = getattr(target, "full_url", None) or str(target)
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def is_loopback(target: Any) -> bool:
    """这个目标是不是本机地址。"""
    host = host_of(target)
    if not host:
        return False
    if host in LOOPBACK_NAMES:
        return True
    if host.startswith("127."):
        return True
    # IPv6 回环可能写成 [::1] 已被 urlsplit 去掉方括号
    return host == "::1"


def open_url(target: Any, timeout: float = 30) -> Any:
    """``urlopen``，但对本机地址**绕过系统代理**。用法与 urlopen 完全一致。"""
    if is_loopback(target):
        return _NO_PROXY_OPENER.open(target, timeout=timeout)
    return urllib.request.urlopen(target, timeout=timeout)  # noqa: S310


def system_proxy() -> str:
    """当前生效的系统代理（没有就返回空串）。"""
    try:
        p = urllib.request.getproxies()
    except Exception:  # noqa: BLE001
        return ""
    return p.get("http") or p.get("https") or ""


def describe_net_error(url: str, exc: BaseException) -> str:
    """把网络异常翻译成**能直接指导排查**的一句话。

    重点是把"本地请求被代理劫持"和"服务真的没起"分开——
    这两种情况的 ``HTTPError`` / ``URLError`` 看起来很像，
    但一个要去关代理、一个要去启动服务。
    """
    loop = is_loopback(url)
    if isinstance(exc, urllib.error.HTTPError):
        px = system_proxy()
        if loop and px and exc.code in (400, 403, 407, 502, 503, 504):
            return (f"本机请求被系统代理劫持（代理 {px} 返回 HTTP {exc.code}）。"
                    f"启动服务后仍连不上就是这个原因：设 NO_PROXY=127.0.0.1,localhost "
                    f"或在系统设置里把 localhost 加入代理例外")
        return f"服务返回 HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, (ConnectionRefusedError, socket.gaierror)) or "refused" in str(reason).lower():
            return "连接被拒绝——服务没有在运行（或端口不对）"
        if "timed out" in str(reason).lower() or isinstance(reason, TimeoutError):
            # 别假设"服务在跑"：Windows 防火墙对回环端口是**静默丢包**，端口完全没监听
            # 也会走满超时、抛 TimeoutError 而不是 ConnectionRefused（实测 8188）。
            # 原来写"服务在跑但没响应"，会把人引去查显存，实际只是没启动。
            return ("连接超时（端口无响应）——可能是服务还没启动，"
                    "也可能是启动了但卡住不返回（如 ComfyUI 显存不足卡在 VAE 解码）；"
                    "先确认端口在监听（netstat -ano | findstr 8188）")
        return f"连接失败（{reason}）"
    if isinstance(exc, TimeoutError):
        return "请求超时"
    return f"未响应（{exc.__class__.__name__}：{exc}）"
