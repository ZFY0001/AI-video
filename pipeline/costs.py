"""费用台账：只记录**真实发生过**的付费调用，金额一律标明是估价。

这个项目没有接入任何收费后端（云端出图/视频/TTS 都还是未启用适配器），
所以台账的意义不是"已经花了多少钱"，而是：

- 当适配器真的接入后，每一次成功调用都会落一条事件，而不是只在内存里加数；
- 服务重启、任务重跑之后，费用仍然可查；
- 预算闸能在提交前按估价拒绝明显超限的任务，而不是等花了钱才发现。

**不能把估价当账单**：单价是代码里的保守估算，供应商实际计费可能按分辨率、
时长、并发或套餐变化。所有对外字段都带 `estimated` 标记，界面也必须这么写。
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

#: 云端图生视频的保守估价（元/秒）。与 `pipeline.pipeline` 的护栏共用同一个数，
#: 避免"任务里按 0.5 算、看板上按别的数算"。
CLOUD_VIDEO_CNY_PER_SECOND = 0.5

LEDGER = "costs.jsonl"


def _ledger_path(run_dir: Path) -> Path:
    return run_dir / LEDGER


def record(
    run_dir: Path,
    *,
    kind: str,
    provider: str,
    amount_cny: float,
    units: float = 0.0,
    unit: str = "",
    rate_cny: float = 0.0,
    note: str = "",
) -> dict | None:
    """追加一条费用事件。金额 <= 0 不落账（免费/本地调用不该污染台账）。"""
    amount = round(max(0.0, float(amount_cny)), 4)
    if amount <= 0:
        return None
    event = {
        # 每次真实调用一个独立 id：同一镜头重跑是两次真实调用，不能因为 id 相同
        # 被去重成一次；只有读取时重复追加同一个事件才应该被去掉。
        "id": uuid.uuid4().hex,
        "ts": round(time.time(), 3),
        "kind": str(kind or "unknown"),
        "provider": str(provider or "unknown"),
        "units": round(float(units), 3),
        "unit": str(unit or ""),
        "rate_cny": round(float(rate_cny), 4),
        "amount_cny": amount,
        "estimated": True,
        "note": str(note or "")[:200],
    }
    path = _ledger_path(run_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        # 记账失败不应让已经成功的生成任务失败：产物已经产出，费用仍在供应商侧。
        return None
    return event


def read_events(run_dir: Path) -> list[dict]:
    """读取台账。坏行跳过，重复事件按 id 去重。"""
    path = _ledger_path(run_dir)
    if not path.is_file():
        return []
    events: dict[str, dict] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if not isinstance(item, dict):
            continue
        event_id = str(item.get("id") or "")
        if not event_id:
            continue
        events[event_id] = item
    return list(events.values())


def summarize(events: list[dict]) -> dict:
    total = round(sum(float(item.get("amount_cny") or 0) for item in events), 4)
    by_provider: dict[str, float] = {}
    for item in events:
        key = str(item.get("provider") or "unknown")
        by_provider[key] = round(by_provider.get(key, 0.0) + float(item.get("amount_cny") or 0), 4)
    return {
        "events": len(events),
        "total_cny": total,
        "by_provider": by_provider,
        "estimated": True,
    }


def cloud_video_state(settings) -> dict:
    """云端视频适配器的真实可用性与估价参数。未配置/未接入都如实说。"""
    from .video import CloudVideoProvider

    provider = CloudVideoProvider(
        base_url=settings.get("VIDEO_API_BASE_URL"),
        api_key=settings.get("VIDEO_API_KEY"),
    )
    ok, why = provider.available()
    return {
        "available": ok,
        "detail": why,
        "rate_cny": CLOUD_VIDEO_CNY_PER_SECOND,
        "configured": bool(settings.get("VIDEO_API_BASE_URL") or settings.get("VIDEO_API_KEY")),
    }


def estimate_video(duration: float, settings) -> dict:
    """按整片时长估算一次云端视频拍摄的费用与预算状态。

    只在**云端视频真正可用**时才有意义：未配置/未接入时估价为 0，
    不能拿一个假想单价去阻止本机免费出片。
    """
    state = cloud_video_state(settings)
    budget = settings.get_float("BUDGET_CNY", 20.0)
    estimate = round(max(0.0, float(duration)) * CLOUD_VIDEO_CNY_PER_SECOND, 2)
    return {
        **state,
        "duration": round(max(0.0, float(duration)), 2),
        "estimate_cny": estimate if state["available"] else 0.0,
        "budget_cny": round(budget, 2),
        "exceeded": bool(state["available"] and estimate > budget),
        "estimated": True,
    }


def budget_message(duration: float, settings) -> str:
    est = estimate_video(duration, settings)
    return (
        f"云端视频按 {est['rate_cny']} 元/秒保守估价，{est['duration']:.0f} 秒约 "
        f"{est['estimate_cny']:.2f} 元，超过单次预算 {est['budget_cny']:.2f} 元，已拒绝提交。"
        "可以调低时长、提高 BUDGET_CNY，或停用云端视频配置改用本地出片。"
        "这是估价，不是供应商账单。"
    )
