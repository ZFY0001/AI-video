# -*- coding: utf-8 -*-
"""代码绘制后端的端到端验证脚本。

为什么单独一个脚本而不是塞进tests/：真实渲染一帧要 1 秒以上，15 秒片就是
360 帧 ≈ 7 分钟。放进单测会让整套测试从几秒变成十几分钟，没法接受。所以
"编译正确性"进单测，"真渲染"放这个脚本，需要时手动跑：

    python tools/verify_painted.py            # 全流程（含 MP4 合成）
    python tools/verify_painted.py --frames   # 只渲染帧，用 PIL 拼图代替 ffmpeg

--frames 模式存在的原因：受限终端/沙箱会禁止 FFmpeg 子进程访问磁盘
（报Permission denied，且对image2 demuxer 表现为误导性的 "could not open file"），
这时用 PIL 拼 contact sheet 一样能验证画面正确性。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import painted as pb  # noqa: E402
from pipeline.llm import CreativeLayer  # noqa: E402
from pipeline.prompts import PromptLibrary  # noqa: E402


def build_shots(theme: str):
    """用项目真实链路生成分镜（简报→剧本→分镜），不手写假数据。"""
    lib = PromptLibrary.load()
    layer = CreativeLayer(lib, None)  # 离线引擎：不依赖任何 Key
    brief = layer.make_brief(theme)
    script = layer.make_script(brief)
    board = layer.make_storyboard(brief, script)
    shots = [
        {"duration": s.duration, "narration": s.narration,
         "visual": s.visual, "camera": s.camera, "act": s.act}
        for s in board.shots
    ]
    return brief, script, shots


def contact_sheet(frames: list[Path], out: Path, cols: int = 3, w: int = 520) -> None:
    """用 PIL 拼关键帧图（绕开 ffmpeg）。"""
    from PIL import Image
    if not frames:
        print("  没有帧可拼")
        return
    picks = [frames[0], frames[len(frames) // 3], frames[2 * len(frames) // 3], frames[-1]]
    h = int(w * 9 / 16)
    rows = (len(picks) + cols - 1) // cols
    sheet = Image.new("RGB", (w * cols, h * rows), (30, 28, 35))
    for i, p in enumerate(picks):
        with Image.open(p) as im:
            sheet.paste(im.convert("RGB").resize((w, h), Image.LANCZOS),
                        ((i % cols) * w, (i // cols) * h))
    sheet.save(out, quality=88)
    print(f"  拼图已存：{out}（取 {[p.name for p in picks]}）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--theme", default="方城烩面宣传片")
    ap.add_argument("--fps", type=int, default=24)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--frames-only", action="store_true",
                    help="只渲染帧并用 PIL 拼图，不调 FFmpeg")
    args = ap.parse_args()

    print("=== 代码绘制后端 · 端到端验证 ===")
    st = pb.check_env()
    print(f"环境：{'就绪' if st.ok else '不可用'} · {st.detail}")
    if not st.ok:
        return 1

    brief, script, shots = build_shots(args.theme)
    total = sum(s["duration"] for s in shots)
    print(f"题材：{brief.style}")
    print(f"分镜：{len(shots)} 镜 / {total:.1f}s")
    for i, s in enumerate(shots):
        print(f"  镜{i+1} {s['duration']:.1f}s [{s['act']}] {s['camera']} · {s['narration'][:24]}")

    out_dir = ROOT / "work" / "painted_verify"
    t0 = time.time()
    res = pb.render_shots(shots, out_dir, genre_label="烟火美食", fps=args.fps,
                          workers=args.workers, log=lambda m: print(" ", m),
                          progress=lambda l: print("  >", l) if "ms/frame" in l else None,
                          # --frames-only：跳过 FFmpeg 合成。受限终端/沙箱会禁止
                          # ffmpeg 读盘（报 Permission denied），此时用 PIL 拼图验证画面。
                          encode=not args.frames_only)
    print(f"\n渲染耗时 {time.time() - t0:.0f}s")
    print(f"结果：ok={res['ok']} frames={res.get('frames', 0)} "
          f"ms/frame={res.get('ms_per_frame', 0)}")
    if not res["ok"] and not args.frames_only:
        print("错误：", res.get("error", ""))
        return 1

    frames = sorted((pb.work_dir() / "out" / "frames").glob("f*.jpg"))
    print(f"帧目录：{len(frames)} 帧")
    if args.frames_only:
        if not frames:
            print("错误：一帧都没渲出来")
            return 1
        contact_sheet(frames, ROOT / "work" / "painted_verify" / "sheet.jpg")
        print("（已跳过 MP4 合成：当前环境禁止 FFmpeg 访问磁盘）")
        return 0
    video = Path(res["video"])
    print(f"成片：{video}（{video.stat().st_size / 1048576:.2f} MB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
