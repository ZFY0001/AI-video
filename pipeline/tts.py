"""一言片场 · 配音与字幕。

配音 provider：
- SAPIProvider  ：Windows 自带语音合成（本机有 Microsoft Huihui 中文音色），零依赖、离线可用
- CloudTTSProvider：云端 TTS（未配置则拒绝）
- SilentProvider：纯静音轨，兜底

字幕：按镜头时间轴生成 SRT（UTF-8 BOM，Windows 播放器/剪映兼容）。
"""

from __future__ import annotations

import shutil
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .common import srt_time
from .models import Storyboard, caption_of
from .video import ffmpeg_available, run_ffmpeg

# 每个镜头的配音最长允许拉伸倍数（超过就靠语速，不硬拉，避免听感怪）
MAX_TEMPO = 1.35
MIN_TEMPO = 0.85


class TTSError(RuntimeError):
    pass


class TTSProvider(Protocol):
    name: str

    def available(self) -> tuple[bool, str]: ...

    def speak(self, text: str, out_wav: Path) -> Path: ...


@dataclass
class SAPIProvider:
    """Windows SAPI 语音合成。输出 WAV（16kHz 单声道），再由 FFmpeg 统一重采样。"""

    voice_hint: str = "Chinese"
    rate: int = 0
    volume: int = 100
    name: str = "sapi"

    def available(self) -> tuple[bool, str]:
        import importlib.util

        if importlib.util.find_spec("win32com") is None:
            return False, "缺少 pywin32"
        try:
            desc = self._voices()
        except Exception as exc:  # noqa: BLE001
            return False, f"SAPI 不可用：{exc}"
        if not desc:
            return False, "系统没有安装语音"
        chosen = self._pick(desc)
        return True, f"音色：{chosen}"

    def _voices(self) -> list[str]:
        import win32com.client

        voice = win32com.client.Dispatch("SAPI.SpVoice")
        return [v.GetDescription() for v in voice.GetVoices()]

    def _pick(self, voices: list[str]) -> str:
        for v in voices:
            if self.voice_hint.lower() in v.lower():
                return v
        return voices[0]

    def speak(self, text: str, out_wav: Path) -> Path:
        if not text.strip():
            raise TTSError("空文本不合成")
        import win32com.client

        out_wav.parent.mkdir(parents=True, exist_ok=True)
        stream = win32com.client.Dispatch("SAPI.SpFileStream")
        # SSFMCreateForWrite = 3
        stream.Open(str(out_wav), 3, False)
        voice = win32com.client.Dispatch("SAPI.SpVoice")
        for v in voice.GetVoices():
            if self.voice_hint.lower() in v.GetDescription().lower():
                voice.Voice = v
                break
        voice.Rate = self.rate
        voice.Volume = self.volume
        old = voice.AudioOutputStream
        voice.AudioOutputStream = stream
        try:
            voice.Speak(text)
        finally:
            voice.AudioOutputStream = old
            stream.Close()
        return out_wav


@dataclass
class CloudTTSProvider:
    base_url: str
    api_key: str
    voice: str = ""
    name: str = "cloud-tts"

    def available(self) -> tuple[bool, str]:
        if not self.base_url or not self.api_key:
            return False, "未配置云端 TTS（TTS_BASE_URL / TTS_API_KEY）"
        return False, "云端 TTS 适配器待接入"

    def speak(self, text: str, out_wav: Path) -> Path:
        raise TTSError("云端 TTS 未启用")


@dataclass
class SilentProvider:
    """静音轨兜底：保证成片一定有时长的音轨，剪辑软件不会报错。"""

    name: str = "silent"

    def available(self) -> tuple[bool, str]:
        return ffmpeg_available()

    def speak(self, text: str, out_wav: Path) -> Path:
        raise TTSError("静音 provider 不合成语音")


def build_tts_chain(settings) -> list[TTSProvider]:
    chain: list[TTSProvider] = []
    engine = (settings.get("TTS_ENGINE") or "auto").lower()
    cloud = CloudTTSProvider(base_url=settings.get("TTS_BASE_URL"), api_key=settings.get("TTS_API_KEY"))
    sapi = SAPIProvider()
    if engine in ("auto", "cloud"):
        ok, _ = cloud.available()
        if ok:
            chain.append(cloud)
    if engine in ("auto", "sapi", "local"):
        ok, _ = sapi.available()
        if ok:
            chain.append(sapi)
    chain.append(SilentProvider())
    return chain


# ------------------------------------------------------------------ 音频处理
def wav_duration(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as fh:
            rate = fh.getframerate() or 1
            return fh.getnframes() / rate
    except Exception:  # noqa: BLE001
        return 0.0


def fit_audio(src: Path, out_path: Path, target: float) -> tuple[Path, float, str]:
    """把一段配音对齐到镜头时长：太长就限速压缩，太短就补静音。

    返回 (路径, 最终时长, 处理说明)。**不改变镜头时长**——时间轴以分镜为准。
    """
    dur = wav_duration(src)
    if dur <= 0:
        raise TTSError(f"配音文件无有效时长：{src}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    note = "原速"
    if dur > target:
        tempo = min(MAX_TEMPO, dur / target)
        if tempo > 1.001:
            run_ffmpeg(
                ["-i", str(src), "-filter:a", f"atempo={tempo:.4f}",
                 "-ar", "44100", "-ac", "1", "-c:a", "pcm_s16le", str(out_path)]
            )
            note = f"压缩 {tempo:.2f}×"
        else:
            shutil.copyfile(src, out_path)
    elif dur < target - 0.05:
        pad = target - dur
        run_ffmpeg(
            ["-i", str(src), "-af", f"apad=pad_dur={pad:.3f}",
             "-ar", "44100", "-ac", "1", "-c:a", "pcm_s16le", str(out_path)]
        )
        note = f"补静音 {pad:.2f}s"
    else:
        shutil.copyfile(src, out_path)
    return out_path, wav_duration(out_path), note


def concat_audio(parts: list[Path], out_path: Path) -> Path:
    if not parts:
        raise TTSError("没有可拼接的音频")
    inputs: list[str] = []
    for p in parts:
        inputs += ["-i", str(p)]
    filt = "".join(f"[{i}:a]" for i in range(len(parts))) + f"concat=n={len(parts)}:v=0:a=1[out]"
    run_ffmpeg(
        [*inputs, "-filter_complex", filt, "-map", "[out]",
         "-ar", "44100", "-ac", "2", "-c:a", "aac", "-b:a", "192k", str(out_path)]
    )
    return out_path


def silence(path: Path, duration: float) -> Path:
    run_ffmpeg(
        ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
         "-t", f"{max(0.1, duration):.3f}", "-c:a", "aac", "-b:a", "192k", str(path)]
    )
    return path


# ------------------------------------------------------------------ 字幕
def write_srt(board: Storyboard, out_path: Path, offset: float = 0.0) -> Path:
    """按镜头时间轴写 SRT。UTF-8 带 BOM，Windows 端兼容性最好。

    ``offset``：整体平移字幕（秒），用于校对外挂字幕与画面/声音的偏差。
    平移后夹在 [0, 总时长] 内——提前过多会得到负起点，延后过多会超出成片，
    两者都会让播放器显示异常。
    """
    limit = float(board.total_duration or 0.0)
    lines: list[str] = []
    idx = 1
    for shot in board.shots:
        text = caption_of(shot)
        if not text:
            continue
        span = _shift_span(shot.start, shot.end, offset, limit)
        if span is None:
            continue
        start, end = span
        lines.append(str(idx))
        lines.append(f"{srt_time(start)} --> {srt_time(end)}")
        lines.append(_wrap(text, 18))
        lines.append("")
        idx += 1
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8-sig")
    return out_path


def write_srt_lines(lines: list[dict], out_path: Path, offset: float = 0.0,
                    limit: float = 0.0) -> Path:
    """按用户编辑过的字幕轨写 SRT。

    与 `write_srt` 共用同一套时间夹取规则：整体平移不能产生负时间，
    也不能超出成片。人工编辑允许一行比镜头长，但仍不能越过片尾。
    """
    body: list[str] = []
    idx = 1
    for line in lines:
        text = str(line.get("text") or "").strip()
        if not text:
            continue
        span = _shift_span(line.get("start") or 0.0, line.get("end") or 0.0,
                           offset, limit)
        if span is None:
            continue
        start, end = span
        body.append(str(idx))
        body.append(f"{srt_time(start)} --> {srt_time(end)}")
        body.append(_wrap(text, 18))
        body.append("")
        idx += 1
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(body), encoding="utf-8-sig")
    return out_path


def _shift_span(start: float, end: float, offset: float, limit: float
                ) -> tuple[float, float] | None:
    """平移并夹取一条字幕；整条落在有效区间之外时返回 None。"""
    start = float(start) + offset
    end = float(end) + offset
    if end <= 0:
        return None
    start = max(0.0, start)
    if limit > 0:
        if start >= limit:
            return None
        end = min(end, limit)
    if end <= start:
        return None
    return start, end


def parse_srt(raw: str) -> list[dict]:
    """解析 SRT 为 ``[{start, end, text}]``（秒）。

    容错优先：时间行不规范、缺序号、多行文本都尽量读出来——它服务于
    "编辑已经产出的字幕"，读失败就等于用户的字幕突然不见了。
    """
    import re

    # 带 BOM 的 SRT 里，第一行序号是 "\ufeff1"。BOM 不算空白，strip() 去不掉，
    # isdigit() 也就认不出它是序号——结果序号被当成正文拼进第一句字幕。
    raw = raw.replace("\ufeff", "")

    def seconds(stamp: str) -> float | None:
        match = re.match(r"\s*(\d+):(\d{2}):(\d{2})[,.](\d{1,3})", stamp)
        if not match:
            return None
        h, m, s, ms = match.groups()
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000

    lines: list[dict] = []
    for block in raw.replace("\r", "").split("\n\n"):
        rows = [row for row in block.split("\n") if row.strip()]
        if not rows:
            continue
        time_row = next((row for row in rows if "-->" in row), "")
        if not time_row:
            continue
        left, _, right = time_row.partition("-->")
        start, end = seconds(left), seconds(right)
        text = " ".join(row.strip() for row in rows if "-->" not in row and not row.strip().isdigit())
        if start is None or end is None or end <= start or not text:
            continue
        lines.append({"start": round(start, 3), "end": round(end, 3), "text": text})
    return lines


def srt_to_lrc(raw: str) -> str:
    """SRT → LRC。LRC 只有行首时间戳，适合把成片字幕交给播放器/剪辑软件。"""
    def stamp(seconds: float) -> str:
        total = max(0, int(round(seconds * 100)))
        return f"[{total // 6000:02d}:{total % 6000 // 100:02d}.{total % 100:02d}]"

    return "\n".join(f"{stamp(line['start'])}{line['text']}"
                     for line in parse_srt(raw)) + "\n"


def _wrap(text: str, per_line: int) -> str:
    t = text.strip()
    if len(t) <= per_line:
        return t
    mid = len(t) // 2
    # 尽量在标点处断行
    for offset in range(0, min(6, mid)):
        for pos in (mid - offset, mid + offset):
            if 0 < pos < len(t) and t[pos - 1] in "，。；、！？,.;":
                return t[:pos].strip() + "\n" + t[pos:].strip()
    return t[:mid] + "\n" + t[mid:]
