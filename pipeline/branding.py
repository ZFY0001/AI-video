"""品牌层：导出时叠加的水印与片头/片尾字卡。

为什么放在导出而不是拍摄：分镜画面、字幕、片段缓存都是按原片一次成型的，
品牌是"同一支片子投给不同账号"的最后一层包装。它必须是确定性的、可撤销的，
并且**不能改变时长与音轨**——一旦把片头片尾做成真正的前后片段，字幕时间轴、
人声、节拍全部要重排，代价与风险都远高于收益。

所以这里生成的是全画布透明 PNG，由 FFmpeg 在目标规格上叠加：

- 水印：logo + 品牌名，位于四角之一；
- 片头/片尾：顶部居中的一条半透明字卡，只在开头/结尾几秒出现。

文字与 logo 的位置直接烘焙进 PNG，导出侧的滤镜因此只有 `overlay=0:0`，
不需要在 FFmpeg 表达式里再做排版。PNG 按「品牌指纹 + 分辨率」缓存，品牌不变
就不会重复绘制。
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from PIL import Image, ImageDraw

from .image_render import FONT_CANDIDATES, load_font

#: 渲染规则版本。改了排版/字号/字体选择就加一，让所有旧缓存自动失效。
VERSION = 2

POSITIONS = ("bottom-right", "bottom-left", "top-right", "top-left")

DEFAULTS: dict = {
    "name": "",
    "intro_text": "",
    "outro_text": "",
    "position": "bottom-right",
    "opacity": 0.85,
    "intro_seconds": 2.0,
    "outro_seconds": 2.0,
    "logo": {},
    "version": 0,
    "updated": "",
}


def _text(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _float(value: object, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def normalize(raw: object) -> dict:
    """把任意来源的品牌配置收敛成固定结构。

    导出缓存指纹按这个结构计算；如果这里随请求原样透传，两个语义相同但字段
    顺序/多余键不同的配置会得到不同指纹，缓存永远命不中。
    """
    data = raw if isinstance(raw, dict) else {}
    logo = data.get("logo") if isinstance(data.get("logo"), dict) else {}
    position = str(data.get("position") or DEFAULTS["position"])
    return {
        "name": _text(data.get("name"), 40),
        "intro_text": _text(data.get("intro_text"), 60),
        "outro_text": _text(data.get("outro_text"), 60),
        "position": position if position in POSITIONS else DEFAULTS["position"],
        "opacity": round(_float(data.get("opacity"), 0.85, 0.1, 1.0), 2),
        "intro_seconds": round(_float(data.get("intro_seconds"), 2.0, 0.0, 8.0), 2),
        "outro_seconds": round(_float(data.get("outro_seconds"), 2.0, 0.0, 8.0), 2),
        "logo": {
            "asset": _text(logo.get("asset"), 200),
            "sha": _text(logo.get("sha"), 64),
            "width": int(logo.get("width") or 0),
            "height": int(logo.get("height") or 0),
            "name": _text(logo.get("name"), 80),
        },
        "version": max(0, int(data.get("version") or 0)),
        "updated": _text(data.get("updated"), 40),
    }


def is_active(brand: dict) -> bool:
    return bool(
        brand.get("name")
        or (brand.get("logo") or {}).get("asset")
        or brand.get("intro_text")
        or brand.get("outro_text")
    )


def font_fingerprint() -> str:
    """实际会用到的字体文件指纹。

    品牌字卡的宽度依赖具体字体；换了系统字体却不失效缓存，会留下一张
    按旧字体排版、已经溢出/留白失衡的叠加层。
    """
    for raw in FONT_CANDIDATES:
        path = Path(raw)
        if not path.is_file():
            continue
        try:
            stat = path.stat()
            return f"{path.name}:{stat.st_size}:{stat.st_mtime_ns}"
        except OSError:
            continue
    return "pillow-default"


def fingerprint(brand: dict, plan_dir: Path) -> str:
    """品牌指纹：品牌配置 + logo 内容 + 字体 + 渲染规则版本。

    只算配置不够：同一份配置换了 logo 图或系统字体，导出画面会变，缓存必须失效。
    """
    if not is_active(brand):
        return ""
    logo = brand.get("logo") or {}
    payload = {
        "version": VERSION,
        "font": font_fingerprint(),
        "brand": {
            key: brand.get(key)
            for key in ("name", "intro_text", "outro_text", "position",
                        "opacity", "intro_seconds", "outro_seconds")
        },
        "logo": logo.get("sha") or "",
    }
    # logo 指纹以文件内容为准：旧 meta 里可能没存 sha，但文件换了也要失效。
    path = logo_path(plan_dir, brand)
    if path is not None:
        try:
            payload["logo_file"] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            payload["logo_file"] = "missing"
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def logo_path(plan_dir: Path, brand: dict) -> Path | None:
    """品牌 logo 的落盘路径。只接受项目内相对路径，越界当作没有。"""
    rel = str((brand.get("logo") or {}).get("asset") or "").strip()
    if not rel:
        return None
    root = plan_dir.resolve()
    path = (root / rel).resolve()
    if path.is_file() and path.is_relative_to(root):
        return path
    return None


def _fit_font(draw: ImageDraw.ImageDraw, text: str, max_width: float,
              start: int, minimum: int = 16):
    size = max(minimum, int(start))
    while size > minimum:
        font = load_font(size)
        if draw.textlength(text, font=font) <= max_width:
            return font
        size -= 2
    return load_font(minimum)


def _watermark(brand: dict, plan_dir: Path, width: int, height: int) -> Image.Image:
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    alpha = brand["opacity"]
    name = brand["name"]

    logo: Image.Image | None = None
    path = logo_path(plan_dir, brand)
    if path is not None:
        try:
            with Image.open(path) as raw:
                logo = raw.convert("RGBA")
                logo.thumbnail(
                    (max(40, int(width * 0.14)), max(40, int(height * 0.10))),
                    Image.LANCZOS,
                )
        except (OSError, ValueError):
            logo = None

    margin = max(20, int(min(width, height) * 0.035))
    pad = max(10, int(min(width, height) * 0.012))
    gap = max(8, int(min(width, height) * 0.01))
    font = _fit_font(draw, name, width * 0.32, max(18, int(min(width, height) * 0.024))) if name else None
    name_w = draw.textlength(name, font=font) if font and name else 0
    name_h = font.size if font and name else 0
    logo_w = logo.width if logo else 0
    logo_h = logo.height if logo else 0

    group_w = logo_w + (gap if logo and name else 0) + int(name_w)
    group_h = max(logo_h, name_h)
    box_w = group_w + pad * 2
    box_h = group_h + pad * 2
    if box_w <= 0 or box_h <= 0:
        return canvas

    left = width - margin - box_w if brand["position"].endswith("right") else margin
    top = margin if brand["position"].startswith("top") else height - margin - box_h
    draw.rounded_rectangle(
        [left, top, left + box_w, top + box_h],
        radius=max(8, int(box_h * 0.22)),
        fill=(0, 0, 0, int(120 * alpha)),
        outline=(255, 255, 255, int(45 * alpha)),
        width=max(1, int(min(width, height) * 0.0015)),
    )
    x = left + pad
    y = top + (box_h - logo_h) // 2 if logo else top + pad
    if logo is not None:
        canvas.alpha_composite(logo, (x, y))
        x += logo_w + gap
    if font is not None and name:
        ty = top + (box_h - name_h) // 2
        draw.text((x, ty), name, font=font, fill=(255, 255, 255, int(255 * alpha)))
    return canvas


def _title_card(text: str, width: int, height: int, alpha: float) -> Image.Image:
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    if not text:
        return canvas
    draw = ImageDraw.Draw(canvas)
    font = _fit_font(draw, text, width * 0.72, max(22, int(min(width, height) * 0.038)))
    text_w = draw.textlength(text, font=font)
    bbox = draw.textbbox((0, 0), text, font=font)
    text_h = bbox[3] - bbox[1]
    pad_x = max(18, int(width * 0.025))
    pad_y = max(10, int(height * 0.012))
    box_w = min(width * 0.86, text_w + pad_x * 2)
    box_h = text_h + pad_y * 2
    left = (width - box_w) / 2
    # 顶部而不是底部：底部是字幕/人物主体的常用位置，片头字卡压上去会挡内容。
    top = max(margin_of(height), height * 0.06)
    draw.rounded_rectangle(
        [left, top, left + box_w, top + box_h],
        radius=max(10, int(box_h * 0.3)),
        fill=(0, 0, 0, int(135 * alpha)),
        outline=(255, 255, 255, int(50 * alpha)),
        width=max(1, int(min(width, height) * 0.0015)),
    )
    draw.text(
        ((width - text_w) / 2, top + pad_y),
        text, font=font, fill=(255, 255, 255, int(255 * alpha)),
    )
    return canvas


def margin_of(height: int) -> int:
    return max(16, int(height * 0.012))


def build_layers(brand: dict, plan_dir: Path, width: int, height: int,
                 cache_dir: Path, brand_fp: str) -> list[tuple[str, Path]]:
    """生成（或复用）全画布透明叠加层，返回 [(kind, png)]。

    kind 用于导出侧决定出现时段：watermark 全程，intro 开头，outro 结尾。
    """
    if not brand_fp or not is_active(brand):
        return []
    cache_dir.mkdir(parents=True, exist_ok=True)
    jobs: list[tuple[str, Image.Image]] = []
    if brand.get("name") or (brand.get("logo") or {}).get("asset"):
        jobs.append(("watermark", _watermark(brand, plan_dir, width, height)))
    if brand.get("intro_text") and brand.get("intro_seconds", 0) > 0:
        jobs.append(("intro", _title_card(brand["intro_text"], width, height, brand["opacity"])))
    if brand.get("outro_text") and brand.get("outro_seconds", 0) > 0:
        jobs.append(("outro", _title_card(brand["outro_text"], width, height, brand["opacity"])))

    layers: list[tuple[str, Path]] = []
    for kind, image in jobs:
        path = cache_dir / f"{brand_fp}-{width}x{height}-{kind}.png"
        if not path.is_file():
            image.save(path, "PNG")
        layers.append((kind, path))
    return layers


def default_brand() -> dict:
    brand = normalize({})
    brand["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return brand
