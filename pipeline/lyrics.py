# -*- coding: utf-8 -*-
"""歌词解析与对齐。

MV 模式的上屏文字应该是**歌词**，不是解说词。这个模块负责两件事：

1. **解析**。LRC（``[mm:ss.xx]词``，含 ``[offset:]`` 元信息）优先；纯文本按行取用。
   同一行可以挂多个时间戳（``[00:01.00][00:05.00]同一句``），
   这是副歌重复的常见写法，必须展开成多条而不是丢掉。

2. **对齐**。每条歌词得到 ``[start, end)`` 区间。LRC 用自带时间戳；
   纯文本按**字数加权**铺满整首（长句多分时间），有节拍网格时再把边界吸到拍上。

**诚实的局限**（写在前面，避免过度承诺）：

- 纯文本歌词没有时间信息，字数加权只能保证"大致均匀、长句更久"，
  **不保证与人声逐句对齐**；要精确必须给 LRC 或带时间戳的歌词。
- 一条歌词跨多个镜头时，非卡拉OK 路径每镜只能显示一条，
  所以取"起点落在该镜头内"的那条（见 :func:`assign_captions`）。
  逐字填充由 Step 9 的 karaoke.js 负责。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: ``[mm:ss.xx]`` / ``[mm:ss:xx]`` / ``[mm:ss]``；分钟数允许三位（超长音频）
_LRC_TIME = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
#: ``[ti:标题]`` 之类的元信息标签
_LRC_META = re.compile(r"^\[([a-zA-Z_]+):(.*)\]$")
#: 纯文本里要跳过的行（制作人员名单之类）
_SKIP_LINE = re.compile(r"^\s*(作词|作曲|编曲|制作人|演唱|混音|母带|op|sp|出品|监制)\s*[:：]", re.I)


@dataclass(frozen=True)
class LyricLine:
    """一条歌词及其上屏区间（秒）。"""

    start: float
    end: float
    text: str

    @property
    def span(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> dict[str, Any]:
        return {"start": self.start, "end": self.end, "text": self.text,
                "span": round(self.span, 3)}


def _parse_fraction(frac: str | None) -> float:
    """把 ``.xx`` / ``.xxx`` 解析成秒的小数部分。"""
    if not frac:
        return 0.0
    return int(frac) / (10 ** len(frac))


def parse_lrc(text: str) -> tuple[list[tuple[float, str]], float]:
    """解析 LRC，返回 ``([(时刻秒, 词)], offset 秒)``。

    ``offset`` 语义按 LRC 惯例：**正值把歌词整体提前**（``有效时刻 = 原始时刻 - offset``）。
    注意这个标签在野生 LRC 里两种相反解释都存在，所以这里显式注明所用语义。
    """
    entries: list[tuple[float, str]] = []
    offset = 0.0
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        meta = _LRC_META.match(line)
        if meta and not _LRC_TIME.match(line):
            if meta.group(1).lower() == "offset":
                try:
                    offset = float(meta.group(2).strip()) / 1000.0
                except ValueError:
                    offset = 0.0
            continue                      # 其余元信息（ti/ar/al/by）直接忽略
        stamps = list(_LRC_TIME.finditer(line))
        if not stamps:
            continue
        body = line[stamps[-1].end():].strip()
        if not body:
            continue
        for m in stamps:
            minute = int(m.group(1))
            second = int(m.group(2))
            t = minute * 60.0 + second + _parse_fraction(m.group(3))
            entries.append((t, body))
    entries.sort(key=lambda e: e[0])
    return entries, offset


def plain_lines(text: str) -> list[str]:
    """从文本里取歌词行：丢掉时间戳、元信息标签与制作人员名单。"""
    out: list[str] = []
    for raw in (text or "").splitlines():
        line = _LRC_TIME.sub("", raw).strip()
        if not line or _LRC_META.match(line) or _SKIP_LINE.match(line):
            continue
        out.append(line)
    return out


def align_plain(
    lines: list[str],
    total: float,
    beats: list[float] | None = None,
) -> list[LyricLine]:
    """纯文本歌词：按字数加权铺满整首，可选把边界吸到拍上。

    加权而不是均分：一句 8 个字和一句 20 个字各占同样长的时间，
    看画面时就会觉得短句"赖着不走"、长句"一闪而过"。
    """
    if not lines or total <= 0:
        return []
    weights = [max(4.0, float(len(t))) for t in lines]
    wsum = sum(weights)
    spans = [total * w / wsum for w in weights]
    spans[-1] = total - sum(spans[:-1])          # 末行吃舍入残差

    if beats:
        from .models import snap_to_beats

        floor = min(0.6, total / max(1, len(lines)))
        spans = snap_to_beats(spans, list(beats), floor)

    out: list[LyricLine] = []
    acc = 0.0
    for i, text in enumerate(lines):
        span = max(0.0, spans[i] if i < len(spans) else 0.0)
        out.append(LyricLine(round(acc, 3), round(min(acc + span, total), 3), text))
        acc += span
    return out


def parse_lyrics(
    text: str,
    total: float = 0.0,
    beats: list[float] | None = None,
) -> list[LyricLine]:
    """把歌词文本解析成带时间轴的歌词行。LRC 优先，否则按字数加权铺满。

    ``total``：音频时长（秒）。0 表示未知——此时纯文本歌词无法铺满，返回空。
    """
    entries, offset = parse_lrc(text)
    if entries:
        lines: list[LyricLine] = []
        for i, (t, body) in enumerate(entries):
            start = max(0.0, t - offset)
            if i + 1 < len(entries):
                end = max(start, entries[i + 1][0] - offset)
            else:
                end = max(start + 2.0, total if total > 0 else start + 4.0)
            if total > 0:
                start, end = min(start, total), min(end, total)
            lines.append(LyricLine(round(start, 3), round(end, 3), body))
        return [ln for ln in lines if ln.span > 0 and ln.text]
    return align_plain(plain_lines(text), total, beats)


def assign_captions(shots: list[Any], lines: list[LyricLine]) -> int:
    """把歌词行分配给镜头，返回被赋值的镜头数。

    非卡拉OK 路径下每镜只能显示一条字幕，所以取**与该镜头重叠时间最长**的那条。

    为什么不是"第一条起点落在镜头内的"：那样一条 5 秒镜头会被在它末尾才开始的那句歌词
    占满整段（实测出现过：镜 10-15s，歌词 13.75s 才开始，却显示满 5 秒，
    而该镜中点其实在上一句里）。按重叠时长取，观众大部分时间看到的才是对的那句。
    """
    if not lines or not shots:
        return 0
    filled = 0
    for shot in shots:
        start = float(getattr(shot, "start", 0.0))
        end = float(getattr(shot, "end", start))
        if end <= start:
            continue
        best_text, best_overlap = "", 0.0
        for ln in lines:
            overlap = min(end, ln.end) - max(start, ln.start)
            if overlap > best_overlap + 1e-9:
                best_text, best_overlap = ln.text, overlap
        if best_text:
            shot.caption = best_text
            filled += 1
    return filled


def lyric_srt(lines: list[LyricLine], out_path: Path) -> Path:
    """按歌词自己的时间轴写 SRT。

    为什么不用 ``tts.write_srt``：那个是**按镜头**写的，一镜一条；
    歌词可能一镜两句、也可能一句跨两镜。字幕文件应该按歌词自己的节奏走，
    播放器才会逐句显示。
    """
    body: list[str] = []
    idx = 1
    for ln in lines:
        text = (ln.text or "").strip()
        if not text or ln.end <= ln.start:
            continue
        body.append(str(idx))
        body.append(f"{_srt_time(ln.start)} --> {_srt_time(ln.end)}")
        body.append(text)
        body.append("")
        idx += 1
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(body), encoding="utf-8-sig")
    return out_path


def _srt_time(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    ms = total_ms % 1000
    total_s = total_ms // 1000
    return f"{total_s // 3600:02d}:{total_s % 3600 // 60:02d}:{total_s % 60:02d},{ms:03d}"
