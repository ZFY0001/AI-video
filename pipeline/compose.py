"""一言片场 · 合成层（FFmpeg）。

拼接 → 配音 → 字幕 → 成片。全部本地、零成本。
输出：out/final.mp4（H.264 + AAC，faststart）、out/subtitles.srt、out/storyboard.json。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .models import Storyboard
from .lyrics import lyric_srt
from .tts import silence, write_srt
from .video import FPS, VideoError, probe_duration, run_ffmpeg

#: 音乐作为**背景垫底**时的线性增益（人声是 1.0）。0.28 ≈ -11dB，
#: 是人声之下垫乐的常用量级：听得见音乐，但不会盖住人声。
BGM_GAIN = 0.28
#: 音乐淡入淡出时长（秒）。硬起硬落会"啪"一声，MV 尤其明显。
MUSIC_FADE_SECONDS = 0.8
#: 响度归一目标（EBU R128）：-16 LUFS 适合网络播放，TP=-1.5 留真峰余量。
LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"


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


def mux_audio(video: Path, audio: Path | None, out_path: Path, duration: float) -> tuple[Path, bool]:
    """合上音轨。没有配音时铺一条等长静音轨，保证成片在剪辑软件里不报错。"""
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
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-shortest", "-movflags", "+faststart",
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
) -> list[str]:
    """构造「多轨音频 → 一条混音文件」的 FFmpeg **参数**（不含程序名与 -y，
    这两个由 ``run_ffmpeg`` 统一拼——和 ``mux_audio`` 保持同一约定，
    多拼一次会把 ffmpeg 自己的路径当成输出文件）。

    ``tracks = [(文件, 线性增益, 是否循环)]``。单独抽成纯函数是为了能在单测里
    断言混音链路真的接上了。以前全仓 ``amix`` / ``loudnorm`` / ``volume=``
    **零命中**——README 里"既能配乐又能配音"这句话从来没有实现过。

    三个关键细节：

    1. **每轨先统一成 stereo/44100/fltp 再混**：素材常见单声道与双声道混杂，
       直接 amix 会因声道布局不一致而报错。
    2. **``normalize=0``**：amix 默认把每轨除以轨数，人声会被无故压小 6dB。
    3. **``apad=whole_dur`` 补齐到整片长度**：这一步是防"音轨比画面短"——
       `mux_audio` 用 `-shortest`，音轨一旦短于画面就会**把画面截断**。
       配乐比解说短是常见情况，所以音乐轨要循环播放（``loop=True``），
       再补一道 apad 兜底。
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
    pad = "".join(f"[a{i}]" for i in range(len(tracks)))
    if len(tracks) == 1:
        chain = pad + "anull"
    else:
        # duration=longest：短的一轨结束后不截断另一轨（尾巴交给 apad / -t）
        chain = pad + f"amix=inputs={len(tracks)}:duration=longest:normalize=0"

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
) -> Path:
    """把多轨混成一条音轨文件并返回它。失败时抛 VideoError（不产出半成品）。"""
    run_ffmpeg(build_mix_args(tracks, out_path, duration=duration,
                              fade=fade, loudnorm=loudnorm))
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
) -> ComposeResult:
    """合成成片。任何一步失败都抛出，绝不产出半成品冒充成品。

    `delivered`：本次真正进成片的镜头清单（终检通过）。给定时会据此裁剪
    分镜契约与字幕——被剔除的镜头不该出现在成片字幕里。若为 None 则视为全部交付。

    `music`：音乐轨。给了就先把「人声（可选）+ 音乐」混成一条再并入成片——
    MV 模式只有音乐一轨；配乐模式是人声 + 压低到 ``BGM_GAIN`` 的音乐。
    两者都会过响度归一与淡入淡出。

    `lyrics`：歌词行。给定时字幕文件按**歌词自己的时间轴**写（一镜可能两句、
    一句可能跨两镜），而不是按镜头切；否则仍按镜头写解说词。
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
            tracks.append((Path(audio), 1.0, False))
        # 音乐轨循环播放：配乐比解说短是常态，而音轨一旦短于画面，
        # mux_audio 的 -shortest 会把**画面**截断。
        tracks.append((Path(music), BGM_GAIN, True))
        source = "music+voice" if len(tracks) > 1 else "music"
        bed = mix_tracks(tracks, work_dir / "audio_mix.m4a", duration,
                         fade=MUSIC_FADE_SECONDS, loudnorm=True)

    final = out_dir / "final.mp4"
    _, has_audio = mux_audio(silent_video, bed, final, duration)
    result.video = str(final)
    result.duration = probe_duration(final) or duration
    result.resolution = probe_resolution(final)
    result.has_audio = has_audio
    result.audio_source = source if has_audio else "silent"
    # 只在真的并入了音频时记路径；静音兜底不算"有音频"，否则下游会把静音当配音复用
    result.audio = str(bed) if (has_audio and bed) else ""
    result.size_mb = round(final.stat().st_size / 1024 / 1024, 2)
    if lyrics:
        result.subtitle = str(lyric_srt(lyrics, out_dir / "subtitles.srt"))
    else:
        result.subtitle = str(write_srt(board, out_dir / "subtitles.srt"))

    import json

    sb_path = out_dir / "storyboard.json"
    sb_path.write_text(json.dumps(board.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    result.storyboard = str(sb_path)
    if result.audio_source == "silent":
        result.notes.append("本片无配音：未启用离线/云端 TTS，已铺等长静音轨")
    return result
