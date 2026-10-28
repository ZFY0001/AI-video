"""模型注册表：把「用哪家的哪个模型」变成配置，而不是改代码。

**为什么要这一层**：在此之前，云端出图和出片是写死智谱的
（`/images/generations`、`/videos/generations` 硬编码在 provider 里）。
换一家就要改 provider 类，等于把"业务选择"和"接口实现"焊死在一起。

这一层的职责只有一件事：**说清楚有什么可选、多少钱、有什么限制**。
真正的调用在 provider 里，按 `provider` 字段分支。

价格是**公开列表价**，用于给用户一个数量级判断，不是账单。
（2026-10-28 从智谱定价页与 MiniMax 公开价核对，换算汇率 7.1。）
"""
from __future__ import annotations

from typing import Any

#: 单价的单位说明：image 按「张」，video 按「秒」
PROVIDERS: dict[str, dict[str, Any]] = {
    "minimax": {
        "label": "MiniMax 海螺",
        "base_url": "https://api.minimax.chat",
        "key_env": "MINIMAX_API_KEY",
        "note": "国产，中文理解好；视频档位只到 768P（官方不支持 1080P）",
        "image": [
            {
                "id": "image-01",
                "label": "Image-01",
                "price_cny": 0.05,
                "note": "支持 aspect_ratio，同步返回 URL",
            },
            {
                "id": "image-01-live",
                "label": "Image-01 Live",
                "price_cny": 0.05,
                "note": "同上，风格偏插画",
            },
        ],
        "video": [
            {
                "id": "MiniMax-Hailuo-2.3-Fast",
                "label": "Hailuo 2.3 Fast（最便宜）",
                "price_cny": 0.32,
                "modes": ["i2v"],
                "max_res": "768P",
                "durations": [6, 10],
                "note": "只能图生视频——必须给首帧图；不支持文生视频",
            },
            {
                "id": "MiniMax-Hailuo-2.3",
                "label": "Hailuo 2.3",
                "price_cny": 0.33,
                "modes": ["t2v", "i2v"],
                "max_res": "768P",
                "durations": [6, 10],
                "note": "最便宜的「既支持文生也支持图生」",
            },
            {
                "id": "MiniMax-Hailuo-02",
                "label": "Hailuo 02",
                "price_cny": 0.68,
                "modes": ["t2v", "i2v"],
                "max_res": "768P",
                "durations": [6, 10],
                "note": "支持 512P 档，更便宜的画质换价",
            },
            {
                "id": "I2V-01-Director",
                "label": "I2V-01 Director（可下运镜指令）",
                "price_cny": 1.25,
                "modes": ["i2v"],
                "max_res": "720P",
                "durations": [6],
                "note": "prompt 里可写 [推进]/[拉远]/[跟随]/[固定] 控制运镜；只有 6 秒",
            },
            {
                "id": "T2V-01-Director",
                "label": "T2V-01 Director（文生 + 运镜）",
                "price_cny": 0.75,
                "modes": ["t2v"],
                "max_res": "720P",
                "durations": [6],
                "note": "纯文字也能指定运镜；只有 6 秒",
            },
        ],
    },
    "zhipu": {
        "label": "智谱 BigModel",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "key_env": "ZHIPU_API_KEY",
        "note": "按次计费，1080P，性价比最高；但视频不支持运镜指令",
        "image": [
            {
                "id": "cogview-4",
                "label": "CogView-4",
                "price_cny": 0.06,
                "note": "1440×720（16:9），中文提示词直接可用",
            },
        ],
        "video": [
            {
                "id": "cogvideox-2",
                "label": "CogVideoX-2（默认，最划算）",
                "price_cny": 0.10,
                "modes": ["t2v", "i2v"],
                "max_res": "1080P",
                "durations": [5, 10],
                "note": "0.5 元/次，实测输出 1720×1080",
            },
            {
                "id": "cogvideox-3",
                "label": "CogVideoX-3（更高质量）",
                "price_cny": 0.20,
                "modes": ["t2v", "i2v"],
                "max_res": "1080P",
                "durations": [5, 10],
                "note": "1 元/次，比 2 代贵一倍",
            },
        ],
    },
}

#: 默认：全部走 MiniMax 的最便宜档（用户 2026-10-28 明确要求）
DEFAULTS = {
    "IMAGE_PROVIDER": "minimax",
    "IMAGE_MODEL": "image-01",
    "VIDEO_PROVIDER": "minimax",
    "VIDEO_MODEL": "MiniMax-Hailuo-2.3-Fast",
}


def provider_ids() -> list[str]:
    return list(PROVIDERS)


def get_provider(name: str) -> dict[str, Any] | None:
    return PROVIDERS.get(str(name or "").strip().lower())


def models_of(kind: str, provider: str) -> list[dict[str, Any]]:
    """取某家的某个类别的模型清单。kind = image / video。"""
    p = get_provider(provider)
    if not p:
        return []
    return list(p.get(kind) or [])


def find_model(kind: str, provider: str, model_id: str) -> dict[str, Any] | None:
    for m in models_of(kind, provider):
        if m.get("id") == model_id:
            return m
    return None


def default_of(kind: str) -> tuple[str, str]:
    """返回 (provider, model_id) 默认值。"""
    if kind == "image":
        return DEFAULTS["IMAGE_PROVIDER"], DEFAULTS["IMAGE_MODEL"]
    return DEFAULTS["VIDEO_PROVIDER"], DEFAULTS["VIDEO_MODEL"]


def resolve(settings: dict[str, Any], kind: str) -> tuple[str, str, dict[str, Any]]:
    """从配置里解析出「实际要用哪家、哪个模型、它的元信息」。

    容错策略：配置里写了不存在的 provider/model 时不报错，而是**回落到默认**，
    并在返回的 meta 里标 `fallback=True`——出片过程中因为一个拼错的模型名
    直接崩掉，比降级严重得多。
    """
    prefix = "IMAGE" if kind == "image" else "VIDEO"
    provider = str(settings.get(f"{prefix}_PROVIDER") or "").strip().lower()
    model_id = str(settings.get(f"{prefix}_MODEL") or "").strip()
    fallback = False

    if not provider or not get_provider(provider):
        provider, model_id = default_of(kind)
        fallback = True
    meta = find_model(kind, provider, model_id)
    if meta is None:
        provider, model_id = default_of(kind)
        meta = find_model(kind, provider, model_id) or {}
        fallback = True
    out = dict(meta)
    out["provider"] = provider
    out["provider_label"] = (get_provider(provider) or {}).get("label", provider)
    out["fallback"] = fallback
    return provider, model_id, out


def catalog() -> dict[str, Any]:
    """给前端 / API 用的完整目录（含默认值）。"""
    return {
        "defaults": DEFAULTS,
        "providers": PROVIDERS,
    }


def price_note(kind: str, provider: str, model_id: str) -> str:
    """一句话人话描述成本，给界面直接显示。"""
    meta = find_model(kind, provider, model_id) or {}
    price = meta.get("price_cny")
    if price is None:
        return ""
    unit = "张" if kind == "image" else "秒"
    return f"约 {price} 元/{unit}"
