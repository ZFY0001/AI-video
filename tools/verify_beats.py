# -*- coding: utf-8 -*-
"""节拍端到端验证：估计准确度、按拍切镜、以及"引擎真的收到节拍"。

三件事分开验，因为它们的失败方式完全不同：

1. **估计本身**：合成已知 BPM/首拍的点击轨，看能不能估回真值。
   同时验一条**纯正弦**（没有节拍）——它必须被判为"不可靠"，
   否则说明置信度形同虚设，管线会拿一个假网格去切镜头。
2. **按拍切镜**：把一条时间轴的切点吸附到拍点，断言吸附后
   「总时长不变」「每镜不低于下限」「切点确实落在拍上」。
3. **引擎接线**：真渲染一小段（只出帧），读 `vendor/painted-template/src/config.js`，
   断言 `PROJECT.bpm/offset` 是**真实值**而不是默认的 96.0 / 0。
   这一步守的是"死参数"——以前 bpm 恒 96、offset 恒 0，引擎照样跑，
   但角色动作跟音乐毫无关系。

用法：
    python tools/verify_beats.py
    python tools/verify_beats.py --fps 6 --seconds 3     # 渲染部分更快
"""
from __future__ import annotations

import argparse
import math
import struct
import sys
import tempfile
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline import painted as pb  # noqa: E402
from pipeline.beats import estimate_beats, manual_grid, tempo_prior  # noqa: E402
from pipeline.models import MIN_SHOT_SECONDS, Shot, build_timeline  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def write_wav(path: Path, buf: list[float], sr: int = 8000) -> Path:
    data = b"".join(struct.pack("<h", int(max(-1.0, min(1.0, v)) * 32000)) for v in buf)
    with wave.open(str(path), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(sr)
        fh.writeframes(data)
    return path


def click_track(path: Path, bpm: float, offset: float, seconds: float, sr: int = 8000) -> Path:
    """点击轨：底鼓式低频音头 + 高音木鱼，更像真实鼓点。"""
    step = 60.0 / bpm
    n = int(seconds * sr)
    buf = [0.0] * n
    t = offset
    k_i = 0
    while t < seconds:
        i0 = int(t * sr)
        for k in range(int(0.05 * sr)):
            i = i0 + k
            if 0 <= i < n:
                env = math.exp(-k / (0.010 * sr))
                bass = math.sin(2 * math.pi * (95 - 40 * k / (0.05 * sr)) * k / sr)
                tick = math.sin(2 * math.pi * 1800 * k / sr)
                buf[i] += (bass * 0.9 + tick * 0.35) * env
        # 每两拍补一个弱拍，制造"强弱"结构
        if k_i % 2 == 1:
            j0 = int((t + step / 2) * sr)
            for k in range(int(0.02 * sr)):
                i = j0 + k
                if 0 <= i < n:
                    buf[i] += math.sin(2 * math.pi * 2400 * k / sr) * math.exp(-k / (0.004 * sr)) * 0.25
        t += step
        k_i += 1
    return write_wav(path, buf, sr)


def sine_track(path: Path, seconds: float, sr: int = 8000) -> Path:
    n = int(seconds * sr)
    return write_wav(path, [math.sin(2 * math.pi * 330 * i / sr) * 0.6 for i in range(n)], sr)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fps", type=int, default=6, help="渲染验证用的帧率")
    ap.add_argument("--seconds", type=float, default=3.0, help="渲染验证用的时长")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    print("=" * 70)
    print("节拍端到端验证（估计 / 按拍切镜 / 引擎接线）")
    print("=" * 70)

    tmp = Path(tempfile.mkdtemp(prefix="beats_"))
    try:
        # ---------- 1. 估计准确度 ----------
        print("\n[1] 估计准确度（合成已知真值的点击轨）")
        cases = [(120.0, 0.25), (100.0, 0.20), (140.0, 0.10), (75.0, 0.40), (160.0, 0.15)]
        worst_bpm = 0.0
        worst_phase = 0.0
        for bpm, off in cases:
            p = click_track(tmp / f"click{int(bpm)}.wav", bpm, off, 16.0)
            g = estimate_beats(p)
            err = abs(g.bpm - bpm)
            phase = min(abs(g.offset - off), abs(abs(g.offset - off) - g.interval))
            worst_bpm = max(worst_bpm, err)
            worst_phase = max(worst_phase, phase)
            print(f"    真值 {bpm:6.1f}BPM/{off:.2f}s → 估计 {g.bpm:7.2f}BPM/{g.offset:.3f}s "
                  f"conf={g.confidence:.2f}  误差 {err:.2f}BPM / 相位 {phase:.3f}s")
            check(f"{bpm:.0f}BPM 估计误差 < 2", err < 2.0, f"{err:.2f}")
            check(f"{bpm:.0f}BPM 首拍相位误差 < 40ms", phase < 0.04, f"{phase:.3f}s")
        print(f"    汇总：最大 BPM 误差 {worst_bpm:.2f}，最大相位误差 {worst_phase:.3f}s")

        # 反向用例：纯正弦没有节拍，必须被判为不可靠
        silent_beats = sine_track(tmp / "sine.wav", 16.0)
        sg = estimate_beats(silent_beats)
        print(f"    纯正弦（无节拍）→ {sg.bpm:.1f}BPM / conf={sg.confidence:.2f}")
        check("纯正弦被判为不可靠（置信度低）", sg.confidence < 0.25,
              f"conf={sg.confidence}（若这里通过不了，说明置信度形同虚设）")

        # ---------- 2. 按拍切镜 ----------
        print("\n[2] 按拍切镜（切点吸附到拍点）")
        bpm, off = 100.0, 0.20
        grid = manual_grid(bpm, off)
        beats = grid.beats_until(20.0)
        # 0.2 起、间隔 0.6、到 20.0 → 0.2+0.6k ≤ 20，k 最大 33，共 34 个
        check("手工网格给出预期拍点数", len(beats) == 34, str(len(beats)))

        shots = [Shot(id=i + 1, act="起", duration=d, narration="", visual="", camera="")
                 for i, d in enumerate([3.3, 3.1, 3.5, 3.5, 3.1, 3.5])]
        before = [round(s.duration, 3) for s in shots]
        build_timeline(shots, 20.0, beats=None)
        free_starts = [round(s.start, 3) for s in shots]
        # 复位后重排，这次带拍点
        shots = [Shot(id=i + 1, act="起", duration=d, narration="", visual="", camera="")
                 for i, d in enumerate([3.3, 3.1, 3.5, 3.5, 3.1, 3.5])]
        build_timeline(shots, 20.0, beats=beats)
        snapped_starts = [round(s.start, 3) for s in shots]
        print(f"    拍点间隔 {grid.interval:.3f}s ｜ 不吸附切点 {free_starts}")
        print(f"                              吸附后切点 {snapped_starts}")
        check("吸附后总时长不变", abs(shots[-1].end - 20.0) < 0.01, str(shots[-1].end))
        check("吸附后每镜不低于下限",
              all(s.duration >= MIN_SHOT_SECONDS - 1e-9 for s in shots),
              str([s.duration for s in shots]))

        def on_beat(t: float) -> bool:
            k = (t - off) / grid.interval
            return abs(k - round(k)) < 1e-6

        snapped_on = [s.start for s in shots[1:] if on_beat(s.start)]
        check("吸附后中间切点全部落在拍点上",
              len(snapped_on) == len(shots) - 1,
              f"{len(snapped_on)}/{len(shots) - 1} 个在拍上：{snapped_starts}")
        check("不吸附时切点不在拍上（说明吸附确实起了作用）",
              not all(on_beat(t) for t in free_starts[1:]), str(free_starts))

        # 反向：切点离最近拍点超出容差时**不该**硬吸（否则某一镜会被拉得离谱）
        wide = [Shot(id=i + 1, act="起", duration=2.2, narration="", visual="", camera="")
                for i in range(4)]
        build_timeline(wide, 8.8, beats=[0.0, 5.0, 10.0])
        check("离拍点超出容差时不硬吸", abs(wide[0].duration - 2.2) < 1e-6,
              str([s.duration for s in wide]))

        # ---------- 3. 引擎接线：config.js 里是真实节拍 ----------
        print(f"\n[3] 引擎接线（真渲染 {args.seconds}s @ {args.fps}fps，只出帧）")
        st = pb.check_env()
        if not st.ok:
            check("渲染环境可用", False, st.detail)
        else:
            render_shots_data = [
                {"duration": args.seconds, "act": "起", "narration": "验证节拍",
                 "visual": "一片开阔的水彩天空", "camera": "缓慢推镜，中景",
                 "acceptance": "画面统一"},
            ]
            res = pb.render_shots(render_shots_data, tmp / "painted",
                                  genre_label="城市宣传", fps=args.fps, workers=2,
                                  encode=False, log=lambda m: print("    · " + m),
                                  bpm=137.5, beat_offset=0.42)
            check("渲染完成（只出帧）", bool(res.get("ok")), str(res.get("error", ""))[:200])
            cfg = (pb.work_dir() / "src" / "config.js").read_text(encoding="utf-8")
            print(f"    生成的 config.js：{cfg.strip().splitlines()[-2].strip()}")
            check("PROJECT.bpm 是真实值 137.5（此前恒 96.0）", "bpm: 137.5" in cfg, cfg[:160])
            check("PROJECT.offset 是真实值 0.42（此前恒 0）", "offset: 0.42" in cfg, cfg[:160])
            check("config.js 仍含 duration（引擎靠它算总帧数）", "duration:" in cfg, cfg[:160])
            check("tempo_prior 在 137.5BPM 处仍有效", tempo_prior(137.5) > 0.8, "")

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
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
