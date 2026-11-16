"""一言片场 · 合成层（FFmpeg）。

拼接 → 配音 → 字幕 → 成片。全部本地、零成本。
输出：out/final.mp4（H.264 + AAC，faststart）、out/subtitles.srt、out/storyboard.json。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .models import Storyboard
from .lyrics import lyric_srt
from .tts import silence, write_srt, write_srt_lines
from .video import FPS, VideoError, probe_duration, run_ffmpeg

#: 音乐作为**背景垫底**时的线性增益（人声是 1.0）。0.28 ≈ -11dB，
#: 是人声之下垫乐的常用量级：听得见音乐，但不会盖住人声。
BGM_GAIN = 0.28
#: 音乐淡入淡出时长（秒）。硬起硬落会"啪"一声，MV 尤其明显。
MUSIC_FADE_SECONDS = 0.8
#: 响度归一目标（EBU R128）：-16 LUFS 适合网络播放，TP=-1.5 留真峰余量。
LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"
#: 对白优先（ducking）：人声出现时把垫乐压到其下。阈值 0.05 ≈ −26dBFS、
#: 比率 8:1、起音 20ms / 释放 320ms——正常说话大约压 12–15dB，
#: 压得下去又不至于让音乐"抽气"。
DUCK_FILTER = ("sidechaincompress=threshold=0.05:ratio=8:"
               "attack=20:release=320:level_sc=1")
#: 可用转场：id → (ffmpeg xfade 名称, 中文名)。只做三种，避免变成自由时间线。
TRANSITIONS = {
    "none": ("", "硬切"),
    "fade": ("fade", "叠化"),
    "slide": ("slideleft", "左滑"),
    "black": ("fadeblack", "黑场"),
}
#: 转场总时长（秒）。相邻镜头各让出一半做冻结补边，镜头内容时间轴不变。
TRANSITION_SECONDS = 0.4
#: 字幕卡入场淡入时长（秒）。只作用于字幕层，固定标题层不参与。
TEXT_FADE_SECONDS = 0.2


@dataclass
class ComposeResult:
    video: str = ""
    subtitle: str = ""
    storyboard: str = ""
    duration: float = 0.0
    resolution: str = ""
    has_audio: bool = False
    audio_source: str = "silent"
    #: 真正并入成片的音频文件路径（无配音时为 ""，成片里是静音兜底轨）。
    #: 下游（如代码绘制后端）要复用同一音轨，不能再自己猜路径。
    audio: str = ""
    size_mb: float = 0.0
    delivered: int = 0
    dropped: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "video": self.video,
            "subtitle": self.subtitle,
            "storyboard": self.storyboard,
            "duration": self.duration,
            "resolution": self.resolution,
            "has_audio": self.has_audio,
            "audio_source": self.audio_source,
            "audio": self.audio,
            "size_mb": self.size_mb,
            "delivered": self.delivered,
            "dropped": self.dropped,
            "notes": self.notes,
        }


def fit_clip(src: Path, duration: float, out_path: Path) -> Path:
    """把已有片段对齐到目标时长：长了裁尾，短了冻结最后一帧补足。

    时间轴编辑只改"怎么拼"，不该重新出图。但片段是按**旧镜长**渲出来的，
    改过时长之后必须先把片段本身对齐——否则 concat 出来的成片与字幕、配音、
    时间轴全对不上，而且不会报错，只是越到后面错得越多。

    编码参数与 `video.KenBurns.make_clip` 保持一致（libx264 / crf 20 /
    yuv420p / 30fps）：`concat_clips` 走 stream copy，参数不一致会直接拼不上。
    """
    have = probe_duration(src)
    if have <= 0:
        # 探测失败时**不能**当成"长度为 0 所以补满"：那会产出一整段冻结帧，
        # 画面看着还在、实际一动不动，而且不会报错。宁可在这里失败。
        raise VideoError(f"探测不出片段时长，无法对齐到 {duration:.3f}s：{src}")
    if abs(have - duration) <= 1.0 / FPS:
        return src
    if have > duration:
        args = ["-i", str(src), "-t", f"{duration:.3f}"]
    else:
        pad = duration - have
        args = ["-i", str(src),
                "-vf", f"tpad=stop_mode=clone:stop_duration={pad:.3f}",
                "-t", f"{duration:.3f}"]
    args += [
        "-an",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p", "-r", str(FPS),
        "-movflags", "+faststart",
        str(out_path),
    ]
    run_ffmpeg(args, timeout=300)
    return out_path


def concat_clips(clips: list[Path], out_path: Path, work_dir: Path) -> Path:
    """无损拼接（stream copy）。用 concat demuxer，不用 filter_complex，快且不掉画质。"""
    good = [c for c in clips if c.exists()]
    if not good:
        raise VideoError("没有可拼接的片段")
    listing = work_dir / "concat.txt"
    lines = []
    for clip in good:
        # concat demuxer 对单引号敏感，统一转义
        safe = str(clip.resolve()).replace("'", r"'\''")
        lines.append(f"file '{safe}'")
    listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
    run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(out_path)])
    return out_path


def transition_clips(
    clips: list[Path],
    durations: list[float],
    out_path: Path,
    work_dir: Path,
    kind: str = "fade",
) -> Path:
    """用 ``xfade`` 在镜头间加转场，且**总时长与分镜台账一致**。

    关键约束：转场不能改变故事时间轴（字幕、配音都按分镜时长走）。做法是
    给每个镜头在自己的分镜时长两端补冻结帧——首个不补头、末个不补尾——
    再用 2×半时长做 xfade。补边正好占用转场的前后半程，镜头的**内容**仍
    落在原来的分镜时间轴上：

        A 内容 [0, L1]，尾部冻结 L1..L1+h
        B 头部冻结 B-h..B，内容 B..B+L2

    因此 ``ΣL`` 不变、边界转场居中、每镜可见内容各让出 h。

    与 ``concat_clips`` 的 stream copy 不同，这里必须重编码（xfade 需要
    解码后的帧）。关闭转场时不会走这个函数，画面与旧版逐像素一致。
    """
    good = [Path(clip) for clip in clips]
    if len(good) < 2 or any(not clip.exists() for clip in good):
        raise VideoError("转场需要至少两个存在的片段")
    if len(durations) != len(good):
        raise VideoError(
            f"转场片段数与分镜时长数不一致：{len(good)} 个片段 / {len(durations)} 个时长"
        )
    name = TRANSITIONS.get(str(kind).lower(), ("", ""))[0]
    if not name:
        raise VideoError(f"未知转场：{kind}")

    # 极短镜头（<1.6s）按 1/4 缩短半时长，避免两端补边把内容吃光。
    half = min(TRANSITION_SECONDS / 2, min(float(d) for d in durations) / 4)
    half = max(half, 1.0 / FPS)
    lengths: list[float] = []
    normalized: list[Path] = []
    for index, (clip, raw) in enumerate(zip(good, durations)):
        length = max(float(raw), half * 2 + 1.0 / FPS)
        head = 0.0 if index == 0 else half
        tail = 0.0 if index == len(good) - 1 else half
        target = head + length + tail
        have = probe_duration(clip)
        if have <= 0:
            raise VideoError(f"探测不出片段时长，无法加转场：{clip}")
        # tpad 补头部/尾部；片段比分镜短时再补足差额，最后裁到精确长度。
        pad_stop = tail + max(0.0, length - have)
        vf = (
            f"tpad=start_duration={head:.3f}:start_mode=clone:"
            f"stop_duration={pad_stop:.3f}:stop_mode=clone,"
            f"fps={FPS},format=yuv420p,settb=AVTB,"
            f"trim=duration={target:.3f},setpts=PTS-STARTPTS"
        )
        out = work_dir / f"_tr{index:02d}.mp4"
        run_ffmpeg([
            "-i", str(clip), "-vf", vf, "-an",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-pix_fmt", "yuv420p", "-r", str(FPS), "-t", f"{target:.3f}",
            "-movflags", "+faststart", str(out),
        ], timeout=600)
        normalized.append(out)
        lengths.append(length)

    args: list[str] = []
    for path in normalized:
        args += ["-i", str(path)]
    parts: list[str] = []
    prev = "[0:v]"
    boundary = 0.0
    for index in range(1, len(normalized)):
        boundary += lengths[index - 1]
        start = boundary - half
        label = f"x{index}"
        parts.append(
            f"{prev}[{index}:v]xfade=transition={name}:duration={half * 2:.3f}:"
            f"offset={start:.3f}[{label}]"
        )
        prev = f"[{label}]"
    args += [
        "-filter_complex", ";".join(parts),
        "-map", prev, "-an",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p", "-r", str(FPS), "-movflags", "+faststart",
        str(out_path),
    ]
    run_ffmpeg(args, timeout=1800)
    if not out_path.exists():
        raise VideoError(f"转场合成失败，未产出文件：{out_path}")
    return out_path


def overlay_text_layers(
    video: Path,
    layers: list[tuple[Path, float, float]],
    out_path: Path,
    *,
    animate: bool = False,
) -> Path:
    """把透明文字层按时间区间叠到已拼接的画面上。

    `layers = [(png, start, end), ...]`。这一步发生在视频拼接之后、混音之前：
    片段本身保持干净，改字幕只需重合成，不需要重新出图或重绘背景。

    `animate=True` 时字幕卡（第 2 层起；第 1 层是固定标题/镜号层）在各自的
    起点做 0.2s 淡入。它只影响最终叠加，不进入片段缓存，重合成即可生效。
    """
    if not layers:
        return video
    duration = probe_duration(video)
    if duration <= 0:
        raise VideoError("无法探测待叠字视频的时长")
    args: list[str] = ["-i", str(video)]
    for path, _start, _end in layers:
        # 静态 PNG 用 image2 循环输入；必须显式 -t，否则循环输入会让
        # overlay 永远等不到 EOF，最终合成永不结束。
        args += ["-loop", "1", "-framerate", str(FPS), "-i", str(path)]
    parts: list[str] = []
    prev = "0:v"
    for index, (_path, start, end) in enumerate(layers, start=1):
        label = f"v{index}"
        source = f"[{index}:v]"
        if animate and index > 1:
            # fade 的 st 是全局时间；PNG 从 0 秒循环，因此正好在图层起点开始淡入。
            parts.append(
                f"[{index}:v]format=rgba,"
                f"fade=t=in:st={max(0.0, float(start)):.3f}:d={TEXT_FADE_SECONDS:.3f}:alpha=1"
                f"[f{index}]"
            )
            source = f"[f{index}]"
        parts.append(
            f"[{prev}]{source}overlay=0:0:"
            f"enable='between(t,{max(0.0, float(start)):.3f},{max(0.0, float(end)):.3f})'"
            f"[{label}]"
        )
        prev = label
    args += [
        "-filter_complex", ";".join(parts),
        "-map", f"[{prev}]",
        "-an",
        "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(out_path),
    ]
    run_ffmpeg(args, timeout=1800)
    if not out_path.is_file() or out_path.stat().st_size <= 0:
        raise VideoError(f"字幕层合成失败，未产出文件：{out_path}")
    return out_path


def mux_audio(video: Path, audio: Path | None, out_path: Path, duration: float) -> tuple[Path, bool]:
    """合上音轨。没有配音时铺一条等长静音轨，保证成片在剪辑软件里不报错。

    画面长度是唯一权威：音轨短了补静音、长了截掉。以前用 `-shortest`，
    配音比画面短（最后一镜没台词、TTS 提前收尾）时会把**画面**一起截断，
    成片凭空少几秒，而时长台账还按拼接后的长度记。
    """
    if audio is None or not audio.exists():
        silent = out_path.parent / "_silence.m4a"
        silence(silent, duration)
        audio = silent
        has_audio = False
    else:
        has_audio = True
    run_ffmpeg(
        [
            "-i", str(video),
            "-i", str(audio),
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy",
            "-af", "apad",
            "-c:a", "aac", "-b:a", "192k",
            "-t", f"{duration:.3f}",
            "-movflags", "+faststart",
            str(out_path),
        ]
    )
    return out_path, has_audio


def build_mix_args(
    tracks: list[tuple[Path, float, bool]],
    out_path: Path,
    *,
    duration: float = 0.0,
    fade: float = 0.0,
    loudnorm: bool = False,
    duck: bool = False,
) -> list[str]:
    """构造「多轨音频 → 一条混音文件」的 FFmpeg **参数**（不含程序名与 -y，
    这两个由 ``run_ffmpeg`` 统一拼——和 ``mux_audio`` 保持同一约定，
    多拼一次会把 ffmpeg 自己的路径当成输出文件）。

    ``tracks = [(文件, 线性增益, 是否循环)]``。单独抽成纯函数是为了能在单测里
    断言混音链路真的接上了。以前全仓 ``amix`` / ``loudnorm`` / ``volume=``
    **零命中**——README 里"既能配乐又能配音"这句话从来没有实现过。

    四个关键细节：

    1. **每轨先统一成 stereo/44100/fltp 再混**：素材常见单声道与双声道混杂，
       直接 amix 会因声道布局不一致而报错。
    2. **``normalize=0``**：amix 默认把每轨除以轨数，人声会被无故压小 6dB。
    3. **``apad=whole_dur`` 补齐到整片长度**：这一步是防"音轨比画面短"——
       `mux_audio` 用 `-shortest`，音轨一旦短于画面就会**把画面截断**。
       配乐比解说短是常见情况，所以音乐轨要循环播放（``loop=True``），
       再补一道 apad 兜底。
    4. **``duck=True`` 时第 0 轨视为对白**：其余轨道（垫乐）在对白出现时由
       ``sidechaincompress`` 压低。侧链键要 apad 到整片长度——否则人声一结束
       压缩器就收工，尾巴上的音乐整段丢失。
    """
    if not tracks:
        raise ValueError("没有可混音的轨道")
    args: list[str] = []
    for path, _gain, loop in tracks:
        if loop:
            args += ["-stream_loop", "-1"]
        args += ["-i", str(path)]

    parts = [
        f"[{i}:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,"
        f"volume={max(0.0, float(gain)):.4g}[a{i}]"
        for i, (_path, gain, _loop) in enumerate(tracks)
    ]
    ducked = duck and len(tracks) > 1
    if ducked:
        rest = len(tracks) - 1
        # 人声一路继续入混，另外复制若干份当侧链键。
        parts.append(f"[a0]asplit={rest + 1}[voice]"
                     + "".join(f"[raw{i}]" for i in range(rest)))
        for i in range(rest):
            key = f"[raw{i}]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo"
            if duration > 0:
                key += f",apad=whole_dur={duration:.3f}"
            parts.append(key + f"[key{i}]")
            parts.append(f"[a{i + 1}][key{i}]{DUCK_FILTER}[d{i}]")
        pad = "[voice]" + "".join(f"[d{i}]" for i in range(rest))
        inputs = 1 + rest
    else:
        pad = "".join(f"[a{i}]" for i in range(len(tracks)))
        inputs = len(tracks)
    if inputs == 1:
        chain = pad + "anull"
    else:
        # duration=longest：短的一轨结束后不截断另一轨（尾巴交给 apad / -t）
        chain = pad + f"amix=inputs={inputs}:duration=longest:normalize=0"

    if fade > 0 and duration > 0:
        f = min(float(fade), duration / 2.0)
        chain += (f",afade=t=in:st=0:d={f:.3f}"
                  f",afade=t=out:st={max(0.0, duration - f):.3f}:d={f:.3f}")
    if duration > 0:
        # 补齐到整片长度：宁可末尾是静音（音乐真的用完了），也绝不让音轨短于画面
        chain += f",apad=whole_dur={duration:.3f}"
    if loudnorm:
        chain += "," + LOUDNORM

    args += [
        "-filter_complex", ";".join(parts) + ";" + chain + "[out]",
        "-map", "[out]",
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
    ]
    if duration > 0:
        args += ["-t", f"{duration:.3f}"]
    args += [str(out_path)]
    return args


def mix_tracks(
    tracks: list[tuple[Path, float, bool]],
    out_path: Path,
    duration: float = 0.0,
    *,
    fade: float = 0.0,
    loudnorm: bool = False,
    duck: bool = False,
) -> Path:
    """把多轨混成一条音轨文件并返回它。失败时抛 VideoError（不产出半成品）。"""
    run_ffmpeg(build_mix_args(tracks, out_path, duration=duration,
                              fade=fade, loudnorm=loudnorm, duck=duck))
    if not out_path.exists():
        raise VideoError(f"混音失败，未产出文件：{out_path}")
    return out_path


def probe_resolution(path: Path) -> str:
    from .video import run_ffprobe

    info = run_ffprobe(["-select_streams", "v:0", "-show_entries", "stream=width,height", str(path)])
    try:
        st = info["streams"][0]
        return f"{st['width']}x{st['height']}"
    except (KeyError, IndexError):
        return ""


def compose(
    board: Storyboard,
    clips: list[Path],
    out_dir: Path,
    work_dir: Path,
    audio: Path | None = None,
    audio_source: str = "silent",
    delivered: list | None = None,
    music: Path | None = None,
    lyrics: list | None = None,
    voice_gain: float = 1.0,
    music_gain: float = BGM_GAIN,
    voice_duck: bool = False,
    subtitle_offset: float = 0.0,
    subtitle_lines: list | None = None,
    text_layers: list[tuple[Path, float, float]] | None = None,
    subtitle_mode: str = "burn",
    transition: str = "none",
    text_anim: bool = False,
) -> ComposeResult:
    """合成成片。任何一步失败都抛出，绝不产出半成品冒充成品。

    `delivered`：本次真正进成片的镜头清单（终检通过）。给定时会据此裁剪
    分镜契约与字幕——被剔除的镜头不该出现在成片字幕里。若为 None 则视为全部交付。

    `music`：音乐轨。给了就先把「人声（可选）+ 音乐」混成一条再并入成片——
    MV 模式只有音乐一轨；配乐模式是人声 + 压低到 ``BGM_GAIN`` 的音乐。
    两者都会过响度归一与淡入淡出。

    `lyrics`：歌词行。给定时字幕文件按**歌词自己的时间轴**写（一镜可能两句、
    一句可能跨两镜），而不是按镜头切；否则仍按镜头写解说词。

    `text_layers`：最终合成的透明文字层。片段本身不含字幕，改字幕只需重新
    合成；旧片段若已经烘死文字，必须先重拍一次生成干净片段。

    `transition`：镜头间转场（none/fade/slide/black）。非 none 时走
    `transition_clips`；它用补冻结帧的方式保证总时长与分镜台账一致。
    `text_anim`：字幕卡入场淡入，只影响叠加层，不影响片段缓存。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    dropped: list[int] = []
    if delivered is not None:
        keep = {s.id for s in delivered}
        dropped = [s.id for s in board.shots if s.id not in keep]
        if dropped:
            # 直接复用原对象，只裁剪列表；避免序列化反解丢字段
            board = Storyboard(
                project=dict(board.project),
                subjects=list(board.subjects),
                shots=[s for s in board.shots if s.id in keep],
            )
            board.project["total_duration"] = board.total_duration
            board.project["dropped_shots"] = sorted(dropped)
            board.assign_timeline()
    result = ComposeResult()
    result.delivered = len(board.shots)
    result.dropped = len(dropped)
    silent_video = work_dir / "_concat.mp4"
    kind = str(transition or "none").lower()
    if kind != "none" and len(clips) > 1:
        silent_video = transition_clips(
            clips, [float(shot.duration) for shot in board.shots],
            work_dir / "_transition.mp4", work_dir, kind,
        )
    else:
        concat_clips(clips, silent_video, work_dir)
    duration = probe_duration(silent_video)
    if duration <= 0:
        raise VideoError("拼接后的视频时长为 0")

    # 音轨准备：有音乐时先把多轨混成一条（人声不受损、音乐垫底、响度归一、淡入淡出），
    # 再整体并入成片。MV 模式只有音乐一轨；配乐模式是人声 + 音乐。
    bed: Path | None = audio
    source = audio_source
    if music is not None and Path(music).exists():
        tracks: list[tuple[Path, float, bool]] = []
        if audio is not None and Path(audio).exists():
            # 人声电平由项目设置控制；0 表示静音，但仍保留音轨占位。
            tracks.append((Path(audio), max(0.0, float(voice_gain)), False))
        # 音乐轨循环播放：配乐比解说短是常态，而音轨一旦短于画面，
        # mux_audio 的 -shortest 会把**画面**截断。
        tracks.append((Path(music), max(0.0, float(music_gain)), True))
        source = "music+voice" if len(tracks) > 1 else "music"
        if voice_duck and len(tracks) < 2:
            result.notes.append("已开启「对白优先」，但本片没有配音，音乐闪避未生效")
        bed = mix_tracks(tracks, work_dir / "audio_mix.m4a", duration,
                         fade=MUSIC_FADE_SECONDS, loudnorm=True,
                         duck=bool(voice_duck and len(tracks) > 1))
    elif audio is not None and Path(audio).exists() and abs(float(voice_gain) - 1.0) > 1e-6:
        # 没有音乐时原本直接 mux 原声；只有人声电平被调过才需要过一次滤镜。
        bed = mix_tracks([(Path(audio), max(0.0, float(voice_gain)), False)],
                         work_dir / "voice_mix.m4a", duration, loudnorm=False)

    # 文字层在拼接之后、混音之前叠加：字幕时间轴与最终画面严格一致，
    # 而片段本身保持干净，后续改字不必重新出图。
    visual = silent_video
    if text_layers:
        visual = overlay_text_layers(silent_video, text_layers,
                                     work_dir / "_texted.mp4", animate=text_anim)

    final = out_dir / "final.mp4"
    # 母版是对外发布的唯一版本。直接往它上面写，重渲期间它会被截断：
    # 历史详情与导出在这段时间全部拿不到片子；中途崩溃还会留下半截文件
    # 被 manifest 当成成片。先写临时文件，成功后原子替换——新片就绪前
    # 旧母版一直可播，失败则原样保留。
    pending = out_dir / ".final.pending.mp4"
    pending.unlink(missing_ok=True)
    try:
        _, has_audio = mux_audio(visual, bed, pending, duration)
        pending.replace(final)
    except BaseException:
        pending.unlink(missing_ok=True)
        raise
    result.video = str(final)
    result.duration = probe_duration(final) or duration
    result.resolution = probe_resolution(final)
    result.has_audio = has_audio
    result.audio_source = source if has_audio else "silent"
    # 只在真的并入了音频时记路径；静音兜底不算"有音频"，否则下游会把静音当配音复用
    result.audio = str(bed) if (has_audio and bed) else ""
    result.size_mb = round(final.stat().st_size / 1024 / 1024, 2)
    if subtitle_mode == "none":
        # 明确不要字幕：不产出 SRT，也不叠字幕卡（固定标题层仍可存在）。
        # 上一版留下的 SRT 必须删掉，否则历史页仍显示"有字幕"、字幕编辑器
        # 还会把旧轨读回来，用户以为自己的"不要字幕"没生效。
        result.subtitle = ""
        (out_dir / "subtitles.srt").unlink(missing_ok=True)
    elif subtitle_lines is not None:
        # 人工编辑过的字幕轨优先于歌词/镜头字幕：用户明确改过时间与断句，
        # 重合成时再按自动规则生成一遍等于把他的工作丢掉。
        result.subtitle = str(write_srt_lines(subtitle_lines, out_dir / "subtitles.srt",
                                              offset=subtitle_offset, limit=duration))
    elif lyrics:
        result.subtitle = str(lyric_srt(lyrics, out_dir / "subtitles.srt",
                                        offset=subtitle_offset, max_end=duration))
    else:
        result.subtitle = str(write_srt(board, out_dir / "subtitles.srt",
                                        offset=subtitle_offset))

    import json

    sb_path = out_dir / "storyboard.json"
    # 这份分镜是拍摄证据的权威副本（复用路径靠它恢复选中图摘要），
    # 同样不能让人读到写了一半的 JSON。
    sb_pending = out_dir / ".storyboard.pending.json"
    sb_pending.write_text(json.dumps(board.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    sb_pending.replace(sb_path)
    result.storyboard = str(sb_path)
    if result.audio_source == "silent":
        result.notes.append("本片无配音：未启用离线/云端 TTS，已铺等长静音轨")
    return result
