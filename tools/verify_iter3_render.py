# -*- coding: utf-8 -*-
"""第三次迭代 · 合成链真实出片验证（真 ffmpeg，约 30 秒）。

守两条本轮修过、但此前只跑过隔离素材的链路：

1. **画面长度是唯一权威**。旧实现用 `-shortest`，配音比画面短时会把**画面**
   一起截断，成片凭空少几秒而时长台账仍按拼接后长度记。这里用「3 秒画面 +
   1 秒配音」和「3 秒画面 + 6 秒配音」两个方向各验一次。
2. **人工改过的字幕必须进成片**。字幕只在这里叠卡，改字只需重合成。这里给
   每条字幕一张纯色块，按时间抽帧采样，确认 0.5/1.5/2.5 秒各是自己的颜色——
   只验「SRT 文本对不对」发现不了「叠卡没生效」。

用法：
    python tools/verify_iter3_render.py
    python tools/verify_iter3_render.py --keep    # 保留产物供人工查看
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

from pipeline.compose import compose  # noqa: E402
from pipeline.models import Shot, Storyboard  # noqa: E402
from pipeline.tts import parse_srt  # noqa: E402
from pipeline.video import probe_duration, run_ffmpeg  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []

W, H = 320, 180
BLOCK = (20, 20, 120, 60)          # 字幕色块：左上角 (20,20)，100×40
SAMPLE = (70, 40)                  # 采样点落在色块中间


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    shown = detail if (detail and not ok) else ""
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {shown}" if shown else ""))


def make_clip(path: Path, seconds: float) -> None:
    run_ffmpeg([
        "-f", "lavfi", "-i", f"color=c=0x202020:s={W}x{H}:d={seconds}:r=25",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
    ])


def make_tone(path: Path, seconds: float) -> None:
    run_ffmpeg([
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-c:a", "aac", "-b:a", "128k", str(path),
    ])


def make_layer(path: Path, rgb: tuple[int, int, int]) -> None:
    image = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    for x in range(BLOCK[0], BLOCK[2]):
        for y in range(BLOCK[1], BLOCK[3]):
            image.putpixel((x, y), (*rgb, 255))
    image.save(path, "PNG")


def sample_at(video: Path, seconds: float, out_dir: Path) -> tuple[int, int, int]:
    frame = out_dir / f"probe-{seconds:.2f}.png"
    run_ffmpeg(["-ss", f"{seconds:.3f}", "-i", str(video), "-frames:v", "1", "-y", str(frame)])
    with Image.open(frame) as image:
        rgb = image.convert("RGB")
        return rgb.getpixel(SAMPLE)


def near(rgb: tuple[int, int, int], expect: tuple[int, int, int], tol: int = 70) -> bool:
    return all(abs(a - b) <= tol for a, b in zip(rgb, expect))


def build_board() -> Storyboard:
    shots = [
        Shot(id=1, act="一", duration=1.0, narration="原来的第一句", visual="v1", camera="c1"),
        Shot(id=2, act="一", duration=1.0, narration="原来的第二句", visual="v2", camera="c2"),
        Shot(id=3, act="二", duration=1.0, narration="原来的第三句", visual="v3", camera="c3"),
    ]
    board = Storyboard(project={"title": "回填验证", "total_duration": 3.0}, shots=shots)
    board.assign_timeline()
    return board


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true", help="保留产物目录")
    args = parser.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="yiyan-i3render-"))
    try:
        work = tmp / "work"
        work.mkdir()
        (tmp / "media").mkdir()

        clips = []
        for index, tone in enumerate(((255, 0, 255), (0, 255, 255), (255, 255, 0)), start=1):
            clip = tmp / "media" / f"c{index}.mp4"
            make_clip(clip, 1.0)
            clips.append(clip)
            make_layer(tmp / "media" / f"t{index}.png", tone)

        layers = [
            (tmp / "media" / f"t{index}.png", float(index - 1), float(index))
            for index in (1, 2, 3)
        ]
        edited = [
            {"start": 0.0, "end": 1.0, "text": "改过的第一句"},
            {"start": 1.0, "end": 2.0, "text": "改过的第二句"},
            {"start": 2.0, "end": 3.0, "text": "改过的第三句"},
        ]
        short_voice = tmp / "media" / "voice_short.m4a"
        long_voice = tmp / "media" / "voice_long.m4a"
        make_tone(short_voice, 1.0)
        make_tone(long_voice, 6.0)

        # ---- 1. 配音短于画面：画面不能被截断 ----
        board = build_board()
        out_a = tmp / "out_a"
        res_a = compose(board, clips, out_a, work, audio=short_voice,
                        subtitle_lines=edited, text_layers=layers, subtitle_mode="burn")
        dur_a = probe_duration(Path(res_a.video))
        check("配音短于画面时成片仍是 3 秒", abs(dur_a - 3.0) < 0.3, f"{dur_a}")
        check("成片时长与台账一致",
              abs((res_a.duration or 0) - board.total_duration) < 0.3,
              f"成片 {res_a.duration} / 台账 {board.total_duration}")
        check("配音短于画面时仍有音轨", bool(res_a.has_audio), str(res_a.audio_source))
        srt_a = parse_srt((out_a / "subtitles.srt").read_text(encoding="utf-8"))
        check("人工字幕原样写成 SRT",
              [row["text"] for row in srt_a] == ["改过的第一句", "改过的第二句", "改过的第三句"],
              str([row["text"] for row in srt_a]))
        colors = [sample_at(Path(res_a.video), t, tmp) for t in (0.5, 1.5, 2.5)]
        check("字幕卡按时间窗叠进成片",
              near(colors[0], (255, 0, 255)) and near(colors[1], (0, 255, 255))
              and near(colors[2], (255, 255, 0)), str(colors))

        # ---- 2. 配音长于画面：以画面为准，音轨被截 ----
        out_b = tmp / "out_b"
        res_b = compose(build_board(), clips, out_b, work, audio=long_voice,
                        subtitle_mode="none")
        dur_b = probe_duration(Path(res_b.video))
        check("配音长于画面时以画面为准", abs(dur_b - 3.0) < 0.3, f"{dur_b}")
        check("明确不要字幕时不产出 SRT",
              not (out_b / "subtitles.srt").exists() and not res_b.subtitle, str(res_b.subtitle))

        # ---- 3. 没有配音：铺等长静音轨 ----
        out_c = tmp / "out_c"
        res_c = compose(build_board(), clips, out_c, work, audio=None, subtitle_mode="none")
        dur_c = probe_duration(Path(res_c.video))
        check("无配音时铺等长静音轨", abs(dur_c - 3.0) < 0.3, f"{dur_c}")
        check("静音兜底不冒充有音频", not res_c.has_audio, str(res_c.audio_source))

        # ---- 4. 母版原子落盘：不留临时文件 ----
        check("合成结束不留临时母版",
              not (out_a / ".final.pending.mp4").exists()
              and not (out_b / ".final.pending.mp4").exists(), "")

        print(f"\n结果：{len(PASS)} 项通过，{len(FAIL)} 项失败")
        if args.keep:
            print(f"产物保留在：{tmp}")
        return 0 if not FAIL else 1
    finally:
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
