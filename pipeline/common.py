"""一言片场 · 通用工具：JSON 容错解析、标点分句、时间轴格式化。"""

from __future__ import annotations

import json
import re
from typing import Any

PUNCT_END = "。！？；!?;…"
PUNCT_SOFT = "，、,：:—"


def strip_code_fence(text: str) -> str:
    """去掉模型可能加上的 ```json ... ``` 包裹。"""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


def extract_json(text: str) -> Any:
    """从模型输出里抠出第一个完整 JSON 对象/数组。

    大模型经常在 JSON 前后加解释文字；这里用括号配平扫描，比正则可靠。
    """
    t = strip_code_fence(text)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    start = None
    stack: list[str] = []
    in_str = False
    escape = False
    for i, ch in enumerate(t):
        if start is None:
            if ch in "{[":
                start = i
                stack.append(ch)
            continue
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if not stack:
                break
            stack.pop()
            if not stack:
                return json.loads(t[start : i + 1])
    raise ValueError("模型输出中找不到合法 JSON")


def split_sentences(text: str, min_len: int = 6) -> list[str]:
    """按中文句末标点切句，过短的句子并入上一句。"""
    text = re.sub(r"\s+", "", text or "")
    if not text:
        return []
    out: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in PUNCT_END:
            if out and len(buf) <= min_len:
                out[-1] += buf
            else:
                out.append(buf)
            buf = ""
    if buf:
        if out:
            out[-1] += buf
        else:
            out.append(buf)
    return out


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def hhmmss(seconds: float) -> str:
    seconds = max(0.0, seconds)
    m, s = divmod(int(round(seconds)), 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def srt_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def stable_hash(text: str) -> int:
    """与 Python 进程无关的稳定哈希（内置 hash 因 PYTHONHASHSEED 会变）。"""
    h = 2166136261
    for ch in text:
        h = (h ^ ord(ch)) * 16777619 & 0xFFFFFFFF
    return h


def fmt_seconds(seconds: float) -> str:
    return f"{seconds:.1f}s"
