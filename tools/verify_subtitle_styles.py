# -*- coding: utf-8 -*-
"""C2 字幕样式真实渲染校验（纯 Pillow，约 5 秒）。

验四件事，都是"只跑单测发现不了"的：

1. 6 套预置 × 16:9/9:16/1:1 的像素包围盒都落在字幕安全区内；
2. 不同样式画出来确实不同（颜色/底条/字号），不是换了 id 不换画面；
3. 长句在竖屏会自动换行/缩字号，既不溢出也不静默丢字（截断必须显式标记）；
4. 缺字检测能识别"字体没有这个字形"，并给出回退字体或明确的未覆盖字符。

用法：
    python tools/verify_subtitle_styles.py
    python tools/verify_subtitle_styles.py --keep    # 保留 PNG 供人工查看
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline import subtitle_style as sstyle  # noqa: E402
from pipeline.image_render import render_subtitle_card  # noqa: E402
from pipeline.models import ProjectBrief, Shot, Storyboard  # noqa: E402
from pipeline.pipeline import Pipeline  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []

SHORT = "羊骨吊足八小时，汤色奶白，一口下去暖到胃里"
LONG = "这是一条故意写得很长很长的字幕用来验证竖屏换行与字号自动收缩不会溢出安全区也不会丢字"
ASPECTS = ("16:9", "9:16", "1:1")


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def inside_safe(bbox, image: Image.Image, style: sstyle.SubtitleStyle) -> bool:
    left, top, right, bottom = sstyle.safe_box(*image.size, style)
    return bool(bbox) and (
        bbox[0] >= left - 2 and bbox[2] <= right + 2
        and bbox[1] >= top - 2 and bbox[3] <= bottom + 2
    )


def near(px, target: str, tolerance: int = 24) -> bool:
    r, g, b = px[:3]
    tr, tg, tb, _ = sstyle._rgba(target, 255)
    return abs(r - tr) <= tolerance and abs(g - tg) <= tolerance and abs(b - tb) <= tolerance


def color_pixels(image: Image.Image, target: str) -> int:
    colors = image.getcolors(maxcolors=image.width * image.height) or []
    return sum(count for count, px in colors if px[3] > 180 and near(px, target))


def opaque_pixels(image: Image.Image) -> int:
    colors = image.getcolors(maxcolors=image.width * image.height) or []
    return sum(count for count, px in colors if px[3] > 0)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="保留渲染产物")
    args = parser.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="yiyan-c2-verify-"))

    print("== 1. 6 套样式 × 3 画幅：安全区 ==")
    bad: list[str] = []
    for aspect in ASPECTS:
        brief = ProjectBrief(theme="字幕安全区", aspect_ratio=aspect)
        for style in sstyle._PRESETS:
            out = tmp / f"{aspect.replace(':', 'x')}-{style.id}.png"
            reports: list[str] = []
            render_subtitle_card(SHORT, brief, out, style=style, report=reports)
            with Image.open(out) as image:
                if image.mode != "RGBA" or not inside_safe(image.getbbox(), image, style) or reports:
                    bad.append(f"{aspect}/{style.id}/{image.getbbox()}/{reports}")
    check("18 张字幕卡全部在安全区内", not bad, "; ".join(bad[:3]))

    print("== 2. 样式差异：颜色/底条真的变了 ==")
    brief = ProjectBrief(theme="字幕差异", aspect_ratio="16:9")
    rendered: dict[str, Image.Image] = {}
    for style in sstyle._PRESETS:
        out = tmp / f"diff-{style.id}.png"
        render_subtitle_card(SHORT, brief, out, style=style)
        rendered[style.id] = Image.open(out).convert("RGBA")
    check("vivid 产出大量亮黄像素",
          color_pixels(rendered["vivid"], sstyle.STYLES["vivid"].color) > 800,
          str(color_pixels(rendered["vivid"], sstyle.STYLES["vivid"].color)))
    check("clean 与 warm 的主色像素不同",
          color_pixels(rendered["clean"], "#FFFFFF") > 800
          and color_pixels(rendered["warm"], "#FFE1B8") > 800, "")
    check("无底条样式（ink/film）透明像素更多",
          opaque_pixels(rendered["ink"]) < opaque_pixels(rendered["clean"]), "")

    print("== 3. 长句：换行/缩字号，不溢出 ==")
    long_bad: list[str] = []
    for aspect in ASPECTS:
        brief_long = ProjectBrief(theme="长句", aspect_ratio=aspect)
        for style in sstyle._PRESETS:
            out = tmp / f"long-{aspect.replace(':', 'x')}-{style.id}.png"
            reports: list[str] = []
            render_subtitle_card(LONG, brief_long, out, style=style, report=reports)
            with Image.open(out) as image:
                info = sstyle.layout(LONG, *image.size, style)
                visible = image.getbbox()
                if not inside_safe(visible, image, style) or reports:
                    long_bad.append(f"{aspect}/{style.id}/{visible}/{reports}")
                if info["truncated"] and not reports:
                    long_bad.append(f"{aspect}/{style.id}/截断但未告警")
    check("长句不溢出安全区且截断可见", not long_bad, "; ".join(long_bad[:3]))

    print("== 4. 缺字检测与回退 ==")
    common = sstyle.coverage([SHORT], sstyle.STYLES["clean"])
    emoji = sstyle.coverage(["测试" + chr(0x1F642)], sstyle.STYLES["clean"])
    check("常见中文被首选字体覆盖", common["ok"] and common["missing_count"] == 0, str(common))
    check("emoji 被明确判为缺字", not emoji["ok"] and emoji["missing_count"] >= 1, str(emoji))
    check("缺字时给出回退字体或未覆盖清单",
          bool(emoji["fallback"] or emoji["uncovered"]), str(emoji))
    arial = Path(r"C:\Windows\Fonts\arial.ttf")
    if arial.is_file():
        missing = sstyle._coverage_for(str(arial), list("中文A"))
        check("无中文字形的字体被识别（arial）",
              "中" in missing and "A" not in missing, str(missing))
    else:
        print("  [SKIP] 本机没有 arial.ttf，跳过无中文字体样本")

    print("== 5. 换样式只重做字幕卡，不碰片段 ==")
    work = tmp / "work"
    work.mkdir()
    clips = work / "clips"
    clips.mkdir()
    fake = SimpleNamespace(
        ctx=SimpleNamespace(work_dir=work),
        genre=None,
        preview=False,
        guides=False,
        subtitle_mode="burn",
        subtitle_offset=0.0,
        subtitle_lines=[{"start": 0.0, "end": 1.0, "text": SHORT}],
        music=SimpleNamespace(enabled=False, is_bgm=False),
        lyric_lines=[],
        subtitle_style=sstyle.STYLES["clean"],
    )
    board = Storyboard(project={"theme": "t", "aspect_ratio": "16:9"},
                       subjects=[], shots=[Shot(id=1, act="A", duration=1.0,
                                                narration=SHORT, visual="", camera="")])
    from pipeline.palette import apply as apply_palette
    from pipeline.genre import pick_genre
    genre = apply_palette(pick_genre("面"), "auto")
    fake.genre = genre
    brief_pipe = ProjectBrief(theme="t", aspect_ratio="16:9")
    layers_a = Pipeline._text_layers(fake, brief_pipe, board, board.shots)
    card_a, chrome_a = Path(layers_a[-1][0]), Path(layers_a[0][0])
    card_a_bytes, chrome_a_bytes = card_a.read_bytes(), chrome_a.read_bytes()
    fake.subtitle_style = sstyle.STYLES["vivid"]
    layers_b = Pipeline._text_layers(fake, brief_pipe, board, board.shots)
    card_b, chrome_b = Path(layers_b[-1][0]), Path(layers_b[0][0])
    check("同一时间轴、同一条字幕，两种样式产出不同卡片",
          card_a_bytes != card_b.read_bytes(), "")
    check("换样式没有生成/改写任何片段",
          not any(clips.iterdir()), str(list(clips.iterdir())))
    check("固定层（镜号/标题）不随样式变化",
          chrome_a_bytes == chrome_b.read_bytes(), "")

    print(f"\n结果：{len(PASS)} 项通过，{len(FAIL)} 项失败")
    if args.keep:
        print(f"产物目录：{tmp}")
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    if FAIL:
        print("失败项：")
        for name in FAIL:
            print("  - " + name)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
