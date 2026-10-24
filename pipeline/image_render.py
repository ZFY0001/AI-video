"""一言片场 · 离线画面合成（占位帧）。

**定位说清楚**：这不是文生图，是「占位帧合成器」。它把镜头信息画成一张有构图、
有影调、带字幕位的静帧，用来验证「分镜 → 画面 → 视频 → 成片」的整条管线。
配置了 ComfyUI 或云端出图后，本模块自动降级为备用（见 images.py 的 provider 选择）。

设计目标：确定性（同一 run 重跑像素级一致）、可辨认（一眼看出是哪一镜）、有美感（能当分镜示意）。
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .common import stable_hash
from .genre import Genre
from .models import ProjectBrief, Shot, caption_of

RESOLUTIONS = {"16:9": (1920, 1080), "9:16": (1080, 1920), "1:1": (1080, 1080)}
FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\Deng.ttf",
)
SHOT_TYPE_BY_CAMERA = (
    ("特写", "CLOSE-UP"),
    ("中景", "MEDIUM"),
    ("全景", "WIDE"),
    ("局部", "DETAIL"),
)


@dataclass
class FrameStyle:
    title: str
    index: int
    total: int
    shot_type: str
    shot_type_en: str
    narration: str
    acceptance: str
    timecode: str
    note: str = ""
    #: 这张画面是谁出的（comfy / cloud / offline / 空）。水印要靠它写实话：
    #: 标"占位帧"or"AI 出图"，不能一律写死。
    still_provider: str = ""


def load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in FONT_CANDIDATES:
        p = Path(path)
        if p.exists():
            try:
                return ImageFont.truetype(str(p), size)
            except OSError:
                continue
    return ImageFont.load_default()


def render_still(
    shot: Shot,
    brief: ProjectBrief,
    genre: Genre,
    out_path: Path,
    total_shots: int = 1,
    show_guides: bool = False,
    scale: int = 1,
    with_text: bool = True,
) -> Path:
    """渲染单张占位帧并落盘，返回路径。

    with_text=False 时只出干净背景（供 Ken Burns 做运动），文字由 render_overlay 单独叠加。
    字幕位在两种情况下都保留（成片需要字幕位）。
    """
    w, h = _dims(brief, scale)
    img = render_background(shot, brief, genre, w, h)
    draw = ImageDraw.Draw(img, "RGBA")
    if with_text:
        _paint_text(img, draw, _frame_style(shot, brief, genre, total_shots), genre, w, h, brief)
    else:
        _paint_subtitle_plates(draw, shot, w, h)
    if show_guides:
        _paint_guides(draw, w, h)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG")
    return out_path


def _dims(brief: ProjectBrief, scale: int = 1) -> tuple[int, int]:
    w, h = RESOLUTIONS.get(brief.aspect_ratio, RESOLUTIONS["16:9"])
    return w * max(1, scale), h * max(1, scale)


def render_background(shot: Shot, brief: ProjectBrief, genre: Genre, w: int, h: int) -> Image.Image:
    """只画背景（渐变 + 题材装饰 + 暗角），不画任何文字。供 Ken Burns 缩放用。"""
    seed = stable_hash(f"{brief.theme}|{shot.id}|{shot.visual}")
    rng = random.Random(seed)
    img = Image.new("RGB", (w, h), genre.palette[0][0])
    draw = ImageDraw.Draw(img, "RGBA")
    _paint_background(img, draw, genre, rng, w, h)
    _paint_decor(img, draw, genre, rng, w, h, seed)
    _paint_vignette(img, w, h)
    return img


def _frame_style(shot: Shot, brief: ProjectBrief, genre: Genre, total_shots: int) -> "FrameStyle":
    st_cn, st_en = _shot_type(shot.camera)
    return FrameStyle(
        title=str(shot.act or ""),
        index=shot.id,
        total=total_shots,
        shot_type=st_cn,
        shot_type_en=st_en,
        narration=caption_of(shot),
        acceptance=shot.acceptance,
        timecode=f"{shot.start:05.1f}-{shot.end:05.1f}s",
        still_provider=getattr(shot, "still_provider", "") or "",
    )


def render_overlay(
    shot: Shot,
    brief: ProjectBrief,
    genre: Genre,
    out_path: Path,
    total_shots: int = 1,
    show_guides: bool = False,
    scale: int = 1,
) -> Path:
    """渲染透明底文字层（RGBA），由 FFmpeg overlay 叠到运动背景之上。

    scale 必须与背景的运动画布倍率一致，否则文字会被二次缩放（糊）。
    """
    w, h = _dims(brief, scale)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img, "RGBA")
    _paint_text(img, draw, _frame_style(shot, brief, genre, total_shots), genre, w, h, brief)
    if show_guides:
        _paint_guides(draw, w, h)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG")
    return out_path


# ------------------------------------------------------------------ 背景
def _paint_background(img: Image.Image, draw: ImageDraw.ImageDraw, genre: Genre,
                      rng: random.Random, w: int, h: int) -> None:
    """整幅竖向渐变（多点平滑插值，无硬边分带）+ 一团柔光。"""
    stops = _gradient_stops(genre)
    for i in range(h):
        draw.line([(0, i), (w, i)], fill=_sample_gradient(stops, i / max(1, h - 1)))

    glow = Image.new("RGB", (w, h), (0, 0, 0))
    gd = ImageDraw.Draw(glow, "RGBA")
    cx = w * rng.uniform(0.25, 0.75)
    cy = h * rng.uniform(0.18, 0.42)
    rx, ry = w * 0.5, h * 0.42
    for k in range(26, 0, -1):
        a = int(11 * (k / 26) ** 2)
        gd.ellipse(
            [cx - rx * k / 26, cy - ry * k / 26, cx + rx * k / 26, cy + ry * k / 26],
            fill=_hex_rgba(genre.accents[0], a),
        )
    glow = glow.filter(ImageFilter.GaussianBlur(w * 0.07))
    img.paste(Image.blend(img, Image.composite(glow, img, _mask_from(glow)), 0.9), (0, 0))


def _gradient_stops(genre: Genre) -> list[tuple[float, tuple[int, int, int]]]:
    """把题材色板摊平成 0-1 位置上的颜色停靠点（深→浅）。"""
    pal = genre.palette or (("#101418", "#1E2830"),)
    stops: list[tuple[float, tuple[int, int, int]]] = []
    n = len(pal)
    for idx, (dark, light) in enumerate(pal):
        base = idx / n
        stops.append((base, _hex_rgb(dark)))
        stops.append((base + 0.5 / n, _hex_rgb(light)))
    stops.append((1.0, _hex_rgb(pal[-1][1])))
    return stops


def _sample_gradient(stops: list[tuple[float, tuple[int, int, int]]], t: float) -> tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    for i in range(len(stops) - 1):
        p0, c0 = stops[i]
        p1, c1 = stops[i + 1]
        if p0 <= t <= p1:
            local = 0.0 if p1 == p0 else (t - p0) / (p1 - p0)
            local = local * local * (3 - 2 * local)  # smoothstep，消除线性插值的生硬感
            return tuple(int(c0[k] + (c1[k] - c0[k]) * local) for k in range(3))
    return stops[-1][1]


def _hex_rgb(color: str) -> tuple[int, int, int]:
    r, g, b, _ = _hex_rgba(color, 255)
    return (r, g, b)


def _mask_from(layer: Image.Image) -> Image.Image:
    return layer.convert("L").point(lambda v: min(255, int(v * 1.6)))


def _paint_decor(img: Image.Image, draw: ImageDraw.ImageDraw, genre: Genre,
                 rng: random.Random, w: int, h: int, seed: int) -> None:
    kind = genre.decor
    if kind == "steam":
        _decor_steam(draw, genre, rng, w, h)
    elif kind == "skyline":
        _decor_skyline(draw, genre, rng, w, h)
    elif kind == "dust":
        _decor_dust(img, draw, genre, rng, w, h)
    elif kind == "grid":
        _decor_grid(draw, genre, rng, w, h)
    elif kind == "ink":
        _decor_ink(img, draw, genre, rng, w, h)
    else:
        _decor_bokeh(img, draw, genre, rng, w, h)


def _decor_steam(draw: ImageDraw.ImageDraw, genre: Genre, rng: random.Random, w: int, h: int) -> None:
    """热气：从画面下三分之一升起、逐层淡出的柔和飘带（不用粗线，避免出现虚线感）。"""
    for i in range(11):
        x0 = w * rng.uniform(0.15, 0.85)
        y0 = h * rng.uniform(0.70, 0.98)
        amp = w * rng.uniform(0.015, 0.045)
        height = h * rng.uniform(0.3, 0.6)
        phase = rng.uniform(0, math.tau)
        # 由下往上采样，越往上越细、越淡
        for step in range(30):
            t = step / 29
            x = x0 + math.sin(t * math.pi * 1.6 + phase) * amp * (0.3 + t)
            y = y0 - height * t
            width = max(1, int(w * 0.014 * (1 - t) + 1))
            alpha = int(13 * (1 - t) ** 1.4 + 2)
            draw.ellipse(
                [x - width, y - width * 0.7, x + width, y + width * 0.7],
                fill=_hex_rgba(genre.accents[2], alpha),
            )
    for i in range(16):
        cx, cy = w * rng.uniform(0.05, 0.95), h * rng.uniform(0.35, 0.98)
        r = w * rng.uniform(0.004, 0.018)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=_hex_rgba(genre.accents[0], rng.randint(16, 44)))


def _decor_skyline(draw: ImageDraw.ImageDraw, genre: Genre, rng: random.Random, w: int, h: int) -> None:
    """城市天际线：两层楼群剪影 + 亮窗 + 光轨。"""
    for layer, (alpha, base) in enumerate(((180, 0.62), (120, 0.78))):
        x = -w * 0.05
        while x < w * 1.05:
            bw = w * rng.uniform(0.03, 0.075)
            bh = h * rng.uniform(0.08, 0.3) * (1.0 + layer * 0.35)
            top = h * base - bh
            draw.rectangle([x, top, x + bw, h * base + h * 0.05], fill=(8, 14, 22, alpha))
            rows = int(bh / (h * 0.035))
            cols = max(1, int(bw / (w * 0.016)))
            for r in range(rows):
                for c in range(cols):
                    if rng.random() < 0.28:
                        wx = x + w * 0.006 + c * w * 0.016
                        wy = top + h * 0.014 + r * h * 0.035
                        draw.rectangle([wx, wy, wx + w * 0.006, wy + h * 0.011],
                                       fill=_hex_rgba(genre.accents[0], rng.randint(90, 210)))
            x += bw * rng.uniform(1.02, 1.35)
    for i in range(3):
        y = h * rng.uniform(0.86, 0.97)
        pts = [(0, y + math.sin(t / 12) * h * 0.006) for t in range(0, int(w), 12)]
        draw.line(pts, fill=_hex_rgba(genre.accents[1], rng.randint(40, 80)), width=max(2, int(h * 0.004)), joint="curve")


def _decor_dust(img: Image.Image, draw: ImageDraw.ImageDraw, genre: Genre,
                rng: random.Random, w: int, h: int) -> None:
    """人物故事：一束窗光 + 浮尘。"""
    beam = Image.new("RGB", (w, h), (0, 0, 0))
    bd = ImageDraw.Draw(beam, "RGBA")
    x0 = w * rng.uniform(0.1, 0.45)
    bd.polygon(
        [(x0, 0), (x0 + w * 0.22, 0), (x0 + w * 0.42, h), (x0 + w * 0.04, h)],
        fill=_hex_rgba(genre.accents[0], 46),
    )
    beam = beam.filter(ImageFilter.GaussianBlur(w * 0.03))
    img.paste(Image.blend(img, Image.composite(beam, img, _mask_from(beam)), 0.9), (0, 0))
    for i in range(90):
        cx, cy = rng.uniform(0, w), rng.uniform(0, h)
        r = rng.uniform(0.6, 2.6)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=_hex_rgba(genre.accents[0], rng.randint(30, 120)))


def _decor_grid(draw: ImageDraw.ImageDraw, genre: Genre, rng: random.Random, w: int, h: int) -> None:
    """产品：干净背景 + 网格 + 一道镜面高光。"""
    step = int(w * 0.04)
    for x in range(0, w, step):
        draw.line([(x, 0), (x, h)], fill=_hex_rgba(genre.accents[2], 10), width=1)
    for y in range(0, h, step):
        draw.line([(0, y), (w, y)], fill=_hex_rgba(genre.accents[2], 10), width=1)
    cx, cy = w * 0.5, h * 0.62
    rw, rh = w * 0.2, h * 0.16
    for k in range(3):
        draw.rounded_rectangle(
            [cx - rw, cy - rh, cx + rw, cy + rh], radius=int(h * 0.02),
            outline=_hex_rgba(genre.accents[1], 90 - k * 25), width=max(2, int(h * 0.003) + k),
        )
    draw.line([(cx - rw * 1.4, cy - rh * 1.3), (cx - rw * 1.05, cy + rh * 1.4)],
              fill=_hex_rgba("#FFFFFF", 60), width=int(w * 0.012))


def _decor_ink(img: Image.Image, draw: ImageDraw.ImageDraw, genre: Genre,
               rng: random.Random, w: int, h: int) -> None:
    """国风：水墨晕染 + 飞檐剪影 + 铜铃。"""
    ink = Image.new("RGB", (w, h), (0, 0, 0))
    idr = ImageDraw.Draw(ink, "RGBA")
    for i in range(7):
        cx, cy = rng.uniform(0, w), rng.uniform(h * 0.2, h * 0.95)
        rx, ry = rng.uniform(w * 0.05, w * 0.22), rng.uniform(h * 0.04, h * 0.16)
        idr.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=_hex_rgba(genre.accents[0], rng.randint(16, 40)))
    ink = ink.filter(ImageFilter.GaussianBlur(w * 0.02))
    img.paste(Image.blend(img, Image.composite(ink, img, _mask_from(ink)), 0.9), (0, 0))
    base_y = h * 0.68
    draw.polygon(
        [(0, base_y), (w * 0.3, base_y - h * 0.05), (w * 0.62, base_y), (w, base_y - h * 0.06), (w, h), (0, h)],
        fill=(10, 10, 9, 150),
    )
    draw.arc([w * 0.55, base_y - h * 0.28, w * 1.05, base_y + h * 0.06], 190, 350,
             fill=_hex_rgba(genre.accents[0], 120), width=max(3, int(h * 0.005)))
    bx, by = w * 0.72, base_y - h * 0.12
    draw.line([(bx, by), (bx, by + h * 0.05)], fill=_hex_rgba(genre.accents[0], 130), width=2)
    draw.ellipse([bx - w * 0.008, by + h * 0.05, bx + w * 0.008, by + h * 0.066],
                 outline=_hex_rgba(genre.accents[1], 150), width=2)


def _decor_bokeh(img: Image.Image, draw: ImageDraw.ImageDraw, genre: Genre,
                 rng: random.Random, w: int, h: int) -> None:
    for i in range(45):
        cx, cy = rng.uniform(0, w), rng.uniform(0, h)
        r = rng.uniform(w * 0.004, w * 0.05)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=_hex_rgba(genre.accents[i % len(genre.accents)], rng.randint(12, 45)))


def _paint_vignette(img: Image.Image, w: int, h: int) -> None:
    mask = Image.new("L", (w, h), 0)
    md = ImageDraw.Draw(mask)
    pad = int(min(w, h) * 0.06)
    md.ellipse([-pad, -pad, w + pad, h + pad], fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(min(w, h) * 0.12))
    dark = Image.new("RGB", (w, h), (0, 0, 0))
    img.paste(Image.composite(img, dark, mask), (0, 0))


# ------------------------------------------------------------------ 文字
def _paint_subtitle_plates(draw: ImageDraw.ImageDraw, shot: Shot, w: int, h: int) -> None:
    """干净背景上的字幕底板：只留半透明色块（成片字幕位），不画任何文字。

    这样背景图直接拿去当封面/预览也不会出现「同一句话叠两遍」。
    """
    text = (shot.narration or "").strip()
    if not text:
        return
    sub_y = int(h * 0.845)
    lines = _wrap_cn(text, 18)[:2]
    for i in range(len(lines)):
        y = sub_y + i * int(h * 0.055)
        draw.rectangle(
            [w * 0.28, y - int(h * 0.006), w * 0.72, y + int(h * 0.05)],
            fill=(0, 0, 0, 70),
        )


def _paint_text(img: Image.Image, draw: ImageDraw.ImageDraw, st: FrameStyle,
                genre: Genre, w: int, h: int, brief: ProjectBrief) -> None:
    pad = int(w * 0.045)
    f_badge = load_font(int(h * 0.026))
    f_small = load_font(int(h * 0.02))
    f_title = load_font(int(h * 0.042))
    f_sub = load_font(int(h * 0.041))

    # 左上：镜号 + 景别 + 时间码
    badge = f"镜 {st.index:02d}/{st.total:02d}   {st.shot_type} / {st.shot_type_en}"
    _text_with_shadow(draw, (pad, pad), badge, f_badge, genre.accents[2])
    _text_with_shadow(draw, (pad, pad + int(h * 0.036)), f"TC {st.timecode}", f_small, genre.accents[0])

    # 右上：项目信息
    info = f"{brief.aspect_ratio} · {brief.total_duration}s · {genre.label}"
    tw = draw.textlength(info, font=f_small)
    _text_with_shadow(draw, (w - pad - tw, pad), info, f_small, genre.accents[0], align_right=True)

    # 上部标题（片名 + 段）
    title = f"{st.index:02d} · {genre.subjects[(st.index - 1) % len(genre.subjects)]}"
    _text_with_shadow(draw, (pad, int(h * 0.62)), title, f_title, "#FFFFFF")

    # 底部字幕（模拟成片字幕位）
    sub_y = int(h * 0.845)
    if st.narration:
        lines = _wrap_cn(st.narration, 18)[:2]
        for i, line in enumerate(lines):
            tw = draw.textlength(line, font=f_sub)
            x = (w - tw) / 2
            y = sub_y + i * int(h * 0.055)
            draw.rectangle(
                [x - int(w * 0.012), y - int(h * 0.006), x + tw + int(w * 0.012), y + int(h * 0.05)],
                fill=(0, 0, 0, 90),
            )
            _text_with_shadow(draw, (x, y), line, f_sub, "#FFFFFF")

    # 左下角：验收标准（给质检用的可见依据）
    if st.acceptance:
        _text_with_shadow(draw, (pad, h - pad - int(h * 0.03)), f"验收：{st.acceptance}",
                          f_small, genre.accents[1])

    # 右下角：画面来源水印。**必须按实际 provider 写**：
    # 原来无条件写"占位帧 · 未接入文生图"，于是 ComfyUI 出了真图、成片里却
    # 还标着"未接入"——比不标更糟，属于自相矛盾的假信息。
    # "" 表示还没经过 provider（分镜示意帧），此时标占位帧是对的。
    _prov = getattr(st, "still_provider", "") or ""
    if _prov == "comfy":
        tag = "AI 出图 · 本地 ComfyUI（SD 1.5）"
    elif _prov == "cloud":
        tag = "AI 出图 · 云端"
    elif st.note:
        tag = st.note
    else:
        tag = "占位帧 · 未接入文生图"
    tw = draw.textlength(tag, font=f_small)
    _text_with_shadow(draw, (w - pad - tw, h - pad - int(h * 0.03)), tag, f_small, "#E8E8E8", align_right=True)


def _text_with_shadow(draw: ImageDraw.ImageDraw, xy, text: str, font, color: str,
                      align_right: bool = False) -> None:
    x, y = xy
    draw.text((x + 2, y + 2), text, font=font, fill=(0, 0, 0, 170))
    draw.text((x, y), text, font=font, fill=color)


def _paint_guides(draw: ImageDraw.ImageDraw, w: int, h: int) -> None:
    """三分线 + 字幕安全区，供人工检查构图（--guides）。"""
    for frac in (1 / 3, 2 / 3):
        draw.line([(w * frac, 0), (w * frac, h)], fill=(255, 255, 255, 60), width=2)
        draw.line([(0, h * frac), (w, h * frac)], fill=(255, 255, 255, 60), width=2)
    draw.rectangle([w * 0.05, h * 0.05, w * 0.95, h * 0.95], outline=(255, 80, 80, 120), width=3)
    draw.rectangle([w * 0.05, h * 0.8, w * 0.95, h * 0.95], outline=(80, 200, 255, 120), width=3)


def _shot_type(camera: str) -> tuple[str, str]:
    for cn, en in SHOT_TYPE_BY_CAMERA:
        if cn in camera:
            return cn, en
    return "中景", "MEDIUM"


def _wrap_cn(text: str, per_line: int) -> list[str]:
    t = (text or "").strip()
    if not t:
        return []
    return [t[i : i + per_line] for i in range(0, len(t), per_line)]


def _hex_rgba(color: str, alpha: int) -> tuple[int, int, int, int]:
    color = color.lstrip("#")
    if len(color) == 3:
        color = "".join(c * 2 for c in color)
    r, g, b = (int(color[i : i + 2], 16) for i in (0, 2, 4))
    return (r, g, b, max(0, min(255, alpha)))


def _lerp_hex(c0: str, c1: str, t: float) -> tuple[int, int, int]:
    a, b = _hex_rgba(c0, 255), _hex_rgba(c1, 255)
    t = max(0.0, min(1.0, t))
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))
