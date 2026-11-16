# -*- coding: utf-8 -*-
"""C5 对白优先（audio ducking）真实 FFmpeg 校验（约 10 秒）。

验的是"接口能返回"之外的事：

1. 参数层：开启时会接上 ``sidechaincompress``，关闭时链路与旧版完全一致；
   只有一轨（没有垫乐）时不接闪避；多轨时每条垫乐各有一把侧链键。
2. 声音层：用 200Hz 低音当音乐、3kHz 短音当人声，真实跑 ffmpeg 混音，
   再解码到 WAV 按窗口算 RMS——人声出现时音乐必须被压低，人声结束后必须恢复。
3. 时长层：侧链键补到整片长度，人声早于画面结束也不会把音乐尾巴截断
   （只验参数里有没有 apad 不够，必须看输出时长）。

分析窗口（5 秒素材）：
    A 0.20–0.80s  只有音乐          → 基准电平
    B 1.30–1.90s  人声正在说话      → 应被压低
    C 4.00–4.80s  人声早已结束      → 应恢复到基准附近

用法：
    python tools/verify_audio_duck.py
    python tools/verify_audio_duck.py --keep    # 保留 WAV 供人工试听
"""
from __future__ import annotations

import argparse
import math
import shutil
import struct
import sys
import tempfile
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline.compose import build_mix_args, mix_tracks  # noqa: E402
from pipeline.video import probe_duration, run_ffmpeg  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []
RATE = 44100
SECONDS = 5.0


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def tone(path: Path, freq: float, amp: float, windows: list[tuple[float, float]]) -> None:
    """写一段单声道 16bit WAV：只在给定时间窗内发声。"""
    frames = bytearray()
    for i in range(int(SECONDS * RATE)):
        t = i / RATE
        value = amp * math.sin(2 * math.pi * freq * t) if any(
            start <= t < end for start, end in windows) else 0.0
        frames += struct.pack("<h", int(max(-1.0, min(1.0, value)) * 32767))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(bytes(frames))


def decode(src: Path, dst: Path, lowpass: float | None = None,
           highpass: float | None = None) -> None:
    chain = []
    if lowpass:
        chain.append(f"lowpass=f={lowpass:.0f}")
    if highpass:
        chain.append(f"highpass=f={highpass:.0f}")
    args = ["-i", str(src)]
    if chain:
        args += ["-af", ",".join(chain)]
    args += ["-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dst)]
    run_ffmpeg(args)


def rms_window(path: Path, start: float, end: float) -> float:
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        first, last = int(start * rate), int(end * rate)
        handle.setpos(first)
        raw = handle.readframes(last - first)
    samples = struct.unpack(f"<{len(raw) // 2}h", raw)
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples)) / 32768.0


def band_rms(src: Path, tmp: Path, tag: str, lowpass: float | None,
             highpass: float | None, start: float, end: float) -> float:
    out = tmp / f"{tag}.wav"
    decode(src, out, lowpass=lowpass, highpass=highpass)
    return rms_window(out, start, end)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="保留产物供人工试听")
    args = parser.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="duck-verify-"))

    print("== 1. 参数层：闪避只在需要时接线 ==")
    plain = build_mix_args([(Path("v.wav"), 1.0, False), (Path("m.wav"), 0.28, True)],
                           Path("mix.m4a"), duration=5.0)
    ducked = build_mix_args([(Path("v.wav"), 1.0, False), (Path("m.wav"), 0.28, True)],
                            Path("mix.m4a"), duration=5.0, duck=True)
    plain_graph = plain[plain.index("-filter_complex") + 1]
    duck_graph = ducked[ducked.index("-filter_complex") + 1]
    check("关闭时链路没有 sidechaincompress", "sidechaincompress" not in plain_graph, plain_graph)
    check("开启时接入 sidechaincompress", duck_graph.count("sidechaincompress") == 1, duck_graph)
    check("侧链键补到整片长度（apad whole_dur）",
          "[raw0]aformat" in duck_graph and "apad=whole_dur=5.000" in duck_graph, duck_graph)
    check("人声仍以原轨入混（asplit 2）",
          "[a0]asplit=2[voice][raw0]" in duck_graph, duck_graph)
    single = build_mix_args([(Path("m.wav"), 0.28, True)], Path("mix.m4a"),
                            duration=5.0, duck=True)
    check("只有一轨时不接闪避",
          "sidechaincompress" not in single[single.index("-filter_complex") + 1], "")
    three = build_mix_args([(Path("v.wav"), 1.0, False), (Path("m1.wav"), 0.2, True),
                            (Path("m2.wav"), 0.2, True)], Path("mix.m4a"),
                           duration=5.0, duck=True)
    three_graph = three[three.index("-filter_complex") + 1]
    check("两条垫乐各自接一把侧链键",
          three_graph.count("sidechaincompress") == 2 and "[a0]asplit=3" in three_graph, three_graph)

    print("== 2. 声音层：真实混音后的电平变化 ==")
    music = tmp / "music.wav"
    voice = tmp / "voice.wav"
    # 200Hz 音乐铺满；3kHz 人声在 1–2s、3–3.6s 说话。
    tone(music, 200.0, 0.4, [(0.0, SECONDS)])
    tone(voice, 3000.0, 0.5, [(1.0, 2.0), (3.0, 3.6)])
    tracks = [(voice, 1.0, False), (music, 1.0, True)]

    off = mix_tracks(tracks, tmp / "off.m4a", SECONDS, fade=0.0, loudnorm=False, duck=False)
    on = mix_tracks(tracks, tmp / "on.m4a", SECONDS, fade=0.0, loudnorm=False, duck=True)

    def low(path: Path, tag: str, start: float, end: float) -> float:
        return band_rms(path, tmp, tag, 600.0, None, start, end)

    a_off, b_off, c_off = (low(off, "off-a", 0.2, 0.8), low(off, "off-b", 1.3, 1.9),
                           low(off, "off-c", 4.0, 4.8))
    a_on, b_on, c_on = (low(on, "on-a", 0.2, 0.8), low(on, "on-b", 1.3, 1.9),
                        low(on, "on-c", 4.0, 4.8))
    check("未开启时人声期间音乐电平基本不变",
          0.75 <= b_off / a_off <= 1.25, f"{a_off:.4f}/{b_off:.4f}")
    reduction = 20 * math.log10(max(b_on, 1e-9) / max(a_on, 1e-9))
    check("开启后人声期间音乐被压低（>6dB）",
          reduction < -6.0, f"{reduction:.1f}dB（A={a_on:.4f} B={b_on:.4f}）")
    recovery = 20 * math.log10(max(c_on, 1e-9) / max(a_on, 1e-9))
    check("人声结束后音乐恢复（±3dB 内）",
          -3.0 <= recovery <= 3.0, f"{recovery:.1f}dB（A={a_on:.4f} C={c_on:.4f}）")
    # 人声本身不能被闪避误伤：3kHz 能量在有声窗口必须明显高于无声窗口。
    hi_before = band_rms(on, tmp, "on-hi-a", None, 2500.0, 0.2, 0.8)
    hi_during = band_rms(on, tmp, "on-hi-b", None, 2500.0, 1.3, 1.9)
    check("人声轨仍完整入混（高频能量显著上升）",
          hi_during > hi_before * 5, f"{hi_before:.5f} → {hi_during:.5f}")

    print("== 3. 时长层：人声早结束不截断音乐 ==")
    duration = probe_duration(on)
    check("闪避输出仍为整片 5 秒", abs(duration - SECONDS) <= 0.15, f"{duration:.2f}s")
    tail = low(on, "on-tail", 4.4, 4.9)
    check("最后一秒仍有音乐（不是被压缩器截断）", tail > a_on * 0.6,
          f"tail={tail:.4f} ref={a_on:.4f}")

    print("== 4. 生产参数组合（淡入淡出 + 响度归一）能真实跑通 ==")
    prod = mix_tracks(tracks, tmp / "prod.m4a", SECONDS, fade=0.8, loudnorm=True, duck=True)
    prod_duration = probe_duration(prod)
    check("淡入淡出 + loudnorm + 闪避组合产出成功",
          prod.exists() and abs(prod_duration - SECONDS) <= 0.15,
          f"{prod_duration:.2f}s")

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
