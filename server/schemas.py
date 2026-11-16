"""请求 / 响应模型。

字段与 `app.engine.RunOptions` 一一对应，不新增也不改名——前端表单直接映射，
避免出现"界面上有、引擎里没有"的假参数。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

ASPECTS = ("16:9", "9:16", "1:1")
QUALITIES = ("fast", "medium", "high")

#: 音轨上传的大小上限（字节）。
#:
#: 60 MB 是按"容得下任何常见压缩格式、挡住长 WAV"定的：一首 5 分钟的 FLAC 约
#: 30 MB，而 10 分钟的 44.1kHz 立体声 WAV 要到 100 MB 以上。后者没有理由走上传——
#: 让 ffmpeg 转一道再传，体积能小一个数量级。
MAX_AUDIO_BYTES = 60 * 1024 * 1024

#: 音轨上传允许的扩展名。**只是第一道**：真正的判据是 ffprobe 能不能解出时长，
#: 把 .txt 改名成 .mp3 必须在这一步被拒（见 server/media.py 的 save_audio）。
AUDIO_EXTS = ("mp3", "wav", "m4a", "aac", "flac", "ogg", "opus")


class RunRequest(BaseModel):
    theme: str = Field(min_length=1, max_length=2000)
    duration: int = Field(default=15, ge=5, le=600)
    aspect: str = "16:9"
    style: str = Field(default="", max_length=200)
    palette: Literal["auto", "warm", "cool", "film", "ink", "vivid"] = "auto"
    shot_len: float = Field(default=5.0, ge=2.0, le=15.0)
    max_shots: int | None = Field(default=None, ge=1, le=200)
    live: bool = False
    guides: bool = False
    no_cache: bool = False
    painted: bool = False
    painted_fps: int = Field(default=24, ge=4, le=60)
    painted_quality: str = "medium"
    clean_frames: bool = False

    #: 音轨 token（由 `POST /api/media/audio` 返回），空串表示没有音轨。
    #:
    #: **不是路径**。服务端只把它当 uploads 目录里的文件名解析（`resolve_audio`），
    #: 正则挡掉分隔符、前缀检查挡掉符号链接。收任意路径等于把 ffmpeg 变成
    #: 任意文件解析器，还能靠报错内容探测文件是否存在。
    audio: str = ""
    #: 歌词原文（可带 LRC 时间戳）。为空时上屏文字退回解说词。
    lyrics: str = Field(default="", max_length=20000)
    #: song = 音乐 MV（音乐即音轨，不生成配音）；bgm = 背景配乐（保留配音）。
    #:
    #: 用 Literal 而不是 str：`MusicInput.is_bgm` 只认 "bgm" 这一个值，拼错一个
    #: 字母就静默变成 MV——这种错必须在进管线之前被 422 拦住。
    music_role: Literal["song", "bgm"] = "song"
    #: 字幕呈现方式。burn=烧进画面；sidecar=只出外置字幕；none=不要字幕。
    subtitle_mode: Literal["burn", "sidecar", "none"] = "burn"
    bpm: float = Field(default=0.0, ge=0.0, le=400.0)
    beat_offset: float = Field(default=0.0, ge=0.0, le=10.0)
    auto_beats: bool = True

    @field_validator("theme")
    @classmethod
    def _strip_theme(cls, value: str) -> str:
        text = (value or "").strip()
        if not text:
            raise ValueError("主题不能为空")
        return text

    @field_validator("aspect")
    @classmethod
    def _aspect(cls, value: str) -> str:
        if value not in ASPECTS:
            raise ValueError(f"画幅必须是 {'/'.join(ASPECTS)}")
        return value

    @field_validator("painted_quality")
    @classmethod
    def _quality(cls, value: str) -> str:
        if value not in QUALITIES:
            raise ValueError(f"画质必须是 {'/'.join(QUALITIES)}")
        return value


class RunAccepted(BaseModel):
    job_id: str
    theme: str
    running: bool


class AudioUpload(BaseModel):
    """一次音轨上传的结果。

    `token` 是后续请求里 `audio` 字段唯一合法的值；`name` 只用于界面显示，
    服务端**不会**拿它拼路径——文件名里的中文、空格、`..` 都不该有机会
    走到文件系统调用上。
    """

    token: str
    name: str
    size: int
    #: 秒。探测失败的文件不会走到这里（`save_audio` 直接 422）。
    duration: float
    #: ffprobe 的 format_name，如 "mp3" / "wav"。用于界面显示真实容器格式。
    format: str


class JobState(BaseModel):
    job_id: str
    theme: str
    running: bool
    finished: bool
    failed: bool
    error: str = ""
    cursor: int = 0
    video: str = ""
    result: dict | None = None


class Meta(BaseModel):
    version: str
    tagline: str
    presets: list[dict]
    aspects: list[str]
    qualities: list[dict]


class EnvCheck(BaseModel):
    name: str
    state: Literal["ok", "warn", "bad"]
    detail: str = ""


# ------------------------------------------------------------------ 拆解层
class ShotPatch(BaseModel):
    """单镜改动。**只改传了的字段**——不传等于"不动"，不是"清空"。

    前端整行提交也可以：它读回来的值原样发回去，没改的字段自然等于原值。
    """

    #: 期望的项目版本。带上它就能把"读到旧版本 → 覆盖新版本"变成明确 409。
    expected_revision: int | None = Field(default=None, ge=0, exclude=True)
    duration: float | None = Field(default=None, gt=0, le=60)
    visual: str | None = None
    camera: str | None = None
    narration: str | None = None
    caption: str | None = None
    prompt_image: str | None = None
    prompt_video: str | None = None
    acceptance: str | None = None


class SubjectPatch(BaseModel):
    """角色卡改动。`visual_lock` 是**文本**锁定，不是参考图。

    改它不会重写任何镜头的提示词：出图时按当前锁定拼接（见
    `Pipeline._locked_prompt`），已拍好的画面仍需单镜重拍才会用上新锁定。
    """

    expected_revision: int | None = Field(default=None, ge=0, exclude=True)
    name: str | None = Field(default=None, min_length=1, max_length=40)
    visual_lock: str | None = Field(default=None, max_length=400)


class ScriptPatch(BaseModel):
    """剧本改动。改文字不改结构时用它；`acts` 传整个数组替换。"""

    expected_revision: int | None = Field(default=None, ge=0, exclude=True)
    title: str | None = None
    logline: str | None = None
    tone: str | None = None
    target_audience: str | None = None
    hook: str | None = None
    narration_full: str | None = None
    acts: list[dict] | None = None


class ReplanRequest(BaseModel):
    """按场重拆。

    `force` 是唯一的开关：这一场若有你手改过的镜头，默认拒绝，把镜号摆出来；
    确认要覆盖才带 `force=true`。自动覆盖是最不能做的那种"智能"。
    """

    force: bool = False
    expected_revision: int | None = Field(default=None, ge=0, exclude=True)


class ReorderRequest(BaseModel):
    """重排镜头顺序。

    收的是**完整置换**：必须把现有全部镜号一个不落地列出来。允许缺项就等于
    允许"悄悄删镜头"——成片少两镜、日志一切正常，是最难查的一类数据丢失。
    """

    order: list[int] = Field(min_length=1)
    expected_revision: int | None = Field(default=None, ge=0, exclude=True)


class SubtitleLine(BaseModel):
    """一条人工字幕。时间以成片时间为准（未叠加项目整体偏移）。"""

    start: float = Field(ge=0, le=36000)
    end: float = Field(gt=0, le=36000)
    text: str = Field(min_length=1, max_length=200)


class SubtitlesBody(BaseModel):
    """整条字幕轨的替换。

    `lines=None` = 恢复按镜头/歌词自动生成；`lines=[]` = 明确不要字幕。
    两者必须能区分：否则"删光所有行"会被当成"恢复自动"，用户会发现删了个寂寞。
    """

    expected_revision: int | None = Field(default=None, ge=0, exclude=True)
    lines: list[SubtitleLine] | None = Field(default=None, max_length=500)
    #: 字幕呈现方式；None = 不改当前设置。
    subtitle_mode: Literal["burn", "sidecar", "none"] | None = None


class SubtitleStyleBody(BaseModel):
    """换一套整句字幕样式。未知 id 由接口回 422 并列出可选值。"""

    expected_revision: int | None = Field(default=None, ge=0, exclude=True)
    preset: str = Field(min_length=1, max_length=32)


class ExportRequest(BaseModel):
    """成片导出规格。只支持「从原片派生」，不重新出图。

    `fit` 用模糊背景补边、保留完整画面；`crop` 中心裁切、可能切到靠边字幕。
    """

    aspect: Literal["16:9", "9:16", "1:1"]
    mode: Literal["fit", "crop"]
    #: 导出规格预设（standard / douyin / shipinhao / bilibili / high / small）。
    #: 用 str 而不是 Literal：预设清单由 exports.PRESETS 单一维护，
    #: 这里再抄一份字面量迟早对不上。
    preset: str = "standard"


class BatchExportRequest(BaseModel):
    """一次选多个规格批量导出。重复项由后端去重，原片已是目标画幅的会跳过。"""

    targets: list[ExportRequest] = Field(min_length=1, max_length=8)


class PaletteBody(BaseModel):
    """项目色板。只改配色，不改题材、文案与分镜。"""

    expected_revision: int | None = Field(default=None, ge=0, exclude=True)
    palette: Literal["auto", "warm", "cool", "film", "ink", "vivid"]


class BrandRequest(BaseModel):
    """品牌模板。只作用于导出层，不改原片、不改变时长与音轨。"""

    expected_version: int | None = Field(default=None, ge=0, exclude=True)
    name: str = Field(default="", max_length=40)
    intro_text: str = Field(default="", max_length=60)
    outro_text: str = Field(default="", max_length=60)
    position: Literal["bottom-right", "bottom-left", "top-right", "top-left"] = "bottom-right"
    opacity: float = Field(default=0.85, ge=0.1, le=1.0)
    intro_seconds: float = Field(default=2.0, ge=0.0, le=8.0)
    outro_seconds: float = Field(default=2.0, ge=0.0, le=8.0)


class RenderRequest(BaseModel):
    """按拆解文件拍摄。分镜已经定稿，这里只剩"怎么拍"的选项。

    **默认全部为 None**：不传 = 继承项目里保存的 `render_profile`。以前这里
    默认 `live=True`，于是从拆解页点「开始拍摄」（只传 run_id）会把一条离线
    拆解悄悄拍成"真实模型"，用户的选择在换页之后被默认值覆盖了。
    """

    #: 期望的项目版本。拍摄前前端会先 flush 未落盘的编辑，再带上最新版本；
    #: 版本不符回 409，避免"拍的是旧分镜"。
    expected_revision: int | None = Field(default=None, ge=0, exclude=True)
    live: bool | None = None
    painted: bool | None = None
    painted_fps: int | None = Field(default=None, ge=4, le=60)
    painted_quality: str | None = None
    clean_frames: bool | None = None
    #: 色板。None = 继承项目保存的 palette；值 = 覆盖本次拍摄。
    palette: Literal["auto", "warm", "cool", "film", "ink", "vivid"] | None = None
    #: MV / 配乐模式。给路径即以音频为时长权威，歌词按 lyrics 上屏。
    audio: str = ""
    lyrics: str = ""
    #: 与 `RunRequest.music_role` 同样用 Literal：这里若写 str，`render_plan` 的
    #: `"bgm" if role == "bgm" else "song"` 会把任何拼错的值静默当成 MV。
    music_role: Literal["song", "bgm"] = "song"
    bpm: float = 0.0
    beat_offset: float = 0.0
    auto_beats: bool = True
    #: 混音电平。0 = 静音，1 = 原值。None = 继承项目保存的 audio_edit。
    voice_gain: float | None = Field(default=None, ge=0, le=2)
    music_gain: float | None = Field(default=None, ge=0, le=2)
    #: 对白优先：人声出现时自动压低垫乐。 None = 继承项目保存值。
    voice_duck: bool | None = None
    #: 字幕整体偏移（秒）。SRT 与烧录字幕使用同一时间轴。
    subtitle_offset: float | None = Field(default=None, ge=-10, le=10)
    #: 字幕呈现方式。None = 继承项目保存值（默认 burn）。
    subtitle_mode: Literal["burn", "sidecar", "none"] | None = None
    #: 镜头间转场。None = 继承项目保存值（默认 none）。
    transition: Literal["none", "fade", "slide", "black"] | None = None
    #: 字幕卡入场淡入。None = 继承项目保存值（默认关）。
    text_anim: bool | None = None

    @field_validator("painted_quality")
    @classmethod
    def _quality(cls, value: str | None) -> str | None:
        if value is not None and value not in QUALITIES:
            raise ValueError(f"画质必须是 {'/'.join(QUALITIES)}")
        return value
