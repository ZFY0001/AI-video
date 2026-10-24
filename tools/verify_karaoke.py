# -*- coding: utf-8 -*-
"""逐字卡拉OK 端到端验证：**画面里到底有没有那条字幕，字符有没有逐字填**。

只验"脚本接没接上"是没用的——页面加载顺序、`const LY` 改不动、字体预载太晚，
每一种失败都会让画面**静默**退回"没有卡拉OK"（甚至没有字幕），而成片照样能出。

**实验设计（两次踩坑后定下来的）**

1. 先试过"两遍渲染逐像素相减"，对照区差异就有 7.9，结论站不住：
   画面本身随 t 变化，水彩/WebGL 栅格化也不是逐位可复现的。
2. 再试过"字条区域暗像素占比"，也不成立：本场景底部**本来就是深色前景**，
   暗占比恒 84-99%，而卡拉OK的字是**浅色**画在深色条上——判据方向反了。
3. 最终用**暖色像素占比**：画面是蓝调的（B>R），而唱到的字是**金色**（R≫B），
   所以"暖色像素"只可能来自卡拉OK。

同时必须把两遍渲染做成**严格对照**，否则会引入混淆：
   叙事留空（两版都不画场景字幕）、**bpm 相同**（否则主体呼吸动作不一样）。
   于是两版的场景 JS 与 config.js 逐字节相同，唯一差别就是卡拉OK 图层本身。
   这个"逐字节相同"本身就是对照组，工具会断言它。

用法：
    python tools/verify_karaoke.py                 # 默认 3 秒 / 4fps
    python tools/verify_karaoke.py --seconds 4 --fps 4 --keep
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageChops

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline import painted as pb  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []

#: 字条实际占据的区域（x 居中，y≈976-1120，画布高 1080 所以底部被裁一点）
BAR_REGION = (360, 975, 1560, 1080)
#: 对照组：画面中上部
CTRL_REGION = (360, 300, 1560, 700)
#: 暖色判据：R 比 B 高 40 以上，且本身够亮（金色文字）
WARM_DELTA = 40
WARM_MIN_R = 90
#: 稳定的 bpm，两遍渲染必须一致
FIXED_BPM = 120.0


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def warm_fraction(path: Path, box: tuple[int, int, int, int]) -> float:
    """给定区域里"暖色像素"（金色文字）的占比。用 PIL 的 C 层运算，够快。"""
    im = Image.open(path).convert("RGB").crop(box)
    r, _g, b = im.split()
    warm = ImageChops.subtract(r, b).point(lambda v: 255 if v > WARM_DELTA else 0)
    bright = r.point(lambda v: 255 if v > WARM_MIN_R else 0)
    mask = ImageChops.multiply(warm, bright)
    return mask.histogram()[255] / float(im.width * im.height)


def shots_fixture(seconds: float) -> list[dict]:
    # narration 留空：两版都不画场景字幕，保证场景 JS 逐字节相同
    return [{
        "duration": seconds, "act": "起", "narration": "",
        "visual": "一片开阔的水彩天空与远山", "camera": "固定机位，中景",
        "acceptance": "画面统一",
    }]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument("--fps", type=int, default=4)
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    print("=" * 70)
    print("逐字卡拉OK 端到端验证（暖色像素 + 逐字填充单调性）")
    print("=" * 70)

    # ---------- 0. 接线自检 ----------
    print("\n[0] 接线自检")
    kjs = (ROOT / "vendor" / "painted-template" / "src" / "karaoke.js").read_text(encoding="utf-8")
    check("karaoke.js 已参数化（读 window.KARAOKE_LY）", "window.KARAOKE_LY" in kjs, "")
    html = pb.build_studio_html("src/scenes/yiyyan.js", karaoke=True)
    check("studio 页按正确顺序加载 lyrics.js → karaoke.js",
          html.index("src/lyrics.js") < html.index("src/karaoke.js"), "")
    check("karaoke.js 晚于 core.js（它用 core 的 PAL/hash/ease）",
          html.index("src/core.js") < html.index("src/karaoke.js"), "")

    st = pb.check_env()
    print(f"\n[1] 渲染环境：{'就绪' if st.ok else '不可用'} · {st.detail}")
    if not st.ok:
        check("渲染环境可用", False, st.detail)
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="karaoke_"))
    frames_dir = pb.work_dir() / "out" / "frames"
    work = pb.work_dir()
    try:
        # ---------- A：无歌词（严格对照：同 bpm、同叙事）----------
        print(f"\n[2] 渲染 A：无歌词（对照）{args.seconds}s @ {args.fps}fps，bpm={FIXED_BPM}")
        ra = pb.render_shots(shots_fixture(args.seconds), tmp / "outA",
                             genre_label="城市宣传", fps=args.fps, workers=2, encode=False,
                             log=lambda m: print("    · " + m), bpm=FIXED_BPM)
        check("渲染 A 成功", bool(ra.get("ok")), str(ra.get("error", ""))[:200])
        scene_a = (work / "src" / "scenes" / "yiyyan.js").read_text(encoding="utf-8")
        config_a = (work / "src" / "config.js").read_text(encoding="utf-8")
        dir_a = tmp / "framesA"
        shutil.copytree(frames_dir, dir_a)

        # ---------- B：带歌词 ----------
        t0, t1 = max(0.25, args.seconds * 0.2), max(0.5, args.seconds * 0.75)
        lyrics = [{"start": t0, "end": t1, "text": "夜色落在窗台上"}]
        print(f"\n[3] 渲染 B：带歌词（逐字卡拉OK）")
        print(f"    歌词 [{t0:.2f}, {t1:.2f}] 「{lyrics[0]['text']}」")
        rb = pb.render_shots(shots_fixture(args.seconds), tmp / "outB",
                             genre_label="城市宣传", fps=args.fps, workers=2, encode=False,
                             log=lambda m: print("    · " + m),
                             bpm=FIXED_BPM, lyrics=lyrics)
        check("渲染 B 成功", bool(rb.get("ok")), str(rb.get("error", ""))[:200])
        check("返回里标明启用了卡拉OK（encode=False 也带这个键）",
              rb.get("karaoke") is True, str(rb.get("karaoke")))
        scene_b = (work / "src" / "scenes" / "yiyyan.js").read_text(encoding="utf-8")
        config_b = (work / "src" / "config.js").read_text(encoding="utf-8")
        ljs = (work / "src" / "lyrics.js").read_text(encoding="utf-8")
        dir_b = tmp / "framesB"
        shutil.copytree(frames_dir, dir_b)

        # ---------- 1. 对照组：两版除卡拉OK 外必须完全一致 ----------
        print("\n[4] 对照组：两版的场景与配置必须逐字节相同（否则结论会被混淆）")
        check("两版场景 JS 逐字节相同", scene_a == scene_b, "场景 JS 不同 → 差异未必来自卡拉OK")
        check("两版 config.js 逐字节相同（bpm 一致）", config_a == config_b, "")
        check("lyrics.js 写入了该句歌词", "夜色落在窗台上" in ljs, ljs[:100])
        check("开了卡拉OK 后场景里不再有整句字幕（避免两套字幕叠加）",
              "caption" not in scene_a and "night" not in scene_a, "")

        # ---------- 2. 暖色像素 ----------
        n = int(round(args.seconds * args.fps))
        inside = [i for i in range(n) if t0 <= i / args.fps < t1]
        outside = [i for i in range(n) if not (t0 <= i / args.fps < t1)]
        print(f"\n[5] 暖色像素占比（字条区域 {BAR_REGION}）")
        print(f"    歌词时间内帧：{inside}")
        print(f"    歌词时间外帧：{outside}")
        print(f"    {'帧':>4}{'t':>7}{'A 无歌词':>12}{'B 带歌词':>12}")

        a_bar, b_bar, a_ctrl, b_ctrl = {}, {}, {}, {}
        for i in range(n):
            fa, fb = dir_a / f"f{i:05d}.jpg", dir_b / f"f{i:05d}.jpg"
            if not (fa.exists() and fb.exists()):
                continue
            a_bar[i] = warm_fraction(fa, BAR_REGION)
            b_bar[i] = warm_fraction(fb, BAR_REGION)
            a_ctrl[i] = warm_fraction(fa, CTRL_REGION)
            b_ctrl[i] = warm_fraction(fb, CTRL_REGION)
            mark = "  ← 词内" if i in inside else ""
            print(f"    {i:>4}{i / args.fps:7.2f}{a_bar[i] * 100:11.2f}%{b_bar[i] * 100:11.2f}%{mark}")

        check("取到足够多的帧", len(a_bar) >= 6, f"只有 {len(a_bar)} 帧")

        a_in = sum(a_bar[i] for i in inside) / max(1, len(inside))
        b_in = sum(b_bar[i] for i in inside) / max(1, len(inside))
        b_out = sum(b_bar[i] for i in outside) / max(1, len(outside))
        ctrl_a_in = sum(a_ctrl[i] for i in inside) / max(1, len(inside))
        ctrl_b_in = sum(b_ctrl[i] for i in inside) / max(1, len(inside))
        print(f"\n    均值：A 词内 {a_in * 100:.2f}% ｜ B 词内 {b_in * 100:.2f}% ｜ "
              f"B 词外 {b_out * 100:.2f}%")
        print(f"    均值（对照区）：A {ctrl_a_in * 100:.2f}% ｜ B {ctrl_b_in * 100:.2f}%")

        print()
        check("【基线】无歌词时字条区域没有暖色像素",
              max(a_bar.values()) < 0.002, f"最大 {max(a_bar.values()) * 100:.2f}%")
        check("【主判据】带歌词时字条区域出现暖色像素（卡拉OK 画上去了）",
              b_in > 0.005, f"词内均值 {b_in * 100:.2f}%")
        check("【主判据】歌词结束后暖色像素归零（字条消失）",
              b_out < 0.002, f"词外均值 {b_out * 100:.2f}%")
        check("跨版本：同一时段 B 明显比 A 亮出金色",
              b_in - a_in > 0.005, f"差 {(b_in - a_in) * 100:.2f} 个百分点")

        # 逐字填充：金色像素应随演唱推进而单调增长
        seq = [b_bar[i] for i in inside if i in b_bar]
        monotonic = all(seq[j + 1] >= seq[j] - 0.0015 for j in range(len(seq) - 1))
        print(f"    词内逐帧暖色序列：{[round(v * 100, 2) for v in seq]}%")
        check("【最强判据】金色像素随演唱单调增长（逐字填充在推进，不是整句静态字幕）",
              len(seq) >= 3 and monotonic and seq[-1] > seq[0] * 1.5,
              f"序列 {[round(v * 100, 2) for v in seq]}")

        # 只影响字条带，不影响画面中上部
        check("字条不侵占画面中上部（对照区两版一致）",
              abs(ctrl_b_in - ctrl_a_in) < 0.01,
              f"A {ctrl_a_in * 100:.2f}% vs B {ctrl_b_in * 100:.2f}%")

        print("\n" + "=" * 70)
        print(f"结果：{len(PASS)} 项通过，{len(FAIL)} 项失败")
        for f in FAIL:
            print(f"  FAIL: {f}")
        print("=" * 70)
        return 1 if FAIL else 0
    finally:
        if args.keep:
            print(f"\n产物保留在：{tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
