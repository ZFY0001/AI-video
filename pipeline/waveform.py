"""音频峰值：给字幕/时间轴校准用的波形数据。

为什么不是前端直接 `<audio>` + WebAudio 解码：音轨在服务端项目目录里，
浏览器拿到的是需要鉴权的路径；而且每次打开拆解页都把一首 222 秒的歌
重解码一遍，明显比读一份几 KB 的峰值缓存贵。

解码成 8kHz 单声道只为画图：波形不需要高频细节，单声道取绝对值包络
足以看清断句与鼓点；采样率越低，长音频的内存与耗时越可控。
"""

from __future__ import annotations

import array
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

from .video import FFMPEG

#: 峰值桶数上下限。200 是"还能看出段落"的最低密度；4000 已经超过
#: 常见屏幕宽度，再高只是让 JSON 变大。
MIN_BUCKETS = 200
MAX_BUCKETS = 4000
DEFAULT_BUCKETS = 1200
SAMPLE_RATE = 8000


def clamp_buckets(value: int | None, default: int = DEFAULT_BUCKETS) -> int:
    try:
        parsed = int(value if value is not None else default)
    except (TypeError, ValueError):
        parsed = default
    return max(MIN_BUCKETS, min(MAX_BUCKETS, parsed))


def fingerprint(path: Path) -> str:
    """音轨指纹：路径 + 大小 + 修改时间。换歌/重传都会让缓存失效。"""
    stat = path.stat()
    raw = f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _compute(path: Path, buckets: int) -> dict:
    proc = subprocess.run(
        [
            FFMPEG, "-hide_banner", "-loglevel", "error",
            "-i", str(path),
            "-map", "0:a:0", "-ac", "1", "-ar", str(SAMPLE_RATE),
            "-f", "s16le", "-",
        ],
        capture_output=True,
        timeout=120,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip()[:300]
        raise RuntimeError(f"FFmpeg 解码音轨失败：{detail or f'退出码 {proc.returncode}'}")

    raw = proc.stdout or b""
    raw = raw[: len(raw) - (len(raw) % 2)]
    samples = array.array("h")
    samples.frombytes(raw)
    if sys.byteorder != "little":
        samples.byteswap()
    total = len(samples)
    if total == 0:
        raise RuntimeError("音轨没有可解码的采样")

    peaks: list[float] = []
    for index in range(buckets):
        low = total * index // buckets
        high = max(low + 1, total * (index + 1) // buckets)
        chunk = samples[low:high]
        if not chunk:
            peaks.append(0.0)
            continue
        peaks.append(max(max(chunk), -min(chunk)) / 32768.0)

    loudest = max(peaks) or 1.0
    return {
        "duration": round(total / SAMPLE_RATE, 3),
        "buckets": buckets,
        # 归一化到本音轨峰值：安静的歌也能看清结构；绝对峰值另给一份，
        # 免得界面把"很轻的一段"误当成"没有声音"。
        "peaks": [round(value / loudest, 4) for value in peaks],
        "peak": round(loudest, 4),
        "sample_rate": SAMPLE_RATE,
    }


def peaks(path: Path, cache_dir: Path, buckets: int | None = None) -> dict:
    """返回峰值数据，命中缓存时直接读盘。

    缓存按桶数一份、以音轨指纹校验：换歌、重传同名文件都会重算；
    桶数不同则各存一份，不会因为用户拉宽窗口就反复重解码。
    """
    count = clamp_buckets(buckets)
    stamp = fingerprint(path)
    cache = cache_dir / f"wave-{count}.json"
    if cache.is_file():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict) and data.get("fingerprint") == stamp:
            return {**data, "cached": True}

    data = _compute(path, count)
    data["fingerprint"] = stamp
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp = cache_dir / f".{cache.name}.{time.time_ns()}.tmp"
    try:
        tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        tmp.replace(cache)
    except OSError:
        # 缓存写不进去不影响本次返回：下次最多再算一遍。
        tmp.unlink(missing_ok=True)
    return {**data, "cached": False}
