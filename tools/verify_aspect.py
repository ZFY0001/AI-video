# -*- coding: utf-8 -*-
"""竖屏 / 方形画幅验证：画幅真的换了，而且字条、主体、情绪都没有跑偏。

**为什么单测不够**：画幅这条链横跨四个文件——`config.js`（下发宽高）、
`studio.html`（输出画布尺寸）、`core.js`（W/H 常量）、`karaoke.js`（字条基线）。
任何一环退回写死的 1920×1080，纯 Python 单测都发现不了，只有真渲染出来才看得见。
所以这里真渲一遍竖屏，直接量产物。

用法：
    python tools/verify_aspect.py                # 默认 9:16 / 3 秒 / 4fps
    python tools/verify_aspect.py --aspect 1:1 --seconds 3 --fps 4
    python tools/verify_aspect.py --keep
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageChops

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline import painted as pb  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []
#: 字条画在 KBAR_Y = H-56 上下，占底部约 5% 高度
BAR_BAND_RATIO = 0.07
WARM_DELTA = 40
WARM_MIN_R = 90


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def warm_fraction(path: Path, box: tuple[int, int, int, int]) -> float:
    im = Image.open(path).convert("RGB").crop(box)
    r, _g, b = im.split()
    warm = ImageChops.subtract(r, b).point(lambda v: 255 if v > WARM_DELTA else 0)
    bright = r.point(lambda v: 255 if v > WARM_MIN_R else 0)
    return ImageChops.multiply(warm, bright).histogram()[255] / float(im.width * im.height)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--aspect", default="9:16")
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument("--fps", type=int, default=4)
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    want = pb.size_for_aspect(args.aspect)
    print("=" * 72)
    print(f"画幅验证：{args.aspect} → 期望 {want[0]}x{want[1]}")
    print("=" * 72)
    print(f"\n[0] 画幅表")
    from pipeline.image_render import RESOLUTIONS
    check("画幅表与 image_render.RESOLUTIONS 一致",
          dict(pb.ASPECT_SIZES) == dict(RESOLUTIONS), f"{pb.ASPECT_SIZES} vs {RESOLUTIONS}")

    print(f"\n[1] 生成物是否跟随画幅")
    cfg = pb.build_config_js(args.seconds, fps=args.fps, size=want)
    check("config.js 下发画幅宽高",
          f"width: {want[0]}" in cfg and f"height: {want[1]}" in cfg, cfg.strip())
    html = pb.build_studio_html("src/scenes/yiyyan.js", karaoke=True, size=want)
    check("studio.html 输出画布尺寸跟随画幅",
          f'width="{want[0]}"' in html and f'height="{want[1]}"' in html, "")
    check("studio.html CSS aspect-ratio 跟随画幅",
          f"aspect-ratio: {want[0]} / {want[1]}" in html, "")
    core = (ROOT / "vendor" / "painted-template" / "src" / "core.js").read_text(encoding="utf-8")
    check("core.js 从 PROJECT 读画幅（不是写死 1920×1080）",
          "PROJECT.width" in core and "PROJECT.height" in core, "")
    kara = (ROOT / "vendor" / "painted-template" / "src" / "karaoke.js").read_text(encoding="utf-8")
    kcode = "\n".join(l for l in kara.splitlines() if not l.strip().startswith("//"))
    check("karaoke.js 字条基线由 H 算（写死 1024 会让竖屏字条跑到上半部）",
          "const KBAR_Y = H - 56" in kcode and "1024" not in kcode, "")
    check("karaoke.js 水平中心由 W 算", "const KCX = W / 2" in kcode, "")

    print(f"\n[2] 场景是否分辨率无关")
    shots = [{"duration": args.seconds, "act": "起", "narration": "",
              "visual": "开阔的水彩天空与远山", "camera": "固定机位，中景"}]
    js = pb.build_scene_js(shots, genre_label="城市宣传")
    check("相机中心用 W/H 全局（不是写死 960/540）",
          "camBegin(kf(t, [[0, W * 0.5]" in js and "960.0" not in js and "540.0" not in js, "")
    check("绝对像素尺寸乘短边换算 U", "const U = Math.min(W, H) / 1080" in js, "")
    # 生成的 JS 语法：这一条被真事故逼出来过（{{ }} 原样输出，只有渲染时才炸）
    try:
        node = pb._node_exe()
    except Exception:  # noqa: BLE001
        node = None
    if node:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "c.js"
            p.write_text(cfg + "const W=1920,H=1080;\n" + js, encoding="utf-8")
            r = subprocess.run([node, "--check", str(p)], capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            check("生成的场景 JS 语法正确（node --check）", r.returncode == 0,
                  (r.stderr or "")[-200:])

    print(f"\n[3] 情绪与机位真的接到了引擎")
    import re
    cw = (ROOT / "vendor" / "painted-template" / "src" / "clawd.js").read_text(encoding="utf-8")
    seg = cw.split("const EMO = {", 1)[1].split("\n};", 1)[0]
    keys = set(re.findall(r"^\s{2}(\w+):\s*\{", seg, re.M))
    four = [(a, pb._mood_for(a, i)) for i, a in enumerate(("起", "承", "转", "合"))]
    bad = [(a, m) for a, m in four if m not in keys]
    check(f"四幕情绪都在引擎 EMO 表内（共 {len(keys)} 键，越界会静默变 neutral）",
          not bad, f"表外的：{bad}")
    check("四幕情绪互不相同（否则每镜表情雷同）",
          len({m for _, m in four}) == 4, str(four))
    check("clawd 调用把 feel() 与 {sq,view} 合并成一个对象（第 5 个参数会被丢弃）",
          "clawd(x, groundY - bob, u, { ...feel(mood, t), sq, view:" in js, "")

    print(f"\n[4] 渲染环境")
    st = pb.check_env()
    print(f"  {'就绪' if st.ok else '不可用'} · {st.detail}")
    if not st.ok:
        check("渲染环境可用", False, st.detail)
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="aspect_"))
    frames_dir = pb.work_dir() / "out" / "frames"
    try:
        t0, t1 = max(0.2, args.seconds * 0.2), max(0.4, args.seconds * 0.7)
        print(f"\n[5] 真渲一遍：{args.seconds}s @ {args.fps}fps，歌词 [{t0:.2f},{t1:.2f}]")
        out = pb.render_shots(
            shots, tmp / "out", genre_label="城市宣传", fps=args.fps, workers=2,
            log=lambda m: print("    · " + m),
            lyrics=[{"start": t0, "end": t1, "text": "夜色落在窗台上"}],
            size=want,
        )
        check("渲染成功", bool(out.get("ok")), str(out.get("error", ""))[:200])
        frames = sorted(frames_dir.glob("f*.jpg"))
        check("取到渲染帧", len(frames) >= 4, f"{len(frames)} 帧")
        if frames:
            im = Image.open(frames[0])
            check(f"帧尺寸就是 {want[0]}x{want[1]}（不是被拉伸或裁切的 16:9）",
                  im.size == want, f"实际 {im.size}")
            w, h = im.size
            band = (0, int(h * (1 - BAR_BAND_RATIO)), w, h)
            inside = [i for i in range(len(frames)) if t0 <= i / args.fps < t1]
            outside = [i for i in range(len(frames)) if not (t0 <= i / args.fps < t1)]
            f_in = [warm_fraction(frames[i], band) for i in inside] or [0.0]
            f_out = [warm_fraction(frames[i], band) for i in outside] or [0.0]
            a_in, a_out = sum(f_in) / len(f_in), sum(f_out) / len(f_out)
            print(f"    底部 {BAR_BAND_RATIO:.0%} 高度带内的暖色像素（字条是金色）")
            print(f"    词内均值 {a_in * 100:.2f}% ｜ 词外均值 {a_out * 100:.2f}%")
            check("歌词时间内底部出现金色字条",
                  a_in > 0.004, f"词内 {a_in * 100:.2f}%")
            check("歌词时间外底部没有字条（说明字条不是常驻）",
                  a_out < a_in * 0.5, f"词内 {a_in * 100:.2f}% vs 词外 {a_out * 100:.2f}%")
            # 字条必须在**下半部**：写死 1024 的旧版在竖屏会落到 53% 高度处
            top = (0, 0, w, int(h * 0.5))
            top_in = sum(warm_fraction(frames[i], top) for i in inside) / max(1, len(inside))
            check("字条不在画面上半部（写死 1024 的旧实现会跑到 53% 处）",
                  top_in < 0.02 or top_in < a_in * 2,
                  f"上半部 {top_in * 100:.2f}% vs 底部带 {a_in * 100:.2f}%")
        # 编码产物分辨率
        if out.get("ok") and out.get("video"):
            probe = ROOT / "bin" / "ffprobe.exe"
            if probe.exists():
                r = subprocess.run(
                    [str(probe), "-v", "error", "-select_streams", "v:0",
                     "-show_entries", "stream=width,height", "-of", "csv=p=0",
                     str(out["video"])],
                    capture_output=True, text=True, encoding="utf-8", errors="replace")
                # ffprobe 的 csv 输出可能带尾随逗号（多流/空字段），按数字解析更稳
                nums = [t.strip() for t in (r.stdout or "").replace("\n", ",").split(",")
                        if t.strip()]
                check(f"成片分辨率就是 {want[0]}x{want[1]}",
                      nums[:2] == [str(want[0]), str(want[1])],
                      f"实际 {nums[:2]}")
        print("\n" + "=" * 72)
        print(f"结果：{len(PASS)} 项通过，{len(FAIL)} 项失败（画幅 {want[0]}x{want[1]}）")
        for f in FAIL:
            print(f"  FAIL: {f}")
        print("=" * 72)
        return 1 if FAIL else 0
    finally:
        if args.keep:
            print(f"\n产物保留在：{tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
