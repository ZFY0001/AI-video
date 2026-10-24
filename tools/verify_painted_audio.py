# -*- coding: utf-8 -*-
"""代码绘制后端 · 音轨与帧缓存端到端验证（真渲染，约 1 分钟）。

守两条实际踩过的坑，每条都对应一次真实缺陷：

1. **成片没有音轨**。编码命令原来只有图像输入（``-i f%05d.jpg``），所以勾选
   「代码绘制」后配音整体丢失，而界面与运行小结照样显示「配音：有」。
   → 现在必须验证成片里真的有一条 aac 音轨，且 ``has_audio`` 如实上报。

2. **旧帧被静默复用**。``render.mjs:122`` 只在「文件存在且 >1000 字节」时跳过
   渲染，于是上一次的帧被当成本次的用——实测「请求 15 秒却产出 18 秒、画面还是
   旧的」，两个验证脚本都因此假通过。
   → 现在必须验证「换了片子 → 旧帧被清掉 → 成片时长等于本次请求」。

用法：
    python tools/verify_painted_audio.py                    # 默认 4 秒 / 12fps
    python tools/verify_painted_audio.py --seconds 3 --fps 8 --workers 4
    python tools/verify_painted_audio.py --keep             # 保留产物供人工查看
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline import painted as pb  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def ffmpeg() -> str:
    local = ROOT / "bin" / "ffmpeg.exe"
    return str(local) if local.exists() else (shutil.which("ffmpeg") or "ffmpeg")


def ffprobe() -> str:
    local = ROOT / "bin" / "ffprobe.exe"
    return str(local) if local.exists() else (shutil.which("ffprobe") or "ffprobe")


def probe(path: Path) -> dict:
    out = subprocess.run(
        [ffprobe(), "-v", "error", "-show_entries",
         "format=duration:stream=codec_type,codec_name,width,height,sample_rate",
         "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    try:
        return json.loads(out.stdout or "{}")
    except json.JSONDecodeError:
        return {}


def make_tone(path: Path, seconds: float) -> Path:
    """生成一段确定的测试音（440Hz 正弦），用来证明音轨真的并进了成片。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [ffmpeg(), "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", f"sine=frequency=440:duration={seconds}", "-c:a", "aac",
         "-b:a", "192k", str(path)],
        capture_output=True, text=True,
    )
    return path


def shots_for(seconds: float, n: int = 2, tag: str = "A") -> list[dict]:
    each = seconds / n
    return [
        {"duration": each, "act": "起" if i == 0 else "承",
         "narration": f"这是一句测试解说词，编号 {tag}{i + 1}。",
         "visual": f"测试画面 {tag}{i + 1}：一片开阔的水彩天空与远山",
         "camera": "缓慢推镜，中景" if i % 2 == 0 else "固定机位，特写",
         "acceptance": "主体清晰，色调统一"}
        for i in range(n)
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=4.0)
    ap.add_argument("--fps", type=int, default=12)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--keep", action="store_true", help="保留产物目录")
    args = ap.parse_args()

    print("=" * 66)
    print("代码绘制 · 音轨与帧缓存端到端验证")
    print("=" * 66)

    st = pb.check_env()
    print(f"\n[0] 渲染环境：{'就绪' if st.ok else '不可用'} · {st.detail}")
    if not st.ok:
        print("    环境不满足，无法验证。")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="painted_audio_"))
    try:
        # 期望帧数：build 内部会把时长对齐到 0.5 秒
        duration = max(3.0, round(args.seconds * 2) / 2)
        expect_frames = int(round(duration * args.fps))

        print(f"\n[1] 首次渲染（无签名 + 模板里可能有旧帧）"
              f"\n    请求 {args.seconds}s → 实际 {duration}s / {args.fps}fps → 期望 {expect_frames} 帧")
        tone = make_tone(tmp / "tone.m4a", duration)
        t0 = time.time()
        r1 = pb.render_shots(
            shots_for(duration, tag="A"), tmp / "out1",
            genre_label="烟火美食", fps=args.fps, workers=args.workers,
            log=lambda m: print("    · " + m),
            audio=tone,
        )
        dt1 = time.time() - t0
        print(f"    返回：ok={r1.get('ok')} frames={r1.get('frames')} "
              f"has_audio={r1.get('has_audio')} 耗时 {dt1:.0f}s")
        if not r1.get("ok"):
            check("首次渲染成功", False, str(r1.get("error", ""))[:300])
            return 1
        check("首次渲染成功", True)

        # ---- 音轨断言（Step 1 的验收）----
        v1 = Path(r1["video"])
        check("成片文件存在", v1.exists(), str(v1))
        info = probe(v1)
        kinds = [s.get("codec_type") for s in info.get("streams", [])]
        check("成片含视频流", "video" in kinds, str(kinds))
        check("成片含音轨（Step 1：配音不再丢失）", "audio" in kinds, f"实际流：{kinds}")
        acodec = next((s.get("codec_name") for s in info.get("streams", [])
                       if s.get("codec_type") == "audio"), "")
        check("音轨编码为 aac", acodec == "aac", acodec)
        check("has_audio 如实上报为 True", r1.get("has_audio") is True, str(r1.get("has_audio")))
        check("返回里带上实际音轨路径", bool(r1.get("audio")), str(r1.get("audio")))

        # ---- 帧缓存断言（Step 2 的验收）----
        dur1 = float(info.get("format", {}).get("duration", 0) or 0)
        check(f"成片时长等于本次请求（{duration}s，不是旧帧的时长）",
              abs(dur1 - duration) < 0.35, f"实际 {dur1:.2f}s")
        check(f"帧数等于本次请求（{expect_frames}）", r1.get("frames") == expect_frames,
              f"实际 {r1.get('frames')}")

        # ---- 换片子：旧帧必须被清掉，时长跟着变 ----
        print(f"\n[2] 换片子（不同分镜/时长）→ 旧帧必须被清掉，不能串片")
        second = duration + 1.5
        tone2 = make_tone(tmp / "tone2.m4a", second)
        r2 = pb.render_shots(
            shots_for(second, n=3, tag="B"), tmp / "out2",
            genre_label="城市宣传", fps=args.fps, workers=args.workers,
            log=lambda m: print("    · " + m),
            audio=tone2,
        )
        check("第二次渲染成功", bool(r2.get("ok")), str(r2.get("error", ""))[:200])
        if r2.get("ok"):
            info2 = probe(Path(r2["video"]))
            dur2 = float(info2.get("format", {}).get("duration", 0) or 0)
            want2 = max(3.0, round(second * 2) / 2)
            check(f"换片后时长等于新请求（{want2}s，证明旧帧已清）",
                  abs(dur2 - want2) < 0.35, f"实际 {dur2:.2f}s")
            check("换片后帧数等于新请求",
                  r2.get("frames") == int(round(want2 * args.fps)), f"实际 {r2.get('frames')}")
            kinds2 = [s.get("codec_type") for s in info2.get("streams", [])]
            check("换片后仍带音轨", "audio" in kinds2, str(kinds2))

        print("\n" + "=" * 66)
        print(f"结果：{len(PASS)} 项通过，{len(FAIL)} 项失败")
        for f in FAIL:
            print(f"  FAIL: {f}")
        print("=" * 66)
        return 1 if FAIL else 0
    finally:
        if args.keep:
            print(f"\n产物保留在：{tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
