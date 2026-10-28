# -*- coding: utf-8 -*-
"""节拍估计：从音频里找出 BPM 与首拍（offset）。

**为什么自己写而不是用 librosa**：项目的运行依赖刻意只有标准库 + Pillow + PyYAML
（见 requirements.txt），为的是一句"能出片"的桌面软件不该背上几十 MB 的科学计算栈，
打包成 EXE 后体积还会直接翻倍。这里用经典三步，全部纯 Python：

1. 用随项目自带的 FFmpeg 把音频解码成 8kHz 单声道 16-bit PCM
2. 按 10ms 一帧算均方根能量，对相邻帧的对数能量取**正向差分**得到 onset 强度
   （只取正向：乐音起始是能量骤增，衰减不该被算成新的起音）
3. 在 60-180 BPM 范围内对 onset 包络做自相关找周期（BPM），
   再枚举相位找「拍点上的 onset 之和」最大的位置（首拍 offset）

**全程无随机数**：同一条音频永远给同样的 BPM/offset，可复现、可断言。

诚实的局限（写在前面，避免过度承诺）：

- 只对**有明确鼓点/节拍**的音乐可靠；自由速度、纯氛围、古典乐可能估不准
- 自相关天生有倍频/半频歧义（这段是 60 还是 120？），这里用"常见区间偏好"裁决，
  但**不保证**与人耳一致
- 所以置信度会被报出来，管线据此决定是否采信；用户也可以用 ``--bpm`` 手工覆盖
"""

from __future__ import annotations

from .proc import run as run_hidden

import array
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: 解码采样率。做能量包络不需要高采样率，8kHz 足够且解码快、内存小。
SAMPLE_RATE = 8000
#: 能量帧长（秒）。5ms 一帧 → 拍点粒度 ±2.5ms。
#: 曾用 10ms：在合成点击轨上相位误差可达 0.1s（BPM 略偏会沿拍序累积），
#: 加密后相位误差降到 0.03s 以内；实测 14 秒音频分析仅 0.14s，4 分钟歌约 2.5s，值得。
FRAME_SECONDS = 0.005
#: BPM 搜索范围。太窄会漏掉偏快的电子乐，太宽会引入倍频误判。
MIN_BPM = 60.0
MAX_BPM = 180.0
#: 倍频歧义时的偏好中心：同等强度的候选里优先靠拢它。
PREFERRED_BPM = 120.0
#: 判定"这是一段有节奏的音乐"所需的最少起音数。
#: 纯音/长音垫只有开头一个瞬态，此时 onset 包络几乎是零向量，自相关的
#: "置信度"会荒谬地接近 1——不查这一条，一段纯正弦会被报成 120BPM / conf=1.00。
MIN_ONSETS = 6
#: 峰值起音 / 平均对数能量的下限。
#: 这是最可靠的一条判据：起音必须是"能量真的跳了一下"，而不是恒定能量上的抖动。
#: 实测（12 秒素材）——纯正弦 330Hz/523Hz 得 0.0067 / 0.0029，
#: 点击轨 120/100BPM 得 13.08 / 15.71，两侧相差约 2000 倍，余量极大。
#: 对比之下"安静帧比例"只有 0.50 vs 0.99 的两倍余量，不够稳，故弃用。
MIN_ONSET_TO_LEVEL = 0.25
#: 判定"这一帧有起音"的相对阈值（仅用于数起音个数）。
ONSET_ACTIVE_RATIO = 0.20


class BeatError(RuntimeError):
    """节拍估计失败（通常是解码不成功）。"""


@dataclass(frozen=True)
class BeatGrid:
    """一条音频的节拍网格。``bpm<=0`` 表示没有可用网格。"""

    bpm: float = 0.0
    offset: float = 0.0
    confidence: float = 0.0
    source: str = "none"          # estimated | manual | none

    @property
    def interval(self) -> float:
        return 60.0 / self.bpm if self.bpm > 0 else 0.0

    @property
    def ok(self) -> bool:
        return self.bpm > 0.0 and self.interval > 0.0

    def beats_until(self, total: float) -> list[float]:
        """列出 [0, total] 内的拍点时刻。"""
        if not self.ok or total <= 0:
            return []
        out: list[float] = []
        t = self.offset
        # offset 只保证落在 [0, interval) 内，但手工覆盖可能给到负数
        while t < 0:
            t += self.interval
        while t <= total + 1e-9:
            out.append(round(t, 4))
            t += self.interval
        return out

    def nearest(self, t: float) -> float:
        """离 t 最近的拍点。没有网格时原样返回。"""
        if not self.ok:
            return t
        k = round((t - self.offset) / self.interval)
        return self.offset + k * self.interval

    def to_dict(self) -> dict[str, Any]:
        return {
            "bpm": round(self.bpm, 2),
            "offset": round(self.offset, 3),
            "confidence": round(self.confidence, 3),
            "source": self.source,
        }


EMPTY = BeatGrid()


def decode_pcm(path: Path, ffmpeg: str | None = None) -> bytes:
    """把任意音频解码成 8kHz 单声道 16-bit LE PCM。"""
    if ffmpeg is None:
        from .video import FFMPEG as _FFMPEG

        ffmpeg = _FFMPEG
    proc = run_hidden(
        [str(ffmpeg), "-v", "error", "-i", str(path),
         "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"],
        capture_output=True,
        # **必须显式 text=False**：输出是原始 PCM 字节流，被解码成 str 就废了
        # （array.frombytes 只收 bytes）。proc.run 默认 text=True 是给文本场景的。
        text=False,
        timeout=600,
    )
    if proc.returncode != 0:
        # proc.run 现在统一带 text=True，stderr 已经是 str；但仍兼容 bytes 的情况
        # （万一某处直接调 subprocess，或走了别的 helper）
        raw = proc.stderr or ""
        err = (raw if isinstance(raw, str) else raw.decode("utf-8", "replace")).strip()[:300]
        raise BeatError(f"解码失败（{proc.returncode}）：{err}")
    return proc.stdout


def onset_envelope(pcm: bytes, frame_seconds: float = FRAME_SECONDS) -> list[float]:
    """把 PCM 变成 onset 强度序列（每帧一个值）。"""
    return _envelope_and_level(pcm, frame_seconds)[0]


def _envelope_and_level(
    pcm: bytes, frame_seconds: float = FRAME_SECONDS
) -> tuple[list[float], float]:
    """返回 (onset 强度序列, 平均对数能量)。

    平均对数能量是判断"起音是不是真的能量跳变"的参照——纯音的 RMS 会因
    帧长与波形周期不整除而轻微起伏，产生一堆**假的** onset；有了这个参照
    就能一眼看出那只是 0.5% 的抖动，而不是真正敲了一下。
    """
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    n = len(samples)
    step = max(1, int(SAMPLE_RATE * frame_seconds))
    if n < step * 4:
        return [], 0.0

    logs: list[float] = []
    for start in range(0, n - step + 1, step):
        acc = 0
        for v in samples[start:start + step]:
            acc += v * v
        # +1.0 防止静音段 log(0)
        logs.append(math.log(math.sqrt(acc / step) + 1.0))

    onset = [0.0]
    for i in range(1, len(logs)):
        onset.append(max(0.0, logs[i] - logs[i - 1]))
    return onset, (sum(logs) / len(logs) if logs else 0.0)


def tempo_prior(bpm: float) -> float:
    """对数-高斯节拍先验（Ellis 2007）。

    没有它时自相关会**系统性地偏向更慢的倍频**：0.75 秒的 lag 与 0.375 秒的 lag
    在点击轨上相关度几乎一样，而 `acc/(n-lag)` 的归一化又会给大 lag 加分，
    于是 160BPM 的曲子稳定被报成 80BPM（实测）。先验以 PREFERRED_BPM 为中心、
    宽度约 1 个八度，把"人耳更常感知到的那个速度"抬上来。
    """
    if bpm <= 0:
        return 0.0
    sigma = 1.0
    return math.exp(-0.5 * (math.log2(bpm / PREFERRED_BPM) / sigma) ** 2)


def phase_score(
    onset: list[float], interval: float, frame_seconds: float = FRAME_SECONDS
) -> tuple[float, float]:
    """给定拍周期，返回 (最佳相位对应首拍时刻, 该相位下的平均 onset 强度)。

    这个"梳状分数"回答的是真正该问的问题：**这个节拍网格能解释多少起音**。
    比只看自相关峰更可靠——自相关在整数 lag 上采样，真实周期一旦不是整数帧
    （160BPM 是 37.5 帧），单周期只能取到部分相关，而双周期恰好完全对齐，
    于是大 lag 反而得分更高（实测 160BPM 被稳定报成 80BPM）。
    """
    n = len(onset)
    if n < 4 or interval <= 0:
        return 0.0, 0.0
    period = interval / frame_seconds
    if period < 1.0:
        return 0.0, 0.0

    best_phase, best_score = 0.0, -1.0
    for p_int in range(int(math.ceil(period))):
        acc = 0.0
        count = 0
        pos = float(p_int)
        while pos < n:
            idx = int(round(pos))
            # ±1 帧容差窗：真实周期常不是整数帧（96BPM 是 62.5 帧），
            # 拍点位置 round 出来的下标会与起音 spike 差一帧，
            # 不取窗口的话"精确网格"反而取不到起音，导致选到完全无关的周期。
            hit = 0.0
            for j in (idx - 1, idx, idx + 1):
                if 0 <= j < n and onset[j] > hit:
                    hit = onset[j]
            acc += hit
            count += 1
            pos += period
        if count and acc / count > best_score:
            best_score, best_phase = acc / count, float(p_int)
    return best_phase * frame_seconds, best_score


def estimate_tempo(
    onset: list[float],
    frame_seconds: float = FRAME_SECONDS,
    level: float | None = None,
) -> tuple[float, float]:
    """找拍周期，返回 (bpm, 置信度)。估不出来返回 (0.0, 0.0)。

    ``level``：平均对数能量（来自 :func:`_envelope_and_level`）。给定时会先检查
    "起音是不是真的能量跳变"，这是识破纯音/氛围垫的关键一步——没有它，
    恒定能量上的微小抖动会被当成起音，自相关照样给出一个周期、置信度还很高。

    两步走：自相关给出**候选周期**（便宜，但只在整数 lag 上采样），
    再用 phase_score 的梳状分数 + 对数-高斯先验在候选与其倍频/半频里**裁决**。
    只做自相关会在真实周期非整数帧时系统性地选到倍频（慢一倍）。
    """
    n = len(onset)
    if n < 8:
        return 0.0, 0.0
    peak_onset = max(onset)
    if peak_onset <= 0:
        return 0.0, 0.0
    # 起音必须是"能量真的跳了一下"，不是恒定能量上的抖动
    if level is not None and peak_onset < MIN_ONSET_TO_LEVEL * level:
        return 0.0, 0.0
    # 起音太少 → 这不是有节奏的音乐
    if sum(1 for v in onset if v > ONSET_ACTIVE_RATIO * peak_onset) < MIN_ONSETS:
        return 0.0, 0.0
    lag_min = max(1, int(round((60.0 / MAX_BPM) / frame_seconds)))
    lag_max = min(n - 1, int(round((60.0 / MIN_BPM) / frame_seconds)))
    if lag_max <= lag_min:
        return 0.0, 0.0

    mean = sum(onset) / n
    z = [v - mean for v in onset]
    energy = sum(v * v for v in z) / n
    if energy <= 0:
        return 0.0, 0.0

    raw: dict[int, float] = {}
    for lag in range(lag_min, lag_max + 1):
        acc = 0.0
        for i in range(n - lag):
            acc += z[i] * z[i + lag]
        raw[lag] = acc / (n - lag)

    # 候选：自相关最强的若干 lag，及其倍频/半频（倍频歧义就藏在这一步里）
    top = sorted(raw, key=lambda k: raw[k], reverse=True)[:8]
    cands: set[float] = set()
    for lag in top:
        for mult in (1.0, 2.0, 0.5):
            cand = lag * mult
            if lag_min <= cand <= lag_max:
                cands.add(cand)

    best: tuple[float, float, float, float] | None = None
    for cand in cands:
        interval = cand * frame_seconds
        offset, score = phase_score(onset, interval, frame_seconds)
        bpm = 60.0 / interval
        weighted = score * tempo_prior(bpm)
        if best is None or weighted > best[0]:
            best = (weighted, bpm, offset, score)

    if best is None:
        return 0.0, 0.0
    _w, bpm, _off, _score = best

    # 置信度取离所选周期最近的整数 lag 的自相关强度（衡量周期性本身，不受先验影响），
    # 并用抛物线插值补偿非整数帧带来的低估。
    lag = 60.0 / (bpm * frame_seconds)
    near = int(round(lag))
    peak = raw.get(near, 0.0)
    if lag_min < near < lag_max:
        y0, y1, y2 = raw[near - 1], raw[near], raw[near + 1]
        denom = y0 - 2.0 * y1 + y2
        if abs(denom) > 1e-12:
            delta = 0.5 * (y0 - y2) / denom
            if -1.0 < delta < 1.0:
                peak = y1 - 0.25 * (y0 - y2) * delta
    conf = max(0.0, min(1.0, peak / energy if energy > 0 else 0.0))
    return bpm, conf


def estimate_offset(
    onset: list[float], interval: float, frame_seconds: float = FRAME_SECONDS
) -> float:
    """找让「拍点上的 onset 之和」最大的相位，返回首拍时刻（秒）。"""
    return phase_score(onset, interval, frame_seconds)[0]


def estimate_beats(path: Path, ffmpeg: str | None = None) -> BeatGrid:
    """完整估计：解码 → onset → BPM → 首拍。失败抛 BeatError，估不出返回 EMPTY。"""
    onset, level = _envelope_and_level(decode_pcm(Path(path), ffmpeg))
    bpm, conf = estimate_tempo(onset, level=level)
    if bpm <= 0:
        return EMPTY
    interval = 60.0 / bpm
    offset = estimate_offset(onset, interval)
    return BeatGrid(bpm=round(bpm, 2), offset=round(offset, 3),
                    confidence=round(conf, 3), source="estimated")


def manual_grid(bpm: float, offset: float = 0.0) -> BeatGrid:
    """用户手工指定的节拍（自动估计不准时的兜底）。"""
    if bpm <= 0:
        return EMPTY
    return BeatGrid(bpm=float(bpm), offset=max(0.0, float(offset)),
                    confidence=1.0, source="manual")
