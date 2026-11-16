"""字幕样式模板：6 套预置、三画幅安全区排版、缺字回退检测。

样式只决定最终叠加的整句字幕（字幕卡 / 绘制后端的整句字幕），不进入片段
缓存键：换样式只重合成，不重出图。逐字高亮暂未实现——它需要逐字时间轴，
而当前只有整句字幕轨（见剪映对标方案的 C1/C2 边界说明）。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, ImageDraw, ImageFont

DEFAULT_STYLE_ID = "clean"

#: 字体族 → 候选文件。Windows 自带字体优先，Linux 上退到文泉驿（没有则用
#: Pillow 默认字体，缺字会由 coverage() 明确报出来，不静默变方块）。
FONT_FAMILIES: dict[str, tuple[str, ...]] = {
    "regular": (
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\Deng.ttf",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    ),
    "bold": (
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    ),
    "hei": (
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\msyhbd.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    ),
    "deng": (
        r"C:\Windows\Fonts\Deng.ttf",
        r"C:\Windows\Fonts\msyh.ttc",
    ),
    "song": (
        r"C:\Windows\Fonts\simsun.ttc",
        r"C:\Windows\Fonts\msyh.ttc",
    ),
}

#: 首选字体缺字时的回退顺序。粗体不一定覆盖更全，所以把常规体排在前面。
FALLBACK_FAMILIES = ("regular", "bold", "hei", "deng", "song")


@dataclass(frozen=True)
class SubtitleStyle:
    """一套整句字幕样式。scale 是相对画面高度的字号比例，跨画幅自动缩放。"""

    id: str
    label: str
    note: str
    family: str = "regular"
    scale: float = 0.041
    color: str = "#FFFFFF"
    stroke: str = "#000000"
    #: 描边宽 / 字号。0 = 不描边。
    stroke_ratio: float = 0.10
    #: 阴影透明度 0-255；0 = 不画阴影。
    shadow: int = 170
    #: 底条透明度 0-255；0 = 不画底条。
    box: int = 90
    box_color: str = "#000000"
    #: bottom = 贴底；low = 略高（给底部压边/平台 UI 留位置）。
    position: str = "bottom"
    max_lines: int = 2
    #: 行高 / 字号。
    line_gap: float = 1.34


_PRESETS: tuple[SubtitleStyle, ...] = (
    SubtitleStyle(
        "clean", "清晰白",
        "白色常规字 + 半透明黑底，默认，任何背景都读得清",
        family="regular", scale=0.041, color="#FFFFFF", stroke="#000000",
        stroke_ratio=0.10, shadow=170, box=90,
    ),
    SubtitleStyle(
        "warm", "暖橙",
        "暖白字 + 深棕描边、深棕底条，适合美食/烟火题材",
        family="regular", scale=0.041, color="#FFE1B8", stroke="#3A1C0A",
        stroke_ratio=0.12, shadow=160, box=70, box_color="#2A1406",
    ),
    SubtitleStyle(
        "cool", "冷蓝",
        "冷白字 + 深蓝描边、深蓝底条，适合科技/夜景题材",
        family="regular", scale=0.040, color="#E4F4FF", stroke="#071E33",
        stroke_ratio=0.12, shadow=160, box=80, box_color="#04121F",
    ),
    SubtitleStyle(
        "film", "胶片",
        "米白宋体 + 细描边、无底条，位置略高，适合慢节奏叙事",
        family="song", scale=0.040, color="#F6EEDC", stroke="#16110B",
        stroke_ratio=0.08, shadow=190, box=0, position="low",
    ),
    SubtitleStyle(
        "ink", "墨白",
        "黑体白字 + 粗描边、无底条，适合水墨/国风",
        family="hei", scale=0.042, color="#FFFFFF", stroke="#000000",
        stroke_ratio=0.16, shadow=0, box=0,
    ),
    SubtitleStyle(
        "vivid", "醒目黄",
        "亮黄粗体 + 粗黑描边、深色底条，信息流高对比",
        family="bold", scale=0.043, color="#FFE92E", stroke="#101010",
        stroke_ratio=0.15, shadow=200, box=110, box_color="#000000",
    ),
)

STYLES: dict[str, SubtitleStyle] = {style.id: style for style in _PRESETS}


def style_of(style_id: str | None) -> SubtitleStyle:
    """按 id 取样式；未知 id 抛 KeyError，由调用方决定是 422 还是回退。"""
    key = str(style_id or "").strip()
    if key not in STYLES:
        raise KeyError(key or DEFAULT_STYLE_ID)
    return STYLES[key]


def resolve_style(style_id: str | None) -> SubtitleStyle:
    """管线内部用：未知 id 回退默认样式，不让一条坏配置拖垮整次拍摄。"""
    try:
        return style_of(style_id)
    except KeyError:
        return STYLES[DEFAULT_STYLE_ID]


def style_catalog() -> list[dict[str, Any]]:
    """给界面用的样式清单。颜色是 CSS 十六进制，前端可直接画色块。"""
    return [
        {
            "id": style.id,
            "label": style.label,
            "note": style.note,
            "color": style.color,
            "stroke": style.stroke,
            "box": style.box > 0,
            "position": style.position,
        }
        for style in _PRESETS
    ]


def style_payload(style: SubtitleStyle, version: int = 0) -> dict[str, Any]:
    """落盘/接口返回用的完整样式描述。"""
    payload = asdict(style)
    payload["version"] = max(0, int(version))
    return payload


# ------------------------------------------------------------------ 字体与缺字
def _font_path(family: str) -> str:
    for candidate in FONT_FAMILIES.get(family, ()):
        if Path(candidate).is_file():
            return candidate
    for fallback in FALLBACK_FAMILIES:
        for candidate in FONT_FAMILIES.get(fallback, ()):
            if Path(candidate).is_file():
                return candidate
    return ""


@lru_cache(maxsize=128)
def _load(size: int, family: str) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    path = _font_path(family)
    if path:
        try:
            return ImageFont.truetype(path, max(8, int(size)))
        except OSError:
            pass
    return ImageFont.load_default()


@lru_cache(maxsize=128)
def _notdef(path: str) -> bytes:
    font = ImageFont.truetype(path, 24)
    image = Image.new("L", (72, 72), 0)
    ImageDraw.Draw(image).text((12, 12), "\uffff", font=font, fill=255)
    return image.tobytes()


@lru_cache(maxsize=8192)
def _supported(path: str, char: str) -> bool:
    """字体是否真有这个字的字形。

    Pillow 不暴露 cmap，所以把字画到 72×72 画布上和 U+FFFF（必然缺失）的
    渲染结果逐字节比较：一样就是 .notdef（方块/空白），不一样才算有字形。
    """
    if not path:
        return False
    if char.isspace():
        return True
    try:
        font = ImageFont.truetype(path, 24)
        missing = _notdef(path)
    except OSError:
        return False
    image = Image.new("L", (72, 72), 0)
    ImageDraw.Draw(image).text((12, 12), char, font=font, fill=255)
    return image.tobytes() != missing


def _coverage_for(path: str, chars: Iterable[str]) -> list[str]:
    if not path:
        return list(chars)
    return [char for char in chars if not _supported(path, char)]


def coverage(texts: Iterable[str], style: SubtitleStyle) -> dict[str, Any]:
    """检查样式字体能否覆盖这些字幕文字，并给出可用的回退字体。

    返回的 ``fallback`` 是**整个文本集都能覆盖**的回退字体文件名；没有就为空，
    同时 ``uncovered`` 列出连回退也缺的字（渲染时只能明确告警，不假装正常）。
    """
    chars = sorted({char for text in texts for char in str(text or "") if not char.isspace()})
    if not chars:
        return {"ok": True, "missing": [], "missing_count": 0,
                "fallback": "", "uncovered": []}
    path = _font_path(style.family)
    missing = _coverage_for(path, chars)
    if not missing:
        return {"ok": True, "missing": [], "missing_count": 0,
                "fallback": "", "uncovered": []}
    fallback = ""
    for family in FALLBACK_FAMILIES:
        candidate = _font_path(family)
        if candidate and candidate != path and not _coverage_for(candidate, chars):
            fallback = candidate
            break
    uncovered: list[str] = []
    if not fallback:
        best = min(
            (_font_path(family) for family in FALLBACK_FAMILIES),
            key=lambda p: len(_coverage_for(p, chars)),
            default="",
        )
        uncovered = _coverage_for(best, chars) if best else list(chars)
        fallback = best
    return {
        "ok": False,
        "missing": missing[:24],
        "missing_count": len(missing),
        "fallback": Path(fallback).name if fallback else "",
        "uncovered": uncovered[:24],
    }


# ------------------------------------------------------------------ 排版与绘制
def _rgba(color: str, alpha: int) -> tuple[int, int, int, int]:
    value = str(color or "#000000").lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    try:
        r, g, b = (int(value[i:i + 2], 16) for i in (0, 2, 4))
    except (ValueError, IndexError):
        r, g, b = 0, 0, 0
    return (r, g, b, max(0, min(255, int(alpha))))


def safe_box(width: int, height: int, style: SubtitleStyle) -> tuple[float, float, float, float]:
    """字幕安全区（像素）。与 render 时的 ``--guides`` 蓝框一致。"""
    if style.position == "low":
        return (width * 0.05, height * 0.74, width * 0.95, height * 0.93)
    return (width * 0.05, height * 0.78, width * 0.95, height * 0.95)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: Any, max_width: float) -> list[str]:
    """按像素宽度逐字换行。中文没有词边界，逐字是最稳的策略。"""
    lines: list[str] = []
    current = ""
    for char in str(text or "").strip():
        if char == "\n":
            if current:
                lines.append(current)
                current = ""
            continue
        trial = current + char
        if current and draw.textlength(trial, font=font) > max_width:
            lines.append(current)
            current = char
        else:
            current = trial
    if current:
        lines.append(current)
    return lines or [""]


def layout(text: str, width: int, height: int, style: SubtitleStyle,
           family: str | None = None) -> dict[str, Any]:
    """计算字号、换行与每行包围盒（含底条留白），不真正画像素。"""
    from PIL import Image as _Image

    scratch = ImageDraw.Draw(_Image.new("RGBA", (8, 8), (0, 0, 0, 0)))
    # 两侧各留 7%：底条左右各再占 1%，还要容纳描边，0.9W 的字宽会压到安全区外。
    max_width = width * 0.86
    size = max(10, int(round(height * style.scale)))
    min_size = max(10, int(round(height * 0.030)))
    font = _load(size, family or style.family)
    lines = _wrap(scratch, text, font, max_width)
    for _ in range(10):
        if len(lines) <= style.max_lines or size <= min_size:
            break
        size = max(min_size, int(size * 0.94))
        font = _load(size, family or style.family)
        lines = _wrap(scratch, text, font, max_width)

    x0, y0, x1, y1 = safe_box(width, height, style)
    line_h = size * style.line_gap
    if len(lines) > style.max_lines:
        # 极长句：安全区高度允许时保留多出来的行；否则截断并明确告警。
        max_by_height = max(1, int((y1 - y0) // line_h))
        if len(lines) > max_by_height:
            lines = lines[:max_by_height]
            lines[-1] = lines[-1][: max(1, len(lines[-1]) - 1)] + "…"
    block_h = line_h * len(lines)
    # 留 2% 底边：底条/描边/阴影都会向外多占几像素，贴到 0.95H 会压线。
    bottom = y1 - height * 0.02
    top = bottom - block_h
    if top < y0:
        top = y0
        bottom = top + block_h

    stroke = max(0, int(round(size * style.stroke_ratio)))
    pad_x = max(4, int(width * 0.012))
    pad_y = max(3, int(size * 0.18))
    boxes: list[list[float]] = []
    for index in range(len(lines)):
        cy = top + line_h * (index + 0.5)
        bbox = scratch.textbbox((width / 2, cy), lines[index], font=font,
                                anchor="mm", stroke_width=stroke)
        boxes.append([bbox[0] - pad_x, bbox[1] - pad_y,
                      bbox[2] + pad_x, bbox[3] + pad_y])

    return {
        "size": size,
        "line_height": line_h,
        "lines": lines,
        "boxes": boxes,
        "stroke": stroke,
        "font": font,
        "font_path": _font_path(family or style.family),
        "truncated": len(lines) < len(_wrap(scratch, text, font, max_width)),
    }


def draw_subtitle(draw: ImageDraw.ImageDraw, text: str, width: int, height: int,
                  style: SubtitleStyle, report: list[str] | None = None) -> None:
    """把一条整句字幕画到底部安全区。``report`` 收集缺字/回退告警。"""
    content = str(text or "").strip()
    if not content:
        return
    info = coverage([content], style)
    family = style.family
    if not info["ok"]:
        if info["uncovered"]:
            # 连回退字体都缺字：明确告警，仍按首选字体排版（不静默当正常）。
            if report is not None:
                report.append(
                    f"样式「{style.label}」字体缺字，回退字体仍缺 "
                    f"{len(info['uncovered'])} 个字符，画面可能显示方块"
                )
        else:
            family = _family_of_path(info["fallback"]) or style.family
            if report is not None:
                report.append(
                    f"样式「{style.label}」字体缺字，已回退到 {info['fallback']}"
                )

    info_layout = layout(content, width, height, style, family=family)
    if any(box[0] < 0 or box[2] > width for box in info_layout["boxes"]):
        if report is not None:
            report.append(f"样式「{style.label}」字幕超出画面两侧，已自动缩小字号")
        # 横向溢出时再缩一档字重排（竖屏 18 字长句最容易触发）。
        shrink = SubtitleStyle(
            **{**asdict(style), "scale": max(0.030, style.scale * 0.9)}
        )
        info_layout = layout(content, width, height, shrink, family=family)
        if any(box[0] < 0 or box[2] > width for box in info_layout["boxes"]):
            if report is not None:
                report.append(f"样式「{style.label}」缩小后仍超出画面两侧，请缩短字幕")

    if info_layout.get("truncated") and report is not None:
        report.append(f"样式「{style.label}」字幕过长，已截断到安全区能容纳的行数")

    box_fill = _rgba(style.box_color, style.box)
    for index, line in enumerate(info_layout["lines"]):
        cy = info_layout["boxes"][index][1] + (info_layout["boxes"][index][3] -
                                              info_layout["boxes"][index][1]) / 2
        if style.box > 0:
            box = info_layout["boxes"][index]
            draw.rounded_rectangle(
                box, radius=max(2, int(info_layout["size"] * 0.22)), fill=box_fill
            )
        if style.shadow > 0:
            offset = max(1, int(info_layout["size"] * 0.05))
            draw.text((width / 2 + offset, cy + offset), line,
                      font=info_layout["font"], anchor="mm",
                      fill=(0, 0, 0, style.shadow),
                      stroke_width=info_layout["stroke"],
                      stroke_fill=(0, 0, 0, style.shadow))
        draw.text((width / 2, cy), line, font=info_layout["font"], anchor="mm",
                  fill=_rgba(style.color, 255),
                  stroke_width=info_layout["stroke"],
                  stroke_fill=_rgba(style.stroke, 255))


def _family_of_path(path: str) -> str:
    name = Path(path).name.lower()
    for family, candidates in FONT_FAMILIES.items():
        if any(Path(item).name.lower() == name for item in candidates):
            return family
    return ""
