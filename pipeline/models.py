"""一言片场 · 数据契约（模块间标准格式）。

与 docs/技术方案-V1.md 第 4.2 节的 Storyboard JSON 契约保持一致。
所有跨模块传递的数据都用这里定义的 dataclass，禁止裸 dict 满天飞。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .common import clamp

# 状态机：pending → generating → reviewing → approved / rejected
SHOT_STATUS = ("pending", "generating", "reviewing", "approved", "rejected", "skipped")


@dataclass
class ProjectBrief:
    theme: str
    total_duration: int = 60
    aspect_ratio: str = "16:9"
    style: str = ""
    language: str = "zh"
    must_include: list[str] = field(default_factory=list)
    must_avoid: list[str] = field(default_factory=list)
    reference: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Script:
    title: str
    logline: str
    tone: str
    target_audience: str
    hook: str
    acts: list[dict[str, str]] = field(default_factory=list)
    narration_full: str = ""
    key_subjects: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Subject:
    id: str
    name: str
    visual_lock: str = ""


@dataclass
class Shot:
    id: int
    act: str
    duration: float
    narration: str
    visual: str
    camera: str
    prompt_image: str = ""
    prompt_video: str = ""
    subject_refs: list[str] = field(default_factory=list)
    workflow_id: str = ""
    acceptance: str = ""
    #: 上屏文字。为空时回落到 ``narration``（解说词）。
    #: MV 模式填的是**歌词行**——音频是音乐、没有配音，字幕必须跟歌词走；
    #: 而配乐模式仍然用解说词，因为那才是配音念的内容（字幕与声音要对得上）。
    caption: str = ""
    # 运行期字段
    status: str = "pending"
    route: str = ""
    provider: str = ""
    attempts: int = 0
    quality: int = 0
    defect_type: str = "none"
    verdict: str = ""
    frame_path: str = ""
    #: 出这张静帧的 provider（comfy / offline / cloud）。
    #: 合成时必须知道它是不是真图：offline 写出来的只是分镜示意图，
    #: 拿它当背景等于白出了真图；而真图必须被用上（否则整条出图链路白跑）。
    still_provider: str = ""
    intent_path: str = ""
    clip_path: str = ""
    start: float = 0.0

    @property
    def end(self) -> float:
        return round(self.start + self.duration, 3)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _fill(cls: Any, data: dict[str, Any]) -> Any:
    """按 dataclass 字段名从 dict 造对象，多余键忽略、缺失键用默认值。

    读人工编辑过的 plan 文件时必须容忍这两种情况：老版本没有新字段、
    将来加了字段又读到老文件。
    """
    import dataclasses

    names = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in names})


def brief_from_dict(data: dict[str, Any]) -> "ProjectBrief":
    return _fill(ProjectBrief, data)


def script_from_dict(data: dict[str, Any]) -> "Script":
    return _fill(Script, data)


def shot_from_dict(data: dict[str, Any]) -> "Shot":
    shot = _fill(Shot, data)
    # 拍摄前把运行期状态清干净：文件里可能残留上一次的 status/attempts
    shot.status = "pending"
    shot.attempts = 0
    shot.quality = 0
    shot.verdict = ""
    return shot


def storyboard_from_dict(data: dict[str, Any]) -> "Storyboard":
    board = Storyboard(project=dict(data.get("project") or {}))
    board.subjects = [_fill(Subject, item) for item in (data.get("subjects") or [])]
    board.shots = [shot_from_dict(item) for item in (data.get("shots") or [])]
    board.assign_timeline()
    return board


def caption_of(shot: Any) -> str:
    """取该镜头的**上屏文字**：优先 ``caption``（歌词），回落 ``narration``（解说词）。

    同时接受 ``Shot`` 对象与 dict——代码绘制后端拿到的是 dict（JSON 序列化过的分镜）。
    三个渲染路径（占位帧、代码绘制、SRT）都必须走这个函数，
    否则会出现"字幕文件是歌词、画面里是解说词"这种自相矛盾的产物。
    """
    if isinstance(shot, dict):
        cap = str(shot.get("caption") or "").strip()
        narr = str(shot.get("narration") or "").strip()
    else:
        cap = str(getattr(shot, "caption", "") or "").strip()
        narr = str(getattr(shot, "narration", "") or "").strip()
    return cap or narr


@dataclass
class Storyboard:
    project: dict[str, Any]
    subjects: list[Subject] = field(default_factory=list)
    shots: list[Shot] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "subjects": [asdict(s) for s in self.subjects],
            "shots": [s.to_dict() for s in self.shots],
        }

    @property
    def total_duration(self) -> float:
        return round(sum(s.duration for s in self.shots), 3)

    def assign_timeline(self) -> None:
        t = 0.0
        for shot in self.shots:
            shot.start = round(t, 3)
            t += shot.duration


@dataclass
class MusicInput:
    """音乐输入：MV 模式的时长权威与音轨来源。

    给了音乐就是 **MV 模式**，与「纯一句话」模式有四处语义差别：

    1. **总时长的权威随角色而变**。``role="song"``（音乐 MV）时**以音频为准**——
       这是 MV 的立身之本：画面服从音乐，而不是让音乐迁就画面。
       ``role="bgm"``（背景配乐）时**以解说为准**：音乐负责垫底，短了就循环铺满，
       否则一首 3 分钟的歌会把 60 秒解说硬拉成 3 分钟。
    2. **音轨含音乐**。``role="song"`` 时音乐就是音轨本身，不再生成 TTS 配音；
       ``role="bgm"`` 时保留配音解说，音乐在混音时被压到背景垫底的音量。
    3. **上屏文字优先用歌词**（``lyrics`` 非空且 ``caption_source="lyrics"``），
       没有歌词时退回解说词。
    4. 分镜切点应落在节拍上（``bpm`` / ``offset``，由节拍层填）。

    ``lyrics`` 承载**原始文本**，可能带 LRC 时间戳（形如 ``[00:12.34]``）。
    这里只承载输入、不做解释——解析与对齐是歌词层的职责。
    """

    path: str = ""
    #: 音乐时长（秒）。探测失败为 0，此时退回用户给的时长并如实记录。
    duration: float = 0.0
    lyrics: str = ""
    #: 音乐扮演的角色：
    #:   ``song`` —— 音乐就是成片音轨（音乐 MV）：不生成配音，歌词上屏
    #:   ``bgm``  —— 音乐是背景垫底：保留配音解说，混音时被压到 BGM_GAIN
    role: str = "song"
    #: 上屏文字来源：lyrics（有歌词时）| narration（退回解说词）
    caption_source: str = "lyrics"
    #: 节拍信息，由节拍层填充；bpm=0 表示未知，交给引擎默认值。
    bpm: float = 0.0
    offset: float = 0.0
    #: 拍点时刻列表（秒）。非空时镜头切点会吸附到最近的拍点（按拍切镜）。
    beats: list[float] = field(default_factory=list)
    #: 是否自动从音频估计节拍。自动估计不准时可用 --no-beats 关掉、用 --bpm 手工指定。
    auto_beats: bool = True
    #: 节拍来源（estimated | manual | none），只用于如实汇报。
    beat_source: str = "none"

    @property
    def enabled(self) -> bool:
        return bool(str(self.path).strip())

    @property
    def is_bgm(self) -> bool:
        return self.enabled and self.role == "bgm"

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "duration": round(self.duration, 3),
            "role": self.role,
            "lyrics_lines": len([ln for ln in self.lyrics.splitlines() if ln.strip()]),
            "caption_source": self.caption_source,
            "bpm": round(self.bpm, 2),
            "offset": round(self.offset, 3),
            "beats": len(self.beats),
            "beat_source": self.beat_source,
        }


#: 单镜时长的上下限（秒）。提成常量：MV 模式要按音乐结构重新定标时会改这里。
MIN_SHOT_SECONDS = 2.0
MAX_SHOT_SECONDS = 10.0


def _median_gap(times: list[float]) -> float:
    if len(times) < 2:
        return 0.0
    gaps = sorted(times[i + 1] - times[i] for i in range(len(times) - 1))
    return gaps[len(gaps) // 2]


def snap_to_beats(
    durations: list[float],
    beats: list[float],
    floor: float,
    tolerance_ratio: float = 0.35,
) -> list[float]:
    """把镜头切点吸附到最近的拍点。返回新的时长列表。

    MV 的观感差别主要就在这一步：切点落在拍上叫"剪辑"，落在拍与拍之间叫"卡顿"。

    三条约束，缺一不可：

    1. **只在拍点离原切点足够近时才吸**（默认容差 = 拍间隔的 35%）。
       拍点稀疏时硬吸会把某一镜拉长得离谱，反而更难看。
    2. **吸附后两侧镜头都不能低于下限**——否则会造出 0.3 秒的闪帧。
    3. **总时长不变**：最后一个切点永远锁在成片末尾（音频时长是权威），
       所以吸附只影响中间切点，不会让成片与音乐错开。
    """
    n = len(durations)
    if n <= 1 or not beats:
        return list(durations)
    total = sum(durations)
    interval = _median_gap(beats)
    tol = max(0.02, interval * tolerance_ratio)

    bounds: list[float] = []
    acc = 0.0
    for d in durations:
        acc += d
        bounds.append(acc)

    out: list[float] = []
    prev = 0.0
    for i in range(n - 1):
        b = bounds[i]
        target = min(beats, key=lambda x: abs(x - b))
        remaining = n - 1 - i
        if (abs(target - b) <= tol
                and target - prev >= floor
                and total - target >= floor * remaining):
            out.append(round(target - prev, 3))
            prev = target
        else:
            out.append(round(b - prev, 3))
            prev = b
    out.append(round(total - prev, 3))
    return out


def build_timeline(shots: list[Shot], total: float, beats: list[float] | None = None) -> list[Shot]:
    """把总时长按镜长权重分配到各镜头，保证累加严格等于总时长。

    **末镜不再无保护地吃残差**。原来是 ``shot.duration = total - t``，镜头一多
    或总时长偏短，末镜就被算成**负数**——实测 10 镜 / 15 秒 → -3.00 秒、
    12 镜 / 15 秒 → -7.00 秒，负时长随后流进配音变速、字幕时间码和出片命令
    （被 ``max(1.0, …)`` 掩盖，所以一直没被发现）。

    现在的做法：先给每一镜算出落在 [下限, 上限] 内的时长，在**还有余量的镜头**
    之间把偏差摊平，最后才让末镜吃掉舍入残差。此时残差只有毫秒级，不可能把末镜
    压成负数。总时长连「每镜都给到下限」都不够时，整体压低下限而不是出负数。

    ``beats``：拍点时刻列表（秒）。给定时会把中间切点吸附到最近的拍点上
    （见 :func:`snap_to_beats`），这是 MV「切点踩在拍上」的实现处。
    """
    if not shots:
        return shots
    n = len(shots)
    floor = min(MIN_SHOT_SECONDS, total / n)
    weights = [max(0.5, s.duration) for s in shots]
    scale = total / sum(weights)
    durations = [clamp(s.duration * scale, floor, MAX_SHOT_SECONDS) for s in shots]

    # 把偏差摊到 0..n-2（末镜专职吃舍入残差，不参与摊派）
    drift = total - sum(durations)
    guard = n * 4
    while abs(drift) > 1e-6 and guard > 0:
        guard -= 1
        moved = False
        for i in range(n - 1):
            if abs(drift) <= 1e-6:
                break
            room = (MAX_SHOT_SECONDS - durations[i]) if drift > 0 else (durations[i] - floor)
            if room <= 1e-9:
                continue
            step = min(room, abs(drift))
            if drift > 0:
                durations[i] += step
                drift -= step
            else:
                durations[i] -= step
                drift += step
            moved = True
        if not moved:
            break

    # 按拍切镜：只动中间切点，末镜仍锁在成片末尾（音频时长是权威）
    if beats:
        durations = snap_to_beats(durations, list(beats), floor)

    t = 0.0
    for i, shot in enumerate(shots):
        if i == n - 1:
            shot.duration = round(total - t, 3)
        else:
            shot.duration = round(durations[i], 3)
        shot.start = round(t, 3)
        t += shot.duration
    return shots