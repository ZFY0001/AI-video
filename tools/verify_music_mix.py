# -*- coding: utf-8 -*-
"""音乐混音端到端验证：amix 真的混了、音乐真的被压低、音轨永不长于/短于画面。

为什么用「两个纯音 + FFT」而不是只看参数：
参数里有 `amix` 不等于混音真的成立——声道布局不一致、`normalize` 把一轨压没、
`-t` 提前截断，这些都会让结果听不出问题、却查不出原因。用 440Hz（人声）与
1200Hz（音乐）两个可辨识频率混音，再对输出做 FFT：两个峰都在 = 真的共存；
音乐峰明显低于人声峰 = 垫底音量生效。

同时守住一个致命方向：`mux_audio` 用 `-shortest`，**音轨一旦短于画面就会把画面
截断**。所以这里专门验「音乐比片子短」时混音结果仍等于整片长度。

用法：
    python tools/verify_music_mix.py
    python tools/verify_music_mix.py --keep
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline.compose import (  # noqa: E402
    BGM_GAIN,
    MUSIC_FADE_SECONDS,
    build_mix_args,
    mix_tracks,
)
from pipeline.video import probe_duration  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []

VOICE_HZ = 440.0
MUSIC_HZ = 1200.0


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def ffmpeg() -> str:
    local = ROOT / "bin" / "ffmpeg.exe"
    return str(local) if local.exists() else (shutil.which("ffmpeg") or "ffmpeg")


def tone(path: Path, hz: float, seconds: float) -> Path:
    subprocess.run(
        [ffmpeg(), "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", f"sine=frequency={hz}:duration={seconds}", "-c:a", "aac",
         "-b:a", "192k", str(path)],
        capture_output=True, text=True,
    )
    return path


def spectrum(path: Path, at: float = 0.5, window: float = 0.4) -> tuple[np.ndarray, np.ndarray]:
    """取 `at` 秒附近 `window` 秒的音频做 FFT，返回 (频率轴, 幅度谱)。"""
    wav = path.with_suffix(".probe.wav")
    subprocess.run(
        [ffmpeg(), "-y", "-loglevel", "error", "-ss", f"{at:.3f}", "-t", f"{window:.3f}",
         "-i", str(path), "-ac", "1", "-ar", "44100", "-f", "wav", str(wav)],
        capture_output=True, text=True,
    )
    with wave.open(str(wav), "rb") as fh:
        frames = fh.readframes(fh.getnframes())
        rate = fh.getframerate()
    wav.unlink(missing_ok=True)
    sig = np.frombuffer(frames, dtype="<i2").astype(float)
    if sig.size < 1024:
        return np.array([]), np.array([])
    sig = sig * np.hanning(sig.size)
    spec = np.abs(np.fft.rfft(sig))
    freqs = np.fft.rfftfreq(sig.size, 1.0 / rate)
    return freqs, spec


def peak_near(freqs: np.ndarray, spec: np.ndarray, hz: float, tol: float = 25.0) -> float:
    """取 hz 附近的最大幅度（容忍编码引入的轻微频偏）。"""
    if freqs.size == 0:
        return 0.0
    band = (freqs >= hz - tol) & (freqs <= hz + tol)
    return float(spec[band].max()) if band.any() else 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    print("=" * 68)
    print("音乐混音端到端验证（amix / 垫底音量 / 音轨长度）")
    print("=" * 68)

    tmp = Path(tempfile.mkdtemp(prefix="music_mix_"))
    try:
        voice = tone(tmp / "voice.m4a", VOICE_HZ, 6.0)     # 模拟人声
        music = tone(tmp / "music.m4a", MUSIC_HZ, 2.0)     # 音乐故意比片子短

        # ---------- A) 人声 + 音乐 ----------
        print(f"\n[A] 人声 {VOICE_HZ:.0f}Hz + 音乐 {MUSIC_HZ:.0f}Hz（音乐只有 2s，片长 6s）")
        out = tmp / "mix.m4a"
        mix_tracks([(voice, 1.0, False), (music, BGM_GAIN, True)], out, 6.0,
                   fade=MUSIC_FADE_SECONDS, loudnorm=True)
        dur = probe_duration(out)
        check("混音产物存在", out.exists(), str(out))
        check("混音时长等于整片长度（音乐短→循环铺满，不截断画面）",
              abs(dur - 6.0) < 0.3, f"{dur:.3f}s")

        freqs, spec = spectrum(out, at=2.5)
        v = peak_near(freqs, spec, VOICE_HZ)
        m = peak_near(freqs, spec, MUSIC_HZ)
        check(f"人声 {VOICE_HZ:.0f}Hz 在输出里存在（amix 真的混了）", v > 0, f"幅度 {v:.1f}")
        check(f"音乐 {MUSIC_HZ:.0f}Hz 在输出里同时存在（两轨共存）", m > 0, f"幅度 {m:.1f}")
        ratio = (m / v) if v > 0 else 0.0
        check("音乐被压到人声之下（垫底音量生效）",
              0.05 < ratio < 0.75, f"音乐/人声 = {ratio:.3f}（目标 {BGM_GAIN}）")
        print(f"    实测：人声峰 {v:.0f} / 音乐峰 {m:.0f} → 比值 {ratio:.3f}"
              f"（期望 ≈ {BGM_GAIN}）")

        # ---------- B) 单轨音乐（MV 模式）----------
        print(f"\n[B] 只有音乐一轨（MV 模式），片长 6s、音乐 2s")
        solo = tmp / "solo.m4a"
        mix_tracks([(music, BGM_GAIN, True)], solo, 6.0,
                   fade=MUSIC_FADE_SECONDS, loudnorm=True)
        dur2 = probe_duration(solo)
        check("单轨混音时长也等于整片长度", abs(dur2 - 6.0) < 0.3, f"{dur2:.3f}s")
        f2, s2 = spectrum(solo, at=2.5)
        check("单轨混音保留音乐频率", peak_near(f2, s2, MUSIC_HZ) > 0, "")

        # ---------- C) 音乐比片子长：应被截到片长 ----------
        print(f"\n[C] 音乐 6s、片长 2.5s（音乐比片子长）")
        long_music = tmp / "long.m4a"
        tone(long_music, MUSIC_HZ, 6.0)
        cut = tmp / "cut.m4a"
        mix_tracks([(long_music, BGM_GAIN, True)], cut, 2.5, fade=0.0, loudnorm=True)
        dur3 = probe_duration(cut)
        check("音乐比片子长时截到片长", abs(dur3 - 2.5) < 0.3, f"{dur3:.3f}s")

        # ---------- D) 参数层面的自检 ----------
        print("\n[D] 参数自检")
        cargs = build_mix_args([(voice, 1.0, False), (music, BGM_GAIN, True)],
                               tmp / "x.m4a", duration=6.0,
                               fade=MUSIC_FADE_SECONDS, loudnorm=True)
        fc = cargs[cargs.index("-filter_complex") + 1]
        check("命令里确实有 amix", "amix=inputs=2" in fc, "")
        check("命令里确实有 loudnorm", "loudnorm=" in fc, "")
        check("输出被 -t 限定", "-t" in cargs and "6.000" in cargs, "")
        check("音乐轨带 -stream_loop -1", cargs.count("-stream_loop") == 1, "")

        print("\n" + "=" * 68)
        print(f"结果：{len(PASS)} 项通过，{len(FAIL)} 项失败")
        for f in FAIL:
            print(f"  FAIL: {f}")
        print("=" * 68)
        return 1 if FAIL else 0
    finally:
        if args.keep:
            print(f"\n产物保留在：{tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
