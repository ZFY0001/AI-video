# -*- coding: utf-8 -*-
"""C4 转场与字幕动效的真实 FFmpeg 校验（约 20 秒）。

验四件单测覆盖不到的事：

1. 三种转场（叠化/左滑/黑场）在镜头边界**真的可见**，不是只改了参数；
2. 开启转场后总时长仍与分镜台账一致（三镜各 2s → 6s，误差 <0.3s）；
3. 关闭转场时仍是硬切（边界帧直接跳到下一镜，不做混合）；
4. 字幕淡入开关真实改变叠加层：0.2s 内由暗到亮，固定标题层不参与。

用法：
    python tools/verify_transitions.py
    python tools/verify_transitions.py --keep    # 保留视频与抽帧供人工查看
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline.compose import (  # noqa: E402
    TRANSITIONS,
    concat_clips,
    overlay_text_layers,
    transition_clips,
)
from pipeline.video import VideoError, probe_duration, run_ffmpeg  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []
SIZE = "320x180"
RED = (253, 0, 0)
GREEN = (0, 127, 0)
BLUE = (0, 0, 254)


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def make_clip(path: Path, color: str, seconds: float) -> Path:
    run_ffmpeg(["-f", "lavfi", "-i", f"color=c={color}:s={SIZE}:r=30:d={seconds}",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                str(path)])
    return path


def frame_pixel(video: Path, time: float, tmp: Path, tag: str, x: int = 160) -> tuple:
    out = tmp / f"f-{tag}-{time:.3f}.png"
    run_ffmpeg(["-ss", f"{time:.3f}", "-i", str(video), "-frames:v", "1", str(out)])
    with Image.open(out) as image:
        return image.convert("RGB").getpixel((x, 90))


def near(px, target, tolerance: int = 30) -> bool:
    return all(abs(int(a) - int(b)) <= tolerance for a, b in zip(px, target))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="保留产物供人工查看")
    args = parser.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="transition-verify-"))

    print("== 1. 参数与前置检查 ==")
    check("只提供三种转场 + 硬切",
          set(TRANSITIONS) == {"none", "fade", "slide", "black"}, str(list(TRANSITIONS)))
    clips = [make_clip(tmp / f"c{i}.mp4", color, 2.0)
             for i, color in enumerate(("red", "green", "blue"))]
    try:
        transition_clips(clips, [2.0, 2.0], tmp / "bad.mp4", tmp, "fade")
        raise_mismatch = False
    except VideoError:
        raise_mismatch = True
    check("片段数与时长数不一致时明确报错", raise_mismatch, "")
    try:
        transition_clips(clips, [2.0, 2.0, 2.0], tmp / "bad.mp4", tmp, "zoom")
        raise_unknown = False
    except VideoError:
        raise_unknown = True
    check("未知转场明确报错", raise_unknown, "")

    print("== 2. 三种转场：边界可见 + 总时长不变 ==")
    results = {}
    for kind in ("fade", "slide", "black"):
        out = transition_clips(clips, [2.0, 2.0, 2.0], tmp / f"{kind}.mp4", tmp, kind)
        duration = probe_duration(out)
        results[kind] = out
        check(f"{kind}：总时长 6.0s（误差 <0.3s）", abs(duration - 6.0) < 0.3,
              f"{duration:.3f}s")
        content = [frame_pixel(out, 0.5, tmp, kind), frame_pixel(out, 2.5, tmp, kind),
                   frame_pixel(out, 4.5, tmp, kind)]
        check(f"{kind}：三个镜头内容仍按原时间轴出现",
              near(content[0], RED) and near(content[1], GREEN) and near(content[2], BLUE),
              str(content))
        boundary = frame_pixel(out, 2.0, tmp, kind)
        if kind == "black":
            ok = max(boundary) < 90
            detail = f"{boundary}（应接近黑）"
        elif kind == "slide":
            left = frame_pixel(out, 2.0, tmp, f"{kind}-l", x=10)
            right = frame_pixel(out, 2.0, tmp, f"{kind}-r", x=310)
            ok = near(left, RED) and near(right, GREEN)
            detail = f"left={left} right={right}（应同时出现前后两镜）"
        else:
            ok = not near(boundary, RED) and not near(boundary, GREEN)
            detail = f"{boundary}（应为混合帧）"
        check(f"{kind}：边界帧不是硬切", ok, detail)

    print("== 3. 关闭转场：仍是硬切 ==")
    plain = concat_clips(clips, tmp / "plain.mp4", tmp)
    plain_duration = probe_duration(plain)
    before = frame_pixel(plain, 1.9, tmp, "plain")
    after = frame_pixel(plain, 2.1, tmp, "plain")
    check("硬切总时长为 6.0s", abs(plain_duration - 6.0) < 0.05, f"{plain_duration:.3f}s")
    check("硬切边界前是红、边界后直接是绿（无混合）",
          near(before, RED) and near(after, GREEN), f"{before} / {after}")

    print("== 4. 字幕淡入：真实改变叠加层 ==")
    base = make_clip(tmp / "black.mp4", "black", 2.0)
    clear = tmp / "clear.png"
    white = tmp / "white.png"
    Image.new("RGBA", (320, 180), (0, 0, 0, 0)).save(clear)
    Image.new("RGBA", (320, 180), (255, 255, 255, 255)).save(white)
    # 第 1 层是固定标题层（不应淡入），第 2 层是字幕卡（应从 0.4s 淡入）。
    # 固定层用透明图、卡片用白色；另一组反过来单独验证固定层不淡入。
    layers = [(clear, 0.0, 2.0), (white, 0.4, 1.6)]
    still = overlay_text_layers(base, layers, tmp / "still.mp4", animate=False)
    animated = overlay_text_layers(base, layers, tmp / "animated.mp4", animate=True)
    check("关闭动效：字幕起点即完全不透明",
          near(frame_pixel(still, 0.45, tmp, "still"), (255, 255, 255), 8), "")
    mid = frame_pixel(animated, 0.45, tmp, "anim")
    full = frame_pixel(animated, 0.7, tmp, "anim")
    check("开启动效：0.05s 处半透明、0.3s 处完全显示",
          20 < mid[0] < 235 and near(full, (255, 255, 255), 8), f"{mid} / {full}")
    chrome_only = overlay_text_layers(
        base, [(white, 0.0, 2.0), (clear, 0.4, 1.6)], tmp / "chrome.mp4", animate=True)
    check("固定标题层不参与淡入（0.05s 已完全显示）",
          near(frame_pixel(chrome_only, 0.05, tmp, "chrome"), (255, 255, 255), 8), "")

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
