"""生成器能力目录与费用看板。

F8 的边界必须说在前面：**没有接入任何新的收费供应商**。这里做的是接口底座：

- 把出图 / 绘制 / 视频 / 配音 / 文本后端的真实可用性、参考图能力、费用类型
  集中成一份可读目录；
- 未配置或适配器未接入的后端明确显示「不可用 / 未接入」，不显示成已就绪；
- 「测试」只做连通性探测，不生成收费样片；
- 费用台账记录真实成功的付费调用，金额一律标记为估价。

预算闸只对云端视频生效，而且只在适配器真的可用时生效——本机绘制、ComfyUI
和离线占位帧不该被一个假想单价挡住。
"""

from __future__ import annotations

import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query

from pipeline import costs
from pipeline.config import Settings, load_env_file, resource_dir
from pipeline.images import provider_status
from pipeline.tts import CloudTTSProvider, SAPIProvider, SilentProvider

from . import history
from .auth import (
    OwnershipUnavailable,
    Principal,
    optional_principal,
    run_owner_key,
)

router = APIRouter(prefix="/api")

COST_LABEL = {
    "free_local": "本机免费",
    "self_hosted": "自建（不计入台账）",
    "paid": "付费（按供应商账单）",
    "paid_unknown": "付费（价格未配置）",
}


def _settings() -> Settings:
    return Settings(values=load_env_file())


def _item(
    *,
    id: str,
    label: str,
    kind: str,
    available: bool,
    detail: str,
    cost: str,
    price_hint: str,
    configured: bool,
    reference_images: bool | None = None,
    reference_detail: str = "",
) -> dict:
    return {
        "id": id,
        "label": label,
        "kind": kind,
        "available": bool(available),
        "detail": detail,
        "reference_images": reference_images,
        "reference_detail": reference_detail,
        "cost": cost,
        "cost_label": COST_LABEL.get(cost, cost),
        "price_hint": price_hint,
        "configured": bool(configured),
        # 说清"测试"是什么：不是生成一张收费样片。
        "test": "仅探测可用性，不生成收费样片",
    }


def provider_catalog(fresh: bool = False) -> dict:
    settings = _settings()
    workflow_dir = resource_dir("workflows")
    items: list[dict] = []

    try:
        from pipeline import painted as painted_backend

        pst = painted_backend.check_env()
        items.append(_item(
            id="painted", label="代码绘制（p5.js）", kind="paint",
            available=pst.ok,
            detail=pst.detail if pst.ok else f"不可用：{pst.detail}",
            cost="free_local", price_hint="本机 Node + 浏览器，无 API 费用",
            configured=True, reference_images=False,
            reference_detail="按分镜脚本确定性绘制，不消费参考图",
        ))
    except Exception as exc:  # noqa: BLE001
        items.append(_item(
            id="painted", label="代码绘制（p5.js）", kind="paint",
            available=False, detail=f"模块加载失败：{exc}",
            cost="free_local", price_hint="本机 Node + 浏览器，无 API 费用",
            configured=True, reference_images=False,
        ))

    for status in provider_status(settings, workflow_dir, fresh=fresh):
        provider = status["provider"]
        if provider == "comfy":
            items.append(_item(
                id="comfy", label="ComfyUI 本地出图", kind="image",
                available=status["available"] == "是",
                detail=status["detail"],
                cost="self_hosted",
                price_hint="本机/局域网 GPU，电量与硬件不计入费用台账",
                configured=bool(settings.get("COMFYUI_BASE_URL")),
                reference_images=status.get("reference_images") == "是",
                reference_detail=status.get("reference_detail") or "",
            ))
        elif provider == "offline":
            items.append(_item(
                id="offline", label="离线占位帧（Pillow）", kind="image",
                available=status["available"] == "是",
                detail=status["detail"],
                cost="free_local", price_hint="本机绘制，不计费",
                configured=True,
                reference_images=status.get("reference_images") == "是",
                reference_detail=status.get("reference_detail") or "",
            ))
        elif provider == "cloud":
            items.append(_item(
                id="cloud-image", label="云端出图", kind="image",
                available=status["available"] == "是",
                detail=status["detail"],
                cost="paid", price_hint="未接入计费；接入后按供应商单价结算",
                configured=bool(settings.get("VIDEO_API_BASE_URL") or settings.get("VIDEO_API_KEY")),
                reference_images=status.get("reference_images") == "是",
                reference_detail=status.get("reference_detail") or "",
            ))

    video = costs.cloud_video_state(settings)
    items.append(_item(
        id="cloud-video", label="云端图生视频", kind="video",
        available=video["available"],
        detail=video["detail"],
        cost="paid",
        price_hint=f"估价 {video['rate_cny']} 元/秒；以供应商账单为准",
        configured=video["configured"],
        reference_images=True if video["available"] else False,
        reference_detail="适配器未接入时不承诺参考图能力",
    ))

    for provider in (SAPIProvider(), CloudTTSProvider(
        base_url=settings.get("TTS_BASE_URL"), api_key=settings.get("TTS_API_KEY")),
            SilentProvider()):
        ok, why = provider.available()
        is_cloud = provider.name == "cloud-tts"
        items.append(_item(
            id=f"tts-{provider.name}", label=f"配音 {provider.name}", kind="tts",
            available=ok, detail=why,
            cost="paid_unknown" if is_cloud else "free_local",
            price_hint="按供应商计费，价格未配置" if is_cloud else "本机合成，无 API 费用",
            configured=bool(settings.get("TTS_BASE_URL") or settings.get("TTS_API_KEY"))
            if is_cloud else True,
            reference_images=None,
        ))

    llm_configured = bool(settings.has("LLM_API_KEY"))
    items.append(_item(
        id="llm", label="文本模型（拆解/提示词）", kind="text",
        available=llm_configured,
        detail="已配置 Key" if llm_configured else "未配置 Key，拆解走离线规则创意",
        cost="paid_unknown" if llm_configured else "free_local",
        price_hint="按供应商计费，价格未配置" if llm_configured
        else "离线规则不产生 API 费用",
        configured=llm_configured,
        reference_images=None,
    ))

    ready = [item["id"] for item in items if item["available"]]
    return {
        "items": items,
        "ready": ready,
        "budget_cny": round(settings.get_float("BUDGET_CNY", 20.0), 2),
        "budget_note": "BUDGET_CNY 是单次运行的云端视频估价上限；不是账户余额",
        "estimated": True,
        "probed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def guard_budget(duration: float, settings: Settings | None = None, *,
                 painted: bool = False, preview: bool = False,
                 recompose: bool = False) -> None:
    """提交前预算闸。

    只对会走云端视频的整片拍摄生效：单镜重拍、只重合成、低清预览和代码绘制
    要么复用已有片段，要么根本不走视频 API，拿整片估价拦它们是误伤。
    """
    if painted or preview or recompose:
        return
    settings = settings or _settings()
    est = costs.estimate_video(duration, settings)
    if est["exceeded"]:
        raise HTTPException(status_code=422, detail=costs.budget_message(duration, settings))


def _run_theme(run_path: Path) -> str:
    import json

    try:
        brief = json.loads((run_path / "plan" / "brief.json").read_text(encoding="utf-8"))
        return str(brief.get("theme") or run_path.name)
    except (OSError, ValueError):
        return run_path.name


def usage(limit: int = 60, principal: Principal | None = None) -> dict:
    """归属当前主体的费用台账。

    只统计有主且属于当前主体的运行；无归属的旧数据不展示——费用看板不该
    成为"谁都能看别人跑了多少"的入口。
    """
    settings = _settings()
    budget = round(settings.get_float("BUDGET_CNY", 20.0), 2)
    if principal is None:
        return {
            "items": [], "total_cny": 0.0, "budget_cny": budget,
            "estimated": True, "scanned": 0,
            "note": "登录或访客凭据缺失，未查询费用台账",
        }
    size = max(1, min(int(limit), 200))
    try:
        runs = [p for p in history.RUNS.iterdir()
                if p.is_dir() and not p.name.startswith("_")]
    except OSError:
        runs = []
    runs.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    items: list[dict] = []
    scanned = 0
    for run_path in runs:
        try:
            owner = run_owner_key(run_path.name)
        except OwnershipUnavailable as exc:
            raise HTTPException(
                status_code=503, detail="归属数据库暂不可用，费用台账稍后再看"
            ) from exc
        if owner != principal.key:
            continue
        scanned += 1
        events = costs.read_events(run_path)
        if not events:
            continue
        summary = costs.summarize(events)
        items.append({
            "run_id": run_path.name,
            "theme": _run_theme(run_path),
            "created": time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(run_path.stat().st_mtime)
            ),
            "events": summary["events"],
            "total_cny": summary["total_cny"],
            "by_provider": summary["by_provider"],
            "over_budget": summary["total_cny"] > budget,
        })
        if len(items) >= size:
            break

    total = round(sum(item["total_cny"] for item in items), 4)
    return {
        "items": items,
        "total_cny": total,
        "budget_cny": budget,
        "estimated": True,
        "scanned": scanned,
        "note": (
            "金额为代码内单价的估价，不是供应商账单；"
            "本地绘制、ComfyUI 与离线占位帧不计费。"
        ),
    }


@router.get("/providers")
def read_providers(fresh: bool = Query(default=False)) -> dict:
    return provider_catalog(fresh=fresh)


@router.get("/usage")
def read_usage(limit: int = Query(default=60, ge=1, le=200),
               principal: Principal | None = Depends(optional_principal)) -> dict:
    return usage(limit=limit, principal=principal)
