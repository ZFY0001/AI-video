"""一言片场 · 主流程编排。

一次运行 = 简报 → 剧本 → 分镜 → (逐镜头：路由 → 生成 → 质检 → 重试) → N 选 1 → 完成度终检 → 合成。

铁律（写死在代码里，不依赖任何模型）：
1. 每个镜头独立闭环，任一镜头失败不影响其他镜头；
2. 质检分数低于阈值必须走重试，重试动作受 2.6 升级规则强制约束；
3. 付费调用前必过 tool-guard，deny 一律降级为本地方案；
4. 未过终检不许交付，宁缺毋滥。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from .common import stable_hash
from .beats import estimate_beats, manual_grid
from .clipcache import clip_key as build_clip_key
from .clipcache import state as clip_state
from .clipcache import write_key as write_clip_key
from .compose import compose, fit_clip, probe_resolution
from .config import UPLOADS_DIR, RunContext, Settings, resource_dir
from .costs import CLOUD_VIDEO_CNY_PER_SECOND as CLOUD_COST_CNY
from .costs import record as record_cost
from .decide import REVIEW_RULE_VERSION, DecisionTrace, RuleDecider, apply_escalation, judge
from . import gencache as gcache
from .genre import Genre, pick_genre
from .image_render import (
    RESOLUTIONS,
    half_size,
    render_overlay,
    render_still,
    render_subtitle_card,
)
from .images import build_provider_chain, generation_size, inspect_image, provider_status
from .llm import CHARS_PER_SEC, CreativeLayer, OpenAIChatClient
from .lyrics import assign_captions, parse_lyrics
from .models import (
    MAX_SHOT_SECONDS,
    MIN_SHOT_SECONDS,
    MusicInput,
    ProjectBrief,
    Script,
    Shot,
    Storyboard,
    brief_from_dict,
    build_timeline,
    caption_of,
    script_from_dict,
    storyboard_from_dict,
)
from .palette import apply as apply_palette
from .palette import resolve as resolve_palette
from .subtitle_style import resolve_style
from .prompts import PromptLibrary
from .runlog import RunLog
from .tts import MAX_TEMPO, build_tts_chain, concat_audio, fit_audio
from .video import (
    build_video_chain,
    ffmpeg_available,
    ffmpeg_filesystem_accessible,
    probe_duration,
)

MAX_ATTEMPTS = 3          # 提示词库 2.4 / 2.6 的 max_attempts
DEEP_REVIEW_CANDIDATES = 3  # 2.3 deep_review 时的候选数
#: 节拍估计置信度下限。低于它就放弃按拍切镜——宁可不跟拍，也不要按错拍切。
BEAT_MIN_CONFIDENCE = 0.25


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        lib: PromptLibrary,
        ctx: RunContext,
        log: RunLog,
        creative: CreativeLayer,
        decider: Any,
        avg_shot_len: float = 5.0,
        guides: bool = False,
        progress: Callable[[str, float], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
        preview: bool = False,
        voice_gain: float = 1.0,
        music_gain: float = 0.28,
        voice_duck: bool = False,
        subtitle_offset: float = 0.0,
        subtitle_lines: list | None = None,
        subtitle_mode: str = "burn",
        subtitle_style: str = "clean",
        transition: str = "none",
        text_anim: bool = False,
        palette_id: str = "auto",
        no_cache: bool = False,
    ):
        self.settings = settings
        self.lib = lib
        self.ctx = ctx
        self.log = log
        self.creative = creative
        self.decider = decider
        self.avg_shot_len = avg_shot_len
        self.guides = guides
        #: MV 模式的音乐输入。run() 里按参数赋值；空对象 = 原来的「纯一句话」模式。
        self.music: MusicInput = MusicInput()
        #: 解析后的歌词行（Step 8）。空表示本次没有歌词或歌词不可用。
        self.lyric_lines: list = []
        #: 单镜重拍：只重拍这一镜，其余镜头复用磁盘上的旧片段。0 = 整片拍摄。
        #:
        #: 由 `render_from_plan(only_shot=...)` 赋值。用 0 而不是 None 当默认值，
        #: 是因为镜号从 1 开始，0 天然表示"没有指定"。
        self.only_shot: int = 0
        # 真实阶段进度回调：由界面消费。之前进度是在启动前一次性推完的假进度，
        # 条子一上来就跳到 80%，之后长时间不动，看着像卡死。
        self.progress = progress or (lambda stage, pct: None)
        # 取消信号：界面点「停止」时为 True。在镜头之间检查，
        # 不在单个 FFmpeg/ComfyUI 调用中间强杀（那样会留下半截文件）。
        self.should_stop = should_stop or (lambda: False)
        self.stopped = False
        self.genre: Genre = pick_genre("")
        self.image_chain = build_provider_chain(settings, resource_dir("workflows"))
        #: 低清预览：成片尺寸减半、工作画布不再放大。像素少四倍，出图与编码都快；
        #: 代价是细节与文字锐度下降——它只用来确认时间轴/字幕/音轨，不替代正式成片。
        self.preview = bool(preview)
        # 运动画布倍率：背景放大 2 倍做 Ken Burns，文字层同倍率渲染保持锐利。
        # 预览降到 1 倍，避免在已经减半的成片上再做一轮无谓的 2 倍绘制。
        self.work_scale = 1 if self.preview else 2
        #: `_make_clip` 的干净背景/文字层倍率；预览用 0.5 直接按半分辨率绘制。
        self.still_scale = 0.5 if self.preview else float(self.work_scale)
        #: `_generate_still` 的占位帧倍率。正常仍是 1 倍（保持原有产物尺寸）。
        self.fixture_scale = 0.5 if self.preview else 1.0
        #: 混音与字幕设置。重合成会复用它们，所以改电平/校准字幕不需要重画。
        self.voice_gain = max(0.0, float(voice_gain))
        self.music_gain = max(0.0, float(music_gain))
        #: 对白优先：人声出现时自动压低垫乐。只影响混音，重合成即可生效。
        self.voice_duck = bool(voice_duck)
        #: 镜头间转场与字幕入场动效。都是合成期参数，不需要重拍片段。
        self.transition = str(transition or "none").lower()
        self.text_anim = bool(text_anim)
        self.subtitle_offset = float(subtitle_offset)
        #: 人工字幕轨。None = 自动生成；空列表 = 明确不要字幕。
        self.subtitle_lines = subtitle_lines
        #: 字幕呈现方式：burn=烧进画面；sidecar=只出 SRT/LRC；none=不要字幕。
        mode = str(subtitle_mode or "burn").strip().lower()
        self.subtitle_mode = mode if mode in ("burn", "sidecar", "none") else "burn"
        #: 整句字幕样式模板。只影响最终叠加的字幕卡，不重出片段。
        self.subtitle_style = resolve_style(subtitle_style)
        #: 项目色板。只替换 Genre 的调色板，不改题材与文案。
        self.palette_id = resolve_palette(palette_id)
        #: 用户明确要求不使用片段缓存（整片重拍时重新出每一镜）。
        self.no_cache = bool(no_cache)
        self.traces: list[DecisionTrace] = []
        self._video_size: tuple[int, int] = RESOLUTIONS["16:9"]
        self.video_chain: list[Any] = []
        self.tts_chain = build_tts_chain(settings)
        #: 视觉评审客户端。按 VLM_* 配置单独建，避免把 VLM_MODEL 配了却仍用
        #: LLM_MODEL 请求图像——那样"开了视觉评审"实际还是文本模型。
        self._vlm_client: Any = None
        self._workflow_hash: str | None = None

    # ------------------------------------------------------------ 决策包装
    def _decide(self, point: str, fn, *args, **kwargs) -> dict:
        """统一决策入口：计时、记日志、失败回退到本地规则（Jev 挂了不能拖垮管线）。"""
        t0 = time.perf_counter()
        source = getattr(self.decider, "source", "?")
        try:
            response = fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            self.log.error(f"decision:{point}", str(exc))
            fallback = RuleDecider(self.lib)
            method = getattr(fallback, point)
            response = method(*args, **kwargs)
            source = "rule(fallback)"
            self.ctx.notes.append(f"{point} 决策回退本地规则：{exc}")
        ms = (time.perf_counter() - t0) * 1000
        request = {"point": point, "args": _short_args(args, kwargs)}
        self.log.decision(point, request, response, source, ms)
        self.traces.append(DecisionTrace(point, request, response, source, ms))
        return response

    # ------------------------------------------------------------ 主流程
    def build_plan(
        self,
        raw_input: str,
        total_duration: int | None = None,
        aspect_ratio: str | None = None,
        style: str | None = None,
        max_shots: int | None = None,
    ) -> tuple[Any, Any, Any]:
        """只做创意层三段：简报 → 剧本 → 分镜，然后停手。

        不渲染、不合成、不碰 GPU。run() 也走这里，所以"先拆解再拍"和"一次跑完"
        拿到的一定是同一份分镜——两条路径不会分叉。
        """
        # 1. 创意层
        self.progress("创意层 · 简报", 0.10)
        brief = self.creative.make_brief(
            raw_input,
            overrides={
                "total_duration": total_duration,
                "aspect_ratio": aspect_ratio,
                "style": style,
            },
        )
        self._setup_for_brief(brief, raw_input)
        self.log.event("brief", f"项目简报：{brief.theme}", brief=brief.to_dict(), genre=self.genre.label)

        self.progress("创意层 · 剧本", 0.16)
        script = self.creative.make_script(brief)
        self.log.event("script", f"剧本：{script.title}", script=script.to_dict())

        self.progress("分镜", 0.22)
        board = self.creative.make_storyboard(brief, script, self.avg_shot_len)
        if max_shots and len(board.shots) > max_shots:
            board.shots = board.shots[:max_shots]
            board.project["total_duration"] = round(sum(s.duration for s in board.shots), 3)
        # MV 模式：把时间轴精确对齐到音频时长。brief.total_duration 是**整数秒**
        # （大模型契约如此），而音乐常带小数位——差个零点几秒就会让 -shortest
        # 提前切掉音乐尾巴，所以这里用探测到的精确时长重排一次。
        # 配乐模式时长权威在解说，但对齐拍点同样有意义，所以两种角色都走这一步。
        # 给了拍点就顺带按拍切镜（snap_to_beats 只动中间切点，末镜锁在成片末尾）。
        if self.music.enabled:
            target_total = (self.music.duration
                            if (not self.music.is_bgm and self.music.duration > 0)
                            else board.total_duration)
            build_timeline(board.shots, target_total, beats=self.music.beats or None)
            board.project["total_duration"] = round(board.total_duration, 3)

        self._finalize_board(board)
        return brief, script, board

    # ------------------------------------------------------------ 共用准备段
    def _setup_for_brief(self, brief: ProjectBrief, extra_text: str = "") -> None:
        """按简报定题材、画幅与视频后端。

        ``build_plan()`` 与 ``render_from_plan()`` 都要走这一步：后者的简报是从
        文件读回来的，但题材判定与成片分辨率仍得照它重算，否则「先拆后拍」会
        沿用管线的默认 16:9。
        """
        self.genre = pick_genre(brief.theme + " " + brief.style + " " + extra_text)
        self.genre = apply_palette(self.genre, self.palette_id)
        # 成片分辨率随画幅走（9:16 / 1:1 也是 1080 短边）；预览取一半。
        size = RESOLUTIONS.get(brief.aspect_ratio, RESOLUTIONS["16:9"])
        self._video_size = half_size(size) if self.preview else size
        self.video_chain = build_video_chain(self.settings, *self._video_size)

    def _apply_lyrics(self, board: Storyboard) -> None:
        """把歌词铺到镜头上（MV 模式的上屏文字）。

        必须在时间轴定稿**之后**做：歌词是按「起点落在哪个镜头内」分配的，
        时长一改落点就变。LRC 用自带时间戳；纯文本按字数加权铺满，
        给了拍点就把边界也吸到拍上。
        """
        if not (self.music.enabled and self.music.caption_source == "lyrics"
                and self.music.lyrics.strip()):
            return
        span = self.music.duration if self.music.duration > 0 else board.total_duration
        self.lyric_lines = parse_lyrics(self.music.lyrics, span, self.music.beats or None)
        if not self.lyric_lines:
            self.ctx.notes.append("歌词解析后为空，本次字幕仍用解说词")
        elif self.music.is_bgm:
            # 配乐模式有配音解说：字幕必须与**念出来的内容**一致，否则声画不符。
            # 歌词仍然解析出来（供 SRT 之外的用途与人工核对），但不覆盖字幕。
            self.log.event(
                "lyrics",
                f"歌词已解析 {len(self.lyric_lines)} 行；配乐模式字幕仍用解说词"
                "（要与配音一致）",
            )
        else:
            filled = assign_captions(board.shots, self.lyric_lines)
            self.log.event(
                "lyrics",
                f"歌词上屏：{len(self.lyric_lines)} 行 ｜ "
                f"{filled}/{len(board.shots)} 个镜头取得歌词",
            )

    def _finalize_board(self, board: Storyboard) -> None:
        """分镜定稿：歌词上屏、题材标注、落盘 storyboard.raw.json、报降级原因。"""
        self._apply_lyrics(board)
        self._attach_genre(board)
        self.log.event(
            "storyboard",
            f"分镜完成：{len(board.shots)} 镜 / {board.total_duration:.1f}s",
            shots=len(board.shots),
            duration=board.total_duration,
        )
        (self.ctx.project_dir / "storyboard.raw.json").write_text(
            json.dumps(board.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        # 创意层的降级原因必须让用户看到：配了 Key 却因为格式不合契约而回退到
        # 离线引擎时，成片照出，但文案会很"模板"——不报出来就永远查不到原因。
        for reason in getattr(self.creative, "degradations", []):
            self.ctx.notes.append(f"创意层降级：{reason}")
            self.log.event("creative", f"创意层降级：{reason}")

    def run(
        self,
        raw_input: str,
        total_duration: int | None = None,
        aspect_ratio: str | None = None,
        style: str | None = None,
        max_shots: int | None = None,
        music: MusicInput | None = None,
    ) -> dict[str, Any]:
        """一次跑完：拆解 + 拍摄。

        内部就是 `build_plan()` 与 `_render_and_compose()` 的串联，与"先拆后拍"
        走的是同一份分镜、同一段逐镜闭环。
        """
        self.music = music or MusicInput()
        # 输入校验要排在"开始运行"之前：音乐文件不存在是**输入错误**，
        # 不是运行中的失败。等日志都开了才报，用户看到的是"跑了一次、失败了"，
        # 而真相是"根本不该开始"。
        self._check_music_source()
        self._log_start(raw_input)
        total_duration = self._prepare_music(total_duration)
        self._env_check()
        brief, script, board = self.build_plan(
            raw_input, total_duration, aspect_ratio, style, max_shots
        )
        return self._render_and_compose(brief, script, board)

    def render_from_plan(
        self,
        plan_dir: str | Path,
        total_duration: int | None = None,
        music: MusicInput | None = None,
        only_shot: int = 0,
    ) -> dict[str, Any]:
        """从拆解文件拍摄。

        **拍什么由文件决定，不由内存决定**——这是"可编辑"能成立的唯一前提。
        所以这里不调 `build_plan()`，而是把 `plan/storyboard.json` 读回来，
        再按用户改过的时长重算时间轴。

        与 `run()` 共用 `_process_shot` / `_final_check` / `compose`，只有入口不同。
        两条路径若各自长出一套逻辑，改了分镜就会得到自相矛盾的结果。

        `only_shot` 非 0 时只重拍这一镜（见 `_render_single_shot`）。
        """
        self.music = music or MusicInput()
        self.only_shot = int(only_shot or 0)
        self._check_music_source()
        brief, script, board = self._load_plan(Path(plan_dir))
        self._log_start(brief.theme, source=str(plan_dir))
        # 注意这里**不拿 brief.total_duration 兜底**：简报里的总时长是拆解当时的
        # 值，用户改了某镜时长之后它已经过时。拿它当目标会把改过的 16 秒又压回
        # 15 秒——那正是"改了等于没改"。目标时长只来自两处：调用方显式指定，
        # 或 MV 音频探测出的精确时长；两者都没有就沿用分镜自己的累加。
        total_duration = self._prepare_music(total_duration)
        self._env_check()
        if self.only_shot:
            # 单镜重拍**不能**重算时间轴：`build_timeline` 为了让累加时长命中目标
            # 会微调镜长，而其余镜头的片段是按旧镜长渲出来的——镜长一动，
            # 已有片段与新时间轴就对不上，成片会从这一镜开始整体音画错位。
            self._sync_starts(board)
        else:
            self._rebuild_timeline(board, total_duration)
        self._finalize_board(board)
        return self._render_and_compose(brief, script, board)

    def recompose_from_plan(
        self,
        plan_dir: str | Path,
        music: MusicInput | None = None,
    ) -> dict[str, Any]:
        """只重合成：一个像素都不重画，按 `storyboard.json` 里现在的镜长与顺序重拼。

        与 `render_from_plan` 只差一句"重不重新出图"。时间轴编辑改的是**怎么拼**
        ——镜长、先后、总长，画面内容一点没动。把"改拼接"做成"重拍一遍"，用户
        改一次时长就要等一小时，那这个功能等于没有。

        代价是画面得迁就新时长：长了裁尾、短了冻结末帧（`fit_clip`）。这是只重排
        不重画的固有取舍，没有第二种解法——除非重新出图。
        """
        self.music = music or MusicInput()
        self.only_shot = 0
        self._check_music_source()
        brief, script, board = self._load_plan(Path(plan_dir))
        self._log_start(brief.theme, source=str(plan_dir))
        # 这里**必须**丢掉 `_prepare_music` 返回的 MV 时长：它算出来的是音乐长度，
        # 而用户刚亲手定下每一镜几秒。拿音乐长度去压时间轴等于把他的话改回去。
        # 音乐只是音轨，长了就在片尾停住、短了就循环——`compose` 本来就那么做。
        target = self._prepare_music(None)
        # 同理不重算镜长：`_rebuild_timeline` 会为了让累加命中目标而微调每一镜，
        # 那是"新建时间轴"该干的事，不是"编辑已有时间轴"。
        self._sync_starts(board)
        self._env_check()
        self._finalize_board(board)
        return self._recompose(brief, script, board, target)

    # ------------------------------------------------------------ 按场重拆
    def replan_act(
        self,
        plan_dir: str | Path,
        act_name: str,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        """只重拆一场，其余场次的镜头原样保留。

        **这一期最难的不是技术，是"改一句话该连累哪些镜"的判定规则**。
        这里按"同场的镜头"粗放处理：剧本里改的是第 N 场，就只重拆第 N 场，
        不做语义级依赖分析——做不到准确的事，做了只会误导。

        三条硬约束：

        1. **不碰别的场**。其余场次的镜头对象连 `id` 都不改（只在必要处顺移），
           所以逐镜调过的时长、画面、台词都保得住。
        2. **总时长不变**。目标时长锁定为"这一场原来的时长之和"，重拆出来的
           镜头按这个额度重新分配。否则改一句话就让整片从 15 秒变 22 秒。
        3. **改过的镜头要显式确认**。这一场若有 `edited.shots`，`force=False`
           时直接拒绝，把镜号摆出来由人决定。自动覆盖是最不能做的那种"智能"。

        `force` 在这里只影响返回值的 `overwritten` 字段（列出被覆盖的镜号），
        是否真的允许覆盖由调用方（`server/plan.py`）判定——因为只有它读得到
        `meta.json` 里的 `edited.shots`。
        """
        plan_dir = Path(plan_dir)
        brief, script, board = self._read_plan(plan_dir)
        act_names = [str(a.get("name") or "") for a in (script.acts or [])]
        if act_name not in act_names:
            raise RuntimeError(
                f"剧本里没有「{act_name}」这一场。现有场次：{'、'.join(n for n in act_names if n) or '（无）'}"
            )

        old_shots = [s for s in board.shots if s.act == act_name]
        old_ids = [s.id for s in old_shots]
        target = round(sum(float(s.duration) for s in old_shots), 3)

        act = next((a for a in (script.acts or []) if str(a.get("name") or "") == act_name), {})
        act_text = str(act.get("content") or act.get("summary") or "")
        if target <= 0:
            # 这一场原本一个镜头都没有（剧本里写了戏，但分镜漏了）。
            # 没有时长额度可锁定，就先看剧本自己写的 time_range（"12-16s"），
            # 那是作者给这一场划的时间，比拍脑袋给一个平均镜长准得多。
            target = self._act_span(act) or max(float(MIN_SHOT_SECONDS), float(self.avg_shot_len))

        # 衔接用的邻镜：取这一场之前的最后一镜与之后的第一个镜头。
        first_at = next((i for i, s in enumerate(board.shots) if s.act == act_name), None)
        if first_at is None:
            first_at = self._insert_index(board, act_names, act_name)
        prev_shot = board.shots[first_at - 1].to_dict() if first_at > 0 else None
        next_shot = board.shots[first_at + len(old_shots)].to_dict() \
            if first_at + len(old_shots) < len(board.shots) else None

        self.log.event(
            "plan",
            f"按场重拆：「{act_name}」（原 {len(old_shots)} 镜 / {target:.1f}s）",
            act=act_name,
            old_shots=old_ids,
            target=target,
        )
        new_shots = self.creative.make_act_shots(
            brief,
            script,
            act_name,
            act_text,
            target,
            avg_shot_len=self.avg_shot_len,
            previous=[s.to_dict() for s in old_shots],
            subjects=list(board.subjects),
            prev_shot=prev_shot,
            next_shot=next_shot,
        )
        if not new_shots:
            raise RuntimeError(f"场次「{act_name}」重拆后没有任何镜头")

        # 把新镜头的时长按额度摊平。用 build_timeline 而不是自己算比例：
        # 它会同时把单镜时长夹回 [下限, 上限]，避免重拆出来一个 1.2 秒的闪帧。
        build_timeline(new_shots, target)

        # 拼回去：这一场的旧镜头整段换掉，其余场次的对象原样不动。
        # 重排镜号前先按**对象身份**记下旧镜号——新镜头的 id 是场内序号，
        # 会和别的场的旧镜号撞上，用镜号做键会串号。
        before_ids = {id(shot): int(shot.id) for shot in board.shots}
        # 按 act 过滤而不是按切片删：模型偶尔会把两场的镜头交错排（起承起承），
        # 切片删除会误伤别的场。这里保证"这一场的所有镜头"被整体摘掉。
        merged: list[Shot] = [s for s in board.shots[:first_at]]
        merged += list(new_shots)
        merged += [s for s in board.shots[first_at:] if s.act != act_name]
        board.shots = merged

        # 全片重排镜号。这里必须记 id_map：`edited.shots` 存的是镜号，
        # 后面场的镜头会顺移，不映射的话"改过"的徽章会标在错的镜头上。
        # 新镜头不在 before_ids 里，因此不会产生映射——它没有"旧号"可言。
        id_map: dict[int, int] = {}
        for i, shot in enumerate(board.shots, 1):
            old = before_ids.get(id(shot))
            if old is not None:
                id_map[old] = i
            shot.id = i
        board.assign_timeline()
        board.project["total_duration"] = board.total_duration

        self.log.event(
            "plan",
            f"按场重拆完成：「{act_name}」{len(old_shots)} 镜 → {len(new_shots)} 镜，"
            f"全片 {len(board.shots)} 镜 / {board.total_duration:.1f}s",
            act=act_name,
            before=len(old_shots),
            after=len(new_shots),
        )
        return {
            "act": act_name,
            "storyboard": board.to_dict(),
            "shots": [s.to_dict() for s in new_shots],
            "old_ids": old_ids,
            "total_duration": board.total_duration,
            "target": target,
            "id_map": id_map,
            "overwritten": old_ids if force else [],
            "notes": list(self.ctx.notes),
        }

    @staticmethod
    def _act_span(act: dict[str, Any]) -> float:
        """从剧本的 `time_range`（"12-16s" / "0-4s"）读出这一场该有多长。

        读不出来就返回 0，由调用方兜底——猜一个数字不如说"我不知道"。
        """
        text = str(act.get("time_range") or "")
        nums = re.findall(r"\d+(?:\.\d+)?", text)
        if len(nums) < 2:
            return 0.0
        try:
            start, end = float(nums[0]), float(nums[1])
        except ValueError:
            return 0.0
        span = round(end - start, 3)
        return span if MIN_SHOT_SECONDS <= span <= 600 else 0.0

    def _insert_index(self, board: Storyboard, act_names: list[str], act_name: str) -> int:
        """这一场若一个镜头都没有，新镜头该插在哪。

        按剧本里的场次顺序找第一个"排在它后面的场"的首镜，插在那一镜之前。
        这样补出来的镜头落在正确的位置，而不是一律甩到片尾。
        """
        try:
            order = act_names.index(act_name)
        except ValueError:
            return len(board.shots)
        later = set(act_names[order + 1:])
        for i, shot in enumerate(board.shots):
            if shot.act in later:
                return i
        return len(board.shots)

    # ------------------------------------------------------------ 开跑前的准备
    def _log_start(self, raw_input: str, source: str = "") -> None:
        fields: dict[str, Any] = {
            "mode": "live" if self.ctx.live else "offline",
            "input": raw_input,
            "settings": self.settings.summary(),
        }
        if source:
            fields["plan_dir"] = source
        self.log.event("run", "开始运行", **fields)

    def _check_music_source(self) -> None:
        """MV / 配乐模式：音频文件必须真的在。

        放在"探测"之前单独一步，是因为它属于**输入校验**：应该在开日志、
        建目录、任何耗时的动作之前就顶回去。之前它埋在 `_prepare_music()` 里，
        而那个函数被排到 `_log_start()` 之后，于是"文件不存在"变成了一次
        跑完又失败的任务——用户看到的现象和真相完全是两回事。
        """
        if not self.music.enabled:
            return
        src = Path(self.music.path)
        if not src.exists():
            raise RuntimeError(f"音乐文件不存在：{src}")

    def _adopt_music_source(self) -> None:
        """把上传目录里的音轨复制进本次运行目录，让运行自带音轨。

        上传目录是按 TTL 自动清理的。一次拆解可能在几天后才被点「开始拍摄」，
        那时原文件已经没了——而它明明是一份有效的工作成果。复制之后，
        `runs/<run_id>/` 是自足的，上传目录可以放心删。

        只对上传目录里的文件这么做：命令行 `--audio D:\\music.mp3` 给的是本机
        路径，长期有效，没必要每次多占一份空间。

        复制失败**不致命**：保留原路径、往 notes 里记一句。复制是个优化，
        不该让它把整次运行搞挂。
        """
        src = Path(self.music.path)
        try:
            src.resolve().relative_to(UPLOADS_DIR.resolve())
        except (ValueError, OSError):
            return

        target = self.ctx.work_dir / f"music{src.suffix}"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, target)
        except OSError as exc:
            self.ctx.notes.append(
                f"音轨未能复制进运行目录（{exc}），原文件被清理后这份记录将无法重拍"
            )
            return
        self.music.path = str(target)

    def _prepare_music(self, total_duration: int | None) -> int | None:
        """音乐探测与节拍估计。返回修正后的总时长（MV 模式下以音频为准）。"""
        # MV 模式：给了音乐就以音频为总时长权威，并把它记进上下文供后续层消费。
        if self.music.enabled:
            self._adopt_music_source()
            src = Path(self.music.path)
            probed = probe_duration(src)
            if probed > 0:
                self.music.duration = probed
            if self.music.is_bgm:
                # 配乐模式：**时长权威是解说**（用户/大模型给的），音乐只负责垫底，
                # 短了就循环铺满。反过来让 3 分钟的歌把 60 秒解说拉长是不合理的。
                if probed <= 0:
                    self.ctx.notes.append(
                        f"背景音乐时长探测失败（{src}），按整片时长循环使用"
                    )
            elif probed > 0:
                # MV 模式：画面服从音乐——总时长以音频为准
                total_duration = max(4, int(round(probed)))
            else:
                # 探测失败不静默：退回用户给的时长，但必须说出来
                self.ctx.notes.append(
                    f"音乐时长探测失败（{src}），本次沿用指定时长"
                    f"{total_duration or '（默认）'}，画面可能与音乐不同步"
                )

            # 节拍：MV 的切点要踩在拍上。两条路——用户手工给了 BPM 就采信手工值；
            # 否则自动估计。估计不可靠时**放弃吸附并说明**，绝不拿一个错的网格去切镜头。
            grid = None
            if self.music.bpm > 0:
                grid = manual_grid(self.music.bpm, self.music.offset)
            elif self.music.auto_beats and probed > 0:
                try:
                    grid = estimate_beats(src)
                except Exception as exc:  # noqa: BLE001
                    self.ctx.notes.append(f"节拍估计失败（{exc}），本次不按拍切镜")
                    grid = None
            if grid is not None and grid.ok:
                if grid.confidence >= BEAT_MIN_CONFIDENCE:
                    self.music.bpm = grid.bpm
                    self.music.offset = grid.offset
                    self.music.beat_source = grid.source
                    span = probed if probed > 0 else 0.0
                    self.music.beats = grid.beats_until(span) if span > 0 else []
                    self.log.event(
                        "beats",
                        f"节拍：{grid.bpm:.1f} BPM ｜ 首拍 {grid.offset:.2f}s ｜ "
                        f"置信度 {grid.confidence:.2f} ｜ {len(self.music.beats)} 个拍点"
                        f"（{grid.source}）",
                        beats=grid.to_dict(),
                    )
                else:
                    self.ctx.notes.append(
                        f"节拍估计不可靠（{grid.bpm:.1f} BPM，置信度 {grid.confidence:.2f}），"
                        "本次不按拍切镜；确知速度时可用 --bpm 手工指定"
                    )
            if self.music.is_bgm:
                role_text, total_text = "背景配乐（保留配音）", "片长以解说为准"
            else:
                role_text, total_text = "音乐 MV（不生成配音）", "总时长以音频为准"
            beat_text = (f" · 节拍 {self.music.bpm:.1f}BPM"
                         if self.music.bpm > 0 else " · 无节拍网格")
            self.log.event(
                "music",
                f"{role_text}：{Path(self.music.path).name} / "
                f"{self.music.duration:.1f}s（{total_text}）{beat_text}",
                music=self.music.to_dict(),
            )
            self.progress("音乐输入", 0.07)
        return total_duration

    def _env_check(self) -> None:
        """FFmpeg 可用性与写盘权限。两条都不满足就直接中止，不留半成品。"""
        # 0. 环境体检
        self.progress("环境体检", 0.05)
        ff_ok, ff_detail = ffmpeg_available()
        self.log.event("env", f"FFmpeg {'可用' if ff_ok else '不可用'}：{ff_detail}")
        for item in provider_status(self.settings, resource_dir("workflows")):
            self.log.event("env", f"出图 provider {item['provider']}：{item['available']}（{item['detail']}）")
        if not ff_ok:
            raise RuntimeError("FFmpeg 不可用，无法出片")
        # 版本能跑不代表能读写磁盘：受限环境（沙箱/受控终端/个别杀软）里 ffmpeg
        # 可执行却被禁止文件 I/O，会让每一镜都在 Ken Burns 阶段以「No such file」失败。
        # 这里提前拦下并说清原因（实测踩过）。
        io_ok, io_detail = ffmpeg_filesystem_accessible()
        self.log.event("env", f"FFmpeg 写盘 {'正常' if io_ok else '异常'}：{io_detail}")
        if not io_ok:
            raise RuntimeError(io_detail + "；请在普通 PowerShell 或 CMD 中运行一言片场")

    # ------------------------------------------------------------ 从文件拍摄
    def _read_plan(self, plan_dir: Path) -> tuple[ProjectBrief, Script, Storyboard]:
        """只把拆解文件读成对象，不做拍摄相关的准备。

        拆出来是因为"按场重拆"也要读同一份文件，但它不是拍摄：套用
        `_load_plan` 会把日志写成"从拆解文件拍摄"，并且拿旧的时长做校验，
        而重拆恰恰要改时长。
        """
        if not plan_dir.is_dir():
            raise RuntimeError(f"拆解目录不存在：{plan_dir}")

        def load(name: str) -> dict:
            file = plan_dir / name
            if not file.is_file():
                raise RuntimeError(f"拆解文件缺失：{file}")
            try:
                return json.loads(file.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"拆解文件不是合法 JSON：{file}（{exc}）") from exc

        brief = brief_from_dict(load("brief.json"))
        script = script_from_dict(load("script.json"))
        board = storyboard_from_dict(load("storyboard.json"))
        if not board.shots:
            raise RuntimeError("分镜为空：至少要有 1 个镜头才能拍摄")
        return brief, script, board

    def _load_plan(self, plan_dir: Path) -> tuple[ProjectBrief, Script, Storyboard]:
        """读回拆解文件。缺文件就报错，不静默回落到空分镜。"""
        brief, script, board = self._read_plan(plan_dir)

        # 题材判定与成片分辨率得照这份简报重算，否则"先拆后拍"会沿用默认 16:9。
        self._setup_for_brief(brief, brief.theme + " " + script.title)
        self.log.event(
            "plan",
            f"从拆解文件拍摄：{len(board.shots)} 镜 / {board.total_duration:.1f}s（{plan_dir}）",
            plan_dir=str(plan_dir),
            shots=len(board.shots),
        )
        # 用户改过的时长必须先校验。放在重算时间轴**之前**：重算会把越界值
        # clamp 回上下限，那时候就再也查不出"你填的 12 秒被偷偷改成 10 秒"了。
        self._check_shots(board, "拆解文件")
        return brief, script, board

    def _sync_starts(self, board: Storyboard) -> None:
        """只重排 `start`，一个镜长都不动。

        单镜重拍必须用它而不是 `_rebuild_timeline`：后者为了让累加时长命中目标值
        会微调镜长，而其余镜头的片段是按旧镜长渲出来的。镜长一动，已有片段与新
        时间轴就对不上，成片从被改的那一镜起整体音画错位——而且不报错。

        代价是总时长可能与目标差零点几秒。单镜重拍本来就不该改总时长：
        用户点的是"重拍这一镜"，不是"把片子改成 20 秒"。
        """
        t = 0.0
        for shot in board.shots:
            shot.start = round(t, 3)
            t += float(shot.duration)
        board.project["total_duration"] = round(t, 3)
        self._check_shots(board, "单镜重拍前")
        self._check_narration(board)

    def _rebuild_timeline(
        self, board: Storyboard, total_duration: int | float | None
    ) -> None:
        """重算 `start` 与总时长。

        用户可能改了某镜时长，累加就变了；MV 模式还要求总时长与音频一致。
        `build_timeline()` 负责在 [下限, 上限] 内把偏差摊平，并按拍点吸附切点。
        """
        before = board.total_duration
        target = float(total_duration) if total_duration and total_duration > 0 else before
        build_timeline(board.shots, target, beats=self.music.beats or None)
        board.project["total_duration"] = round(board.total_duration, 3)
        after = board.total_duration
        if abs(after - target) > 1e-2:
            raise RuntimeError(
                f"时间轴重算后总时长 {after:.2f}s 与目标 {target:.2f}s 不符，已中止拍摄"
            )
        self._check_shots(board, "时间轴重算后")
        self._check_narration(board)
        if abs(after - before) > 0.01:
            self.log.event("plan", f"时间轴重算：{before:.2f}s → {after:.2f}s",
                           before=before, after=after)
            self.ctx.notes.append(
                f"时间轴已按改过的镜长重算：总时长 {before:.2f}s → {after:.2f}s"
            )

    def _check_shots(self, board: Storyboard, stage: str = "") -> None:
        """镜数与单镜时长上下限。越界一律中止，**不静默修正**。

        用户改的值被系统偷偷改掉，比不支持编辑更糟：他会以为片子拍出来是 12 秒，
        拿到手却是 10 秒，而日志里什么也没说。

        下限用 `min(MIN_SHOT_SECONDS, 总时长 / 镜数)`，与 `build_timeline`
        里的 floor 同一个公式。写死 2 秒的话，一段 4 秒的音频被拆成 3 镜
        （每镜 1.333 秒）就永远拍不出去——而那份拆解正是 `build_timeline`
        在"总时长装不下 3 个 2 秒镜头"时给出的最优解。MV 模式下音频可以短到
        几秒，这个自相矛盾立刻暴露。
        """
        if not board.shots:
            raise RuntimeError(f"{stage}校验未通过：没有任何镜头")
        total = sum(float(s.duration) for s in board.shots)
        floor = round(min(MIN_SHOT_SECONDS, total / len(board.shots)), 3)
        bad: list[str] = []
        for shot in board.shots:
            d = float(shot.duration)
            if d < floor - 1e-6:
                bad.append(f"镜 {shot.id} 时长 {d:g}s 短于下限 {floor:g}s")
            elif d > MAX_SHOT_SECONDS + 1e-6:
                bad.append(f"镜 {shot.id} 时长 {d:g}s 超出上限 {MAX_SHOT_SECONDS:g}s")
        if bad:
            raise RuntimeError(
                f"{stage}校验未通过，请回到拆解页修正后再拍：\n" + "\n".join(bad)
            )

    def _check_narration(self, board: Storyboard) -> None:
        """台词与镜长是否还对得上。**只报告，不改写**。

        这里刻意不调 `distribute_narration()` 重排：那是拆解阶段的活，重跑一次会把
        用户逐镜改过的台词整体冲掉。人改的东西被系统偷偷改回去，比不支持编辑更糟，
        所以只把问题摆出来，由他决定是删字还是加时长。
        """
        if self.music.enabled and not self.music.is_bgm:
            return  # MV 模式没有配音，字数约束不成立
        tight: list[str] = []
        for shot in board.shots:
            cap = max(1, int(shot.duration * CHARS_PER_SEC * MAX_TEMPO))
            n = len((shot.narration or "").strip())
            if n > cap:
                tight.append(
                    f"镜 {shot.id}：{n} 字 / {shot.duration:.1f}s（约 {cap} 字为上限）"
                )
        if tight:
            msg = ("台词超出该镜能容纳的字数，配音会被压到变速上限、可能与画面对不齐："
                   + "；".join(tight))
            self.ctx.notes.append(msg)
            self.log.event("check", msg)

    # ------------------------------------------------------------ 拍摄段
    def _render_and_compose(
        self, brief: ProjectBrief, script: Script, board: Storyboard
    ) -> dict[str, Any]:
        """逐镜头闭环 → 完成度终检 → 合成。两个入口共用这一段。"""
        if self.only_shot:
            if self.preview:
                return self._render_single_shot_preview(brief, script, board)
            return self._render_single_shot(brief, script, board)

        # 2. 逐镜头闭环
        total = max(1, len(board.shots))
        for i, shot in enumerate(board.shots):
            if self._stop_requested(f"镜 {shot.id}/{total} · 已停止"):
                break
            self.progress(
                f"生成镜头 {shot.id}/{len(board.shots)}",
                0.25 + 0.55 * i / total,
            )
            self._process_shot(shot, brief, board)

        # 3. 完成度终检：**真的阻断**，不做"文字上交付"
        if self.stopped:
            self.progress("已停止", 0.82)
        else:
            self.progress("完成度终检", 0.82)
        approved = self._final_check(board)
        # 用 id 集合比较，不用 dataclass 相等：Shot 是 dataclass，
        # 值相等的两个镜头会被误判成同一个，导致终检漏报被剔除的镜头。
        approved_ids = {s.id for s in approved}
        missing = [s.id for s in board.shots if s.id not in approved_ids]
        if missing:
            self.log.event(
                "check",
                f"终检未通过：{len(missing)} 个镜头未达标，按铁律「宁缺毋滥」不剪进成片",
                missing=missing,
                delivered=len(approved),
            )
            self.ctx.notes.append(
                f"终检未通过：{len(missing)} 个镜头（{missing}）未达标，已从成片中剔除，"
                f"成片只包含 {len(approved)} 个合格镜头，需人工复核后重跑"
            )

        # 4. 合成：**只剪达标镜头**（此前按 clip_path 过滤，会把被拒镜头也剪进去）
        return self._compose_and_report(brief, script, board, approved)

    def _render_single_shot(
        self, brief: ProjectBrief, script: Script, board: Storyboard
    ) -> dict[str, Any]:
        """只重拍一镜，其余镜头复用磁盘上的旧片段，然后整片重新合成。

        为什么要有这条路：拆解页改一镜的动机通常是"这一镜画面不对"。一条 222 秒
        的 MV 有 45 镜，为了改一镜把 45 镜全渲一遍，改一次等一小时——改一镜的成本
        比整片重拍低不了多少，那"能改单镜"就只是个摆设。

        **不对其余镜头重跑终检**。它们的片段是上一次拍摄的产物，帧缓存可能已经被
        `clean_frames` 清掉了；重跑终检只会把它们判成"未达标"然后按宁缺毋滥原则
        剔除——那等于用一次单镜重拍把整条片毁掉，而且日志上看着还挺合理。
        """
        target = next((s for s in board.shots if s.id == self.only_shot), None)
        if target is None:
            raise RuntimeError(f"分镜里没有镜 {self.only_shot}，无法单镜重拍")

        total = max(1, len(board.shots))
        # 其余镜头沿用上次产物，证据也要沿用：目标镜自己会重新拍，不需要恢复。
        self._restore_reuse_state(board, skip_shot=target.id)
        # 先确认"其余镜头"的片段仍然对应当前输入，再动目标镜。否则会先花时间
        # 重拍目标镜，最后才发现别的镜头已经过期，白白等一轮。
        missing: list[int] = []
        stale: list[int] = []
        unknown: list[int] = []
        for shot in board.shots:
            if shot.id == target.id:
                continue
            clip = self.ctx.work_dir / "clips" / f"shot{shot.id:02d}.mp4"
            state = clip_state(clip, self._clip_key(shot, brief, board))
            if state == "missing":
                missing.append(shot.id)
            elif state == "stale":
                stale.append(shot.id)
            elif state == "unknown":
                unknown.append(shot.id)
        if missing:
            raise RuntimeError(
                f"镜 {missing} 的已有片段不在了（work/clips 被清理过？），没法只重拍一镜。"
                f"请改用「开始拍摄」整片重拍"
            )
        if stale:
            raise RuntimeError(
                f"镜 {stale} 的画面/文案/渲染规格改过了，但它们的片段还是旧的。"
                f"单镜重拍不会替你重画这些镜头——请先整片重拍（只会重画变化的镜头），"
                f"或逐镜重拍它们"
            )
        if unknown:
            raise RuntimeError(
                f"镜 {unknown} 的旧片段缺少缓存指纹，无法确认是否与当前分镜一致。"
                f"请先整片重拍一次建立指纹；本次没有改动任何片段"
            )
        self.log.event(
            "shot",
            f"单镜重拍：只重拍镜 {target.id}，其余 {total - 1} 镜复用已有片段",
            shot=target.id,
        )
        self.progress(f"重拍镜 {target.id}", 0.3)
        self._process_shot(target, brief, board)

        if not target.clip_path or not Path(target.clip_path).is_file():
            raise RuntimeError(
                f"镜 {target.id} 重拍后没有产出可用片段（质检未通过），成片保持原样未改动"
            )

        # 其余镜头：前面已经校验过指纹，这里只负责收集路径。
        approved: list[Shot] = []
        for shot in board.shots:
            if shot.id == target.id:
                approved.append(shot)
                continue
            clip = self.ctx.work_dir / "clips" / f"shot{shot.id:02d}.mp4"
            shot.clip_path = str(clip)
            shot.status = "approved"
            approved.append(shot)

        self.ctx.notes.append(
            f"单镜重拍：镜 {target.id} 已重拍，其余 {len(approved) - 1} 镜沿用上次的片段，"
            f"成片已重新合成"
        )
        self.progress("合成成片", 0.88)
        return self._compose_and_report(brief, script, board, approved)

    def _render_single_shot_preview(
        self, brief: ProjectBrief, script: Script, board: Storyboard
    ) -> dict[str, Any]:
        """单镜预览：只出这一镜的低清片段，不重合成、不动正式片段。

        与「单镜重拍」差在之后做什么：重拍要拿新片段和其余旧片段重拼成片；
        预览只回答"这一镜现在长什么样"，所以不碰 `work/clips`，也不产出 final.mp4。
        产物落在 preview/ 命名空间（由调用方给出的 ctx 决定）。
        """
        target = next((s for s in board.shots if s.id == self.only_shot), None)
        if target is None:
            raise RuntimeError(f"分镜里没有镜 {self.only_shot}，无法预览")

        self.log.event(
            "shot",
            f"单镜预览：只生成镜 {target.id}，不重合成、不影响正式片段",
            shot=target.id,
        )
        self.progress(f"预览镜 {target.id}", 0.3)
        self._process_shot(target, brief, board)
        if not target.clip_path or not Path(target.clip_path).is_file():
            raise RuntimeError(f"镜 {target.id} 预览没有产出可用片段（质检未通过）")

        clip = Path(target.clip_path)
        resolution = probe_resolution(clip)
        size_mb = round(clip.stat().st_size / 1024 / 1024, 2)
        summary = f"单镜预览：镜 {target.id}，{target.duration:.2f}s，{resolution or '未知分辨率'}"
        self.log.event("done", summary)
        self.progress("完成", 1.0)
        self.ctx.notes.append(
            f"单镜预览：只生成镜 {target.id} 的低清片段，没有重合成整片，"
            "正式成片与逐镜片段缓存均未改动；预览片段不带音轨"
        )
        return {
            "run_id": self.ctx.run_id,
            "project_dir": str(self.ctx.project_dir),
            "summary": summary,
            "log": str(self.log.path),
            "brief": brief.to_dict(),
            "script": script.to_dict(),
            # 整份分镜照常返回：前端要显示这是哪一镜，也便于对照其它镜的规格。
            "storyboard": board.to_dict(),
            "compose": {
                "video": str(clip),
                "subtitle": "",
                "storyboard": "",
                "duration": round(target.duration, 3),
                "resolution": resolution,
                "has_audio": False,
                "audio_source": "silent",
                "audio": "",
                "size_mb": size_mb,
                "delivered": 1,
                "dropped": 0,
                "notes": ["单镜预览片段，不是整片成片"],
            },
            "music": self.music.to_dict(),
            "lyrics": [],
            "subtitle": {
                "mode": self.subtitle_mode,
                "offset": self.subtitle_offset,
                "lines": self.subtitle_lines,
                "style": self.subtitle_style.id,
            },
            "decisions": len(self.traces),
            "notes": self.ctx.notes,
            "stopped": self.stopped,
            "shot_preview": True,
            "shot_id": target.id,
        }

    #: 复用旧片段时要一起绑回来的运行期字段。整片重拍必须清空它们（否则被跳过
    #: 的镜头会冒充"有证据"），但复用路径用的就是上次那批产物，清空等于自断指纹。
    _REUSE_STATE_FIELDS = (
        "image_sha256", "frame_path", "frame_source", "frame_is_clean",
        "evidence_id", "candidate_evidence", "review_mode", "visual_check",
        "review_rule", "tech_ok", "tech_reason", "image_width", "image_height",
        "image_mean", "image_contrast", "quality", "verdict", "defect_type",
        "attempts", "provider", "route", "intent_path",
    )

    def _restore_reuse_state(self, board: Storyboard, *, skip_shot: int = 0) -> int:
        """把上一次拍摄的证据绑回读进来的分镜，供「只重合成 / 单镜重拍」使用。

        片段指纹含选中图摘要，而 `shot_from_dict()` 读回分镜时会清空运行期字段。
        不恢复的话每一镜都会被判成"画面改过了"，时间轴编辑与单镜重拍全部失效。
        证据取自 `output/storyboard.json`——合成时写下的那份才是拍摄结果；
        按镜号匹配，找不到记录的镜头保持空摘要，让指纹校验如实拒绝。
        """
        path = self.ctx.out_dir / "storyboard.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        by_id: dict[int, dict[str, Any]] = {}
        for item in (data.get("shots") or []):
            if not isinstance(item, dict):
                continue
            try:
                by_id[int(item.get("id"))] = item
            except (TypeError, ValueError):
                continue
        restored = 0
        expected = 0
        for shot in board.shots:
            if skip_shot and shot.id == skip_shot:
                continue
            expected += 1
            record = by_id.get(shot.id)
            if not record:
                continue
            for field in self._REUSE_STATE_FIELDS:
                if field in record:
                    setattr(shot, field, record[field])
            restored += 1
        if restored < expected:
            self.ctx.notes.append(
                f"有 {expected - restored} 镜读不到上一次拍摄的证据"
                "（output/storyboard.json 缺失或不含该镜），这些镜头无法复用旧片段"
            )
        self.log.event(
            "plan", f"复用上次拍摄证据：{restored}/{expected} 镜",
            restored=restored, shots=expected, source=str(path),
        )
        return restored

    def _recompose(
        self, brief: ProjectBrief, script: Script, board: Storyboard,
        target_duration: int | None = None,
    ) -> dict[str, Any]:
        """把 `work/clips` 里已有的片段按当前时间轴对齐后重拼。

        两件事必须做对，否则成片"看着正常"但从某一镜起音画全错：

        1. **先对齐片段再拼**。片段是按旧镜长渲的，concat 只认文件本身的长度，
           不会照 `start/duration` 去裁。不 fit 就等于时间轴白改。
        2. **按 `board.shots` 的顺序传片段**。`compose` 按给定顺序拼接，
           重排镜头就是把 clips 列表换个次序——不需要改任何文件名。
        """
        src_dir = self.ctx.work_dir / "clips"
        fit_dir = self.ctx.work_dir / "clips_fit"
        fit_dir.mkdir(parents=True, exist_ok=True)
        self._restore_reuse_state(board)

        approved: list[Shot] = []
        missing: list[int] = []
        stale: list[int] = []
        unknown: list[int] = []
        refit: list[int] = []
        for shot in board.shots:
            src = src_dir / f"shot{shot.id:02d}.mp4"
            state = clip_state(src, self._clip_key(shot, brief, board))
            if state == "missing":
                missing.append(shot.id)
                continue
            if state == "stale":
                stale.append(shot.id)
                continue
            if state == "unknown":
                unknown.append(shot.id)
                continue
            dst = fit_dir / f"shot{shot.id:02d}.mp4"
            # 输出**绝不指向 src**：对齐要重编码，原地写一遍画质就掉一遍，
            # 而且裁短之后再也补不回来。源片段永远是拍摄的原始产物。
            out = fit_clip(src, float(shot.duration), dst)
            if out != src:
                refit.append(shot.id)
            shot.clip_path = str(out)
            shot.status = "approved"
            approved.append(shot)

        if missing:
            raise RuntimeError(
                f"镜 {missing} 的片段不在 work/clips 里，没法只重合成。"
                f"这几镜可能从没拍过，或者 work 目录被清理过——请用「开始拍摄」整片重拍"
            )
        if stale:
            raise RuntimeError(
                f"镜 {stale} 的画面/文案/渲染规格改过了，旧片段与当前分镜不一致，"
                f"无法只重合成。请先整片重拍（会只重画变化的镜头）"
            )
        if unknown:
            raise RuntimeError(
                f"镜 {unknown} 的旧片段缺少缓存指纹，无法确认是否与当前分镜一致。"
                f"请先整片重拍一次建立指纹；本次没有改动任何片段"
            )
        if not approved:
            raise RuntimeError("没有可用的片段，拒绝产出成片")

        total = board.total_duration
        self.log.event(
            "timeline",
            f"时间轴重合成：{len(approved)} 镜 / {total:.2f}s，"
            f"{len(refit)} 镜的片段被对齐到新时长（{refit or '无'}）",
            shots=len(approved),
            duration=total,
            refit=refit,
        )
        if target_duration and abs(total - target_duration) > 0.5:
            # 不静默：用户缩短了镜头，成片就真的短了，音乐会在片尾停住。
            # 这件事得说出来，否则 MV 用户会以为是自己音轨坏了。
            self.ctx.notes.append(
                f"时间轴总长 {total:.2f}s 与音轨 {target_duration:.2f}s 不一致，"
                f"成片按画面长度出，音乐在 {total:.2f}s 处收住"
            )
        self.ctx.notes.append(
            f"本次只重合成，没有重新出图：{len(refit)} 个镜头的片段按新时长做了"
            f"裁尾或冻结末帧（{refit or '无'}），其余镜头原样复用"
        )
        self.progress("合成成片", 0.6)
        # 配音从 `work/vo/*_raw.wav` 复用后重新 fit：台词没变，重新合成一遍
        # 纯属浪费（云端 TTS 还要走网络），但镜长变了，拉伸与留白必须跟着变。
        return self._compose_and_report(brief, script, board, approved, reuse_voice=True)

    def _text_layers(
        self, brief: ProjectBrief, board: Storyboard, approved: list[Shot]
    ) -> list[tuple[Path, float, float]]:
        """构建最终合成的文字层：固定标题 + 当前字幕。

        片段是干净的，字幕只在这里出现。因此改字幕、调偏移、清空字幕都只需
        重合成，不需要重新出图；旧片段若已烘死文字，缓存指纹会把它判成过期。
        """
        overlay_scale = 0.5 if self.preview else 1.0
        layers: list[tuple[Path, float, float]] = []
        frame_dir = self.ctx.work_dir / "frames" / "text"
        total = len(approved)
        cursor = 0.0

        # 固定层：镜号、标题、验收标准。字幕留空，由下面的字幕卡单独负责。
        for shot in approved:
            path = render_overlay(
                shot, brief, self.genre,
                frame_dir / f"shot{shot.id:02d}_chrome.png",
                total_shots=total, show_guides=self.guides,
                scale=overlay_scale, subtitle_text="",
            )
            layers.append((path, cursor, cursor + float(shot.duration)))
            cursor += float(shot.duration)

        if self.subtitle_mode != "burn":
            return layers

        # 字幕卡：按实际字幕时间轴逐条叠加，而不是把整镜文字画在一张图上。
        lines: list[tuple[float, float, str]] = []
        if self.subtitle_lines is not None:
            for line in self.subtitle_lines:
                try:
                    lines.append((
                        float(line.get("start") or 0.0),
                        float(line.get("end") or 0.0),
                        str(line.get("text") or "").strip(),
                    ))
                except (AttributeError, TypeError, ValueError):
                    continue
        elif self.music.enabled and not self.music.is_bgm and self.lyric_lines:
            for line in self.lyric_lines:
                lines.append((float(line.start), float(line.end), str(line.text).strip()))
        else:
            # 自动字幕：交付时间轴上的每镜一句（与 compose 的 write_srt 一致）
            cursor = 0.0
            for shot in approved:
                text = (shot.caption or shot.narration or "").strip()
                if text:
                    lines.append((cursor, cursor + float(shot.duration), text))
                cursor += float(shot.duration)

        total_duration = sum(float(s.duration) for s in approved)
        warnings: list[str] = []
        for index, (start, end, text) in enumerate(lines, start=1):
            start = start + self.subtitle_offset
            end = end + self.subtitle_offset
            start = max(0.0, min(start, total_duration))
            end = max(start + 0.05, min(end, total_duration))
            if end <= start or not text:
                continue
            card = render_subtitle_card(
                text, brief,
                frame_dir / f"sub{index:03d}.png",
                scale=overlay_scale,
                style=self.subtitle_style,
                report=warnings,
            )
            layers.append((card, start, end))
        if warnings:
            # 缺字/回退必须让用户看见，不能只在画面里悄悄变化。
            self.log.event("subtitle", "；".join(dict.fromkeys(warnings)))
        return layers

    def _compose_and_report(
        self, brief: ProjectBrief, script: Script, board: Storyboard,
        approved: list[Shot],
        *,
        reuse_voice: bool = False,
    ) -> dict[str, Any]:
        """配音 → 合成 → 落跑测小结。整片拍摄、单镜重拍、时间轴重合成共用这一段。

        分成几段是因为调用方要换的是"谁来当 approved"，而不是合成怎么做。
        合成逻辑复制一份出来的话，几条路的字幕/混音/响度迟早会分叉。

        `reuse_voice`：时间轴重合成时复用上次合成的原始配音（见 `_voiceover`）。
        """
        self.progress("合成成片", 0.88)
        clips = [Path(s.clip_path) for s in approved if s.clip_path]
        if not clips:
            if self.stopped:
                raise RuntimeError(
                    "已手动停止，且停止前没有任何镜头完成，因此没有产出成片"
                )
            raise RuntimeError(
                f"没有任何镜头通过质检（{len(board.shots)} 个镜头全部未达标），拒绝产出成片"
            )
        music_path = Path(self.music.path) if self.music.enabled else None
        if self.music.is_bgm:
            # 配乐模式：音乐给人声垫底。保留配音解说，音乐在混音层被压到 BGM_GAIN，
            # 两者一起过响度归一与淡入淡出——这正是「既能配乐又能配音」。
            audio, audio_source = self._voiceover(board, only=approved, reuse=reuse_voice)
            self.log.event(
                "compose",
                f"配乐模式：人声 + 背景音乐 {music_path.name}（音乐压低垫底）",
            )
        elif self.music.enabled:
            # MV 模式：音乐本人就是音轨。**不生成 TTS 配音**——解说词压在音乐上
            # 听感是坏的，而且「字数 ≈ 时长 × 3.6」这套节奏约束对 MV 没有意义。
            audio, audio_source = None, "silent"
            self.log.event(
                "compose", f"MV 模式：音轨使用音乐 {music_path.name}，本次不生成配音"
            )
        else:
            audio, audio_source = self._voiceover(board, only=approved, reuse=reuse_voice)
        result = compose(board, clips, self.ctx.out_dir, self.ctx.work_dir, audio, audio_source,
                         delivered=approved, music=music_path,
                         # 字幕文件按歌词自己的节奏走（一镜可能两句、一句可能跨两镜）。
                         # 配乐模式不传：那时字幕要与配音念的内容一致。
                         lyrics=self.lyric_lines if (self.music.enabled
                                                     and not self.music.is_bgm) else None,
                         voice_gain=self.voice_gain,
                         music_gain=self.music_gain,
                         voice_duck=self.voice_duck,
                         subtitle_offset=self.subtitle_offset,
                         subtitle_lines=self.subtitle_lines,
                         subtitle_mode=self.subtitle_mode,
                         transition=self.transition,
                         text_anim=self.text_anim,
                         # 片段已不含文字，标题与字幕在拼接后统一叠加。
                         text_layers=self._text_layers(brief, board, approved))
        self.log.event("compose", f"成片产出：{result.video}", **result.to_dict())

        summary = self.log.write_summary(self.ctx, brief, script, board, result.to_dict())
        self.log.event("done", f"跑测小结：{summary}")
        self.progress("完成", 1.0)
        return {
            "run_id": self.ctx.run_id,
            "project_dir": str(self.ctx.project_dir),
            "summary": str(summary),
            "log": str(self.log.path),
            "brief": brief.to_dict(),
            "script": script.to_dict(),
            "storyboard": board.to_dict(),
            "compose": result.to_dict(),
            # 交给渲染后端：引擎真的会用 PROJECT.bpm/offset 驱动角色动作，
            # 以前这里传不出去，bpm 恒为 96.0、offset 恒为 0（死参数）。
            "music": self.music.to_dict(),
            # 歌词行交给渲染后端做逐字卡拉OK（Step 9）。只传 MV 模式的上屏歌词。
            "lyrics": [ln.to_dict() for ln in self.lyric_lines]
            if (self.music.enabled and not self.music.is_bgm) else [],
            # 字幕设置交给渲染后端：代码绘制版必须与占位帧版用同一套模式/偏移/人工轨。
            "subtitle": {
                "mode": self.subtitle_mode,
                "offset": self.subtitle_offset,
                "lines": self.subtitle_lines,
                "style": self.subtitle_style.id,
            },
            "decisions": len(self.traces),
            "notes": self.ctx.notes,
            "stopped": self.stopped,
        }

    # ------------------------------------------------------------ 停止
    def _stop_requested(self, stage: str) -> bool:
        """在镜头边界检查取消信号。

        只在镜头之间检查，不打断单次 FFmpeg/ComfyUI 调用——中途强杀会留下
        半截文件，反而更难清理。已完成的镜头照常进成片。
        """
        if self.stopped:
            return True
        if not self.should_stop():
            return False
        self.stopped = True
        self.log.event("stop", f"收到停止请求，停在「{stage}」，已完成的镜头照常进成片")
        self.ctx.notes.append(
            "本次运行被手动停止：成片只包含停止前已完成的镜头，未跑完的镜头不在片中"
        )
        return True

    # ------------------------------------------------------------ 单镜头闭环
    def _process_shot(self, shot: Shot, brief: ProjectBrief, board: Storyboard) -> None:
        # 2.3 任务分级
        route_state = {
            "shot_desc": f"镜 {shot.id}：{shot.visual}",
            "is_key_shot": shot.id <= 2 or shot.id == len(board.shots),
            "has_subject_ref": bool(shot.subject_refs),
            "attempt_history": [],
        }
        route = self._decide("route_task", self.decider.route_task, f"Shot {shot.id}: {shot.visual}", route_state)
        shot.route = str(route.get("route") or route.get("decision") or "proceed_fast")
        if shot.route == "block":
            shot.status = "skipped"
            self.log.event("shot", f"镜 {shot.id} 被 block，跳过", shot=shot.id, guidance=route.get("guidance"))
            return
        n_candidates = DEEP_REVIEW_CANDIDATES if shot.route == "deep_review" else 1

        # 2.2 模型路由
        candidates = self._model_candidates()
        mr = self._decide(
            "model_route",
            self.decider.model_route,
            task=f"Shot {shot.id}: {shot.visual} | {shot.prompt_video}",
            candidates=candidates,
            priorities=["quality", "cost", "latency"],
            constraints=["8GB VRAM local only", f"remaining budget: {'ok' if self.ctx.budget_ok else 'tight'}"],
            stakes="high" if shot.route == "deep_review" else "medium",
        )
        chosen = str(mr.get("decision") or mr.get("candidate") or "comfy-sdxl-base")
        shot.provider = chosen
        if mr.get("needs_review"):
            self.ctx.notes.append(f"镜 {shot.id} 路由信心不足，标记待人工复核")
        self.log.event("shot", f"镜 {shot.id} 路由：{chosen}", shot=shot.id, route=shot.route, candidates=n_candidates)

        # 生成缓存：昂贵的是出图与语义评审，不是编码。必须在调用 provider 之前
        # 检查，命中后连评审都不再跑，并恢复同一张图对应的完整证据。
        gen_key = ""
        cache_enabled = not self.no_cache and shot.id != self.only_shot
        if cache_enabled:
            gen_shot, locks = self._locked_prompt(shot, board)
            gen_key = self._gen_signature(
                shot, brief, board, n_candidates, gen_shot, locks,
                _seed(brief.theme, shot.id, 1),
            )
            cached = gcache.load(self.ctx.work_dir / "gencache", shot.id, gen_key)
            if cached is not None and self._restore_generation(shot, cached):
                self.log.event(
                    "generate",
                    f"镜 {shot.id} 命中生成缓存：跳过出图与评审，恢复选中证据",
                    shot=shot.id,
                    cached=True,
                    evidence_id=shot.evidence_id,
                    provider=shot.frame_source,
                )
                clip = self._make_clip(shot, brief, board)
                if clip is None:
                    shot.status = "rejected"
                return

        # 生成 → 质检 → 重试
        #
        # 循环语义（与提示词库 2.4/2.6 对齐）：
        #   每一轮 = 生成 1 个候选 → 按验收标准质检；
        #   通过记分并累计候选，攒够 n_candidates 或达到 MAX_ATTEMPTS 才停；
        #   未通过立即走 2.6 重试策略（含强制升级），不等攒候选。
        history: list[dict[str, Any]] = []
        candidates_img: list[dict[str, Any]] = []
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if len(candidates_img) >= n_candidates and n_candidates > 1:
                break
            if self._stop_requested(f"镜 {shot.id} 第 {attempt} 次尝试"):
                return
            shot.attempts = attempt
            seed = _seed(brief.theme, shot.id, attempt)
            generated = self._generate_still(shot, brief, board, attempt, seed)
            if generated is None:
                continue
            still, still_provider = generated
            desc = self._describe(still, shot, brief, attempt)
            review = self._decide(
                "review_quality",
                self.decider.review_quality,
                state={
                    "shot": f"{shot.visual} | {shot.prompt_image}",
                    "acceptance": shot.acceptance,
                    "candidate_desc": desc,
                    "attempt": attempt,
                },
            )
            score, verdict = judge(review)
            defect = str(review.get("defect_type") or "none") if score else "unknown"

            # 把"分数是怎么来的"一起落盘：模拟评审与真实视觉评审必须可区分。
            try:
                desc_info = json.loads(desc)
            except (TypeError, ValueError):
                desc_info = {}

            # 技术检查是硬闸门，**不依赖决策层**：接入 Jev/远程模型后，
            # 它们没有理由知道"图片其实读不出来"，不能靠它们自觉拒判。
            technical = desc_info.get("technical") if isinstance(desc_info, dict) else None
            if isinstance(technical, dict) and technical.get("ok") is False:
                score, verdict, defect = 1, "reject", "image_unreadable"
                self.ctx.notes.append(
                    f"镜 {shot.id} 技术检查失败（{technical.get('reason') or '图片不可用'}），本镜不放行"
                )

            shot.review_mode = str(
                review.get("review_mode") or desc_info.get("review_mode") or "simulated"
            )
            shot.visual_check = str(
                review.get("visual_check") or desc_info.get("visual_check") or "not_performed"
            )
            shot.review_rule = str(review.get("rule_version") or REVIEW_RULE_VERSION)
            shot.image_sha256 = str(
                desc_info.get("image_sha256")
                or (desc_info.get("technical") or {}).get("sha256")
                or ""
            )
            # 技术证据原样留档：质检面板要给出"看了多大、多亮、对比度多少"，
            # 只留一个分数无法证明画面真的被检查过。
            if isinstance(technical, dict):
                shot.tech_ok = bool(technical.get("ok"))
                shot.tech_reason = str(technical.get("reason") or "")
                shot.image_width = int(technical.get("width") or 0)
                shot.image_height = int(technical.get("height") or 0)
                shot.image_mean = round(float(technical.get("mean") or 0.0), 2)
                shot.image_contrast = round(float(technical.get("contrast") or 0.0), 2)

            if score <= 0:
                self.ctx.notes.append(f"镜 {shot.id} 质检响应无法解析（{review}），按需复核处理，不放行")
            self.log.event(
                "review",
                f"镜 {shot.id} 第 {attempt} 次质检：{score}/4 {verdict}"
                f"（{_review_label(shot.review_mode, shot.visual_check)}）",
                shot=shot.id,
                attempt=attempt,
                quality=score,
                verdict=verdict,
                defect=defect,
                review_mode=shot.review_mode,
                visual_check=shot.visual_check,
                decided_by="pipeline(judge)",
            )
            if verdict == "accept":
                shot.quality = score
                shot.verdict = verdict
                shot.defect_type = defect
                shot.status = "approved"
                shot.frame_path = str(still)
                candidates_img.append(
                    {
                        "index": str(len(candidates_img) + 1),
                        "desc": desc,
                        "path": str(still),
                        "quality": score,
                        "evidence": self._candidate_evidence(
                            shot, still, desc, review, score, verdict, defect,
                            still_provider, attempt,
                        ),
                    }
                )
                if n_candidates <= 1:
                    break
                continue

            # 未通过 → 记录本镜状态，再走重试策略
            shot.quality = score
            shot.verdict = verdict
            shot.defect_type = defect
            shot.status = "reviewing"
            self.log.event("retry", f"镜 {shot.id} 第 {attempt} 次未通过，进入重试策略", shot=shot.id)
            retry_state = {
                "failure_reason": _failure_reason(desc),
                "defect_type": defect,
                "attempt": attempt,
                "max_attempts": MAX_ATTEMPTS,
                "prompt_used": shot.prompt_image,
                "remaining_budget": "ok" if self.ctx.budget_ok else "tight",
                "attempt_history": history,
            }
            decision = self._decide("retry_strategy", self.decider.retry_strategy, retry_state)
            decision = apply_escalation(decision, retry_state)
            action = str(decision.get("action") or "retry_same")
            history.append({"attempt": attempt, "defect_type": defect, "action": action})
            self.log.event(
                "retry",
                f"镜 {shot.id} 重试动作：{action}（focus={decision.get('focus')}）",
                shot=shot.id,
                action=action,
                escalated=decision.get("escalated", False),
                guidance=decision.get("guidance"),
            )
            if action == "skip":
                shot.status = "skipped"
                self.ctx.notes.append(f"镜 {shot.id} 连续同缺陷，跳过并进入人工复核队列")
                candidates_img.append(
                    {
                        "index": str(len(candidates_img) + 1),
                        "desc": desc,
                        "path": str(still),
                        "quality": score,
                        "evidence": self._candidate_evidence(
                            shot, still, desc, review, score, verdict, defect,
                            still_provider, attempt,
                        ),
                    }
                )
                break
            if action == "switch_model":
                shot.provider = self._switch_provider(shot.provider)
            if action == "rewrite_prompt":
                shot.prompt_image = _rewrite(shot.prompt_image, str(decision.get("focus") or "subject"))
            if attempt == MAX_ATTEMPTS:
                shot.status = "rejected"
                self.ctx.notes.append(f"镜 {shot.id} 达最大尝试次数仍未通过，转人工复核")
                candidates_img.append(
                    {
                        "index": str(len(candidates_img) + 1),
                        "desc": desc,
                        "path": str(still),
                        "quality": score,
                        "evidence": self._candidate_evidence(
                            shot, still, desc, review, score, verdict, defect,
                            still_provider, attempt,
                        ),
                    }
                )

        if not candidates_img:
            shot.status = shot.status if shot.status != "pending" else "rejected"
            return

        # 2.5 N 选 1（多候选时）
        best = candidates_img[0]
        if len(candidates_img) > 1:
            pick = self._decide(
                "pick_best",
                self.decider.pick_best,
                state={
                    "shot": shot.visual,
                    "acceptance": shot.acceptance,
                    "candidates": [{"index": c["index"], "desc": c["desc"]} for c in candidates_img],
                },
            )
            best_idx = str(pick.get("best") or "1")
            best = next((c for c in candidates_img if c["index"] == best_idx), candidates_img[0])
            self.log.event(
                "shot",
                f"镜 {shot.id} N 选 1 → 候选 {best_idx}（margin={pick.get('margin')}）",
                shot=shot.id,
                margin=pick.get("margin"),
            )
        shot.candidate_evidence = [c["evidence"] for c in candidates_img if c.get("evidence")]
        self._apply_evidence(shot, best.get("evidence") or {})
        shot.frame_path = best["path"]
        shot.quality = int(best.get("quality") or shot.quality)
        if shot.status not in ("approved", "skipped"):
            shot.status = "approved" if shot.quality >= 3 else "rejected"
            if shot.status == "rejected":
                self.ctx.notes.append(f"镜 {shot.id} 最高分 {shot.quality}/4 未达阈值，标记为不合格并转人工复核")

        if cache_enabled and gen_key:
            gcache.store(
                self.ctx.work_dir / "gencache", shot.id, gen_key,
                Path(shot.frame_path),
                provider=shot.frame_source,
                quality=shot.quality,
                status=shot.status,
                evidence=best.get("evidence") or {},
                candidates=shot.candidate_evidence,
            )

        # 出片
        clip = self._make_clip(shot, brief, board)
        if clip is None:
            shot.status = "rejected"

    # ------------------------------------------------------------ 生成
    def _locked_prompt(self, shot: Shot, board: Storyboard) -> tuple[Shot, list[str]]:
        """把被引用主体的视觉锁定拼进**本次出图**的提示词。

        参考图目前不被任何 provider 消费（能力声明里写明），能做的跨镜一致性
        只有文字锁定。刻意不改 `shot.prompt_image` 原稿：它是用户/模型写的，
        改动它会让"我改过什么"失去可信度，也会在重试时越拼越长。
        """
        if not shot.subject_refs:
            return shot, []
        by_id = {s.id: s for s in board.subjects}
        locks: list[str] = []
        for sid in shot.subject_refs:
            subject = by_id.get(str(sid))
            text = (subject.visual_lock if subject else "").strip()
            if text and text not in locks:
                locks.append(text)
        if not locks:
            return shot, []
        base = shot.prompt_image.strip()
        prompt = (base + ("；" if base else "") + "角色一致性：" + "；".join(locks))
        return replace(shot, prompt_image=prompt), locks

    def _generate_still(
        self, shot: Shot, brief: ProjectBrief, board: Storyboard, attempt: int, seed: int
    ) -> tuple[Path, str] | None:
        frame_path = self.ctx.work_dir / "frames" / f"shot{shot.id:02d}_a{attempt}.png"
        intent_path = self.ctx.work_dir / "frames" / f"shot{shot.id:02d}_intent.png"
        # 分镜示意帧（人工对照用，永远产出）。用原稿而不是加锁版本：
        # 它代表"人写的分镜"，出图实际用的提示词另记在 generate 事件里。
        render_still(shot, brief, self.genre, intent_path, total_shots=len(board.shots), show_guides=self.guides, scale=self.fixture_scale)
        shot.intent_path = str(intent_path)
        gen_shot, locks = self._locked_prompt(shot, board)
        for provider in self.image_chain:
            ok, why = provider.available()
            if not ok:
                continue
            try:
                t0 = time.perf_counter()
                if provider.name == "offline":
                    # 占位帧：用镜头自身信息合成，保证与本镜一一对应
                    out = render_still(gen_shot, brief, self.genre, frame_path, total_shots=len(board.shots), show_guides=self.guides, scale=self.fixture_scale)
                else:
                    out = provider.generate(gen_shot, brief, frame_path, seed)
                self.log.event(
                    "generate",
                    f"镜 {shot.id} 第 {attempt} 次出图完成（{provider.name}）",
                    shot=shot.id,
                    provider=provider.name,
                    attempt=attempt,
                    ms=round((time.perf_counter() - t0) * 1000, 1),
                    file=str(out),
                    subject_locks=locks or None,
                )
                return out, provider.name
            except Exception as exc:  # noqa: BLE001
                self.log.error(f"generate:shot{shot.id}", f"{provider.name} 出图失败：{exc}")
                self.ctx.notes.append(f"镜 {shot.id} 的 {provider.name} 出图失败，已降级下一 provider")
                continue
        self.log.event("generate", f"镜 {shot.id} 所有出图 provider 都失败", shot=shot.id)
        return None

    def _vision_client(self) -> Any:
        """视觉评审客户端。VLM_BASE_URL/KEY 留空时回落到 LLM 的配置。"""
        model = str(self.settings.get("VLM_MODEL") or "").strip()
        if not model:
            return None
        if self._vlm_client is None:
            base = str(self.settings.get("VLM_BASE_URL") or self.settings.get("LLM_BASE_URL") or "").strip()
            key = str(self.settings.get("VLM_API_KEY") or self.settings.get("LLM_API_KEY") or "").strip()
            if not base or not key:
                return None
            self._vlm_client = OpenAIChatClient(base_url=base, api_key=key, model=model)
        return self._vlm_client

    def _describe(self, image: Path, shot: Shot, brief: ProjectBrief, attempt: int) -> str:
        """质检前置：读图、技术检查、再尝试视觉描述（提示词库 2.1）。

        三种模式必须能区分开：

        - `vlm`：真把图片发给多模态模型，评审结论基于画面；
        - `simulated`：图片可读，但评审文字由镜头规格 + 确定性规则生成；
        - `technical_failed`：图片不存在/无法解码，直接判技术检查失败。

        以前无论哪种情况返回的都是同一段"看起来像评审"的文字，
        两张根本不存在的图片也能得到相同描述——分数因此不能当作画面依据。
        """
        technical = inspect_image(image)
        payload: dict[str, Any] | None = None
        mode = "simulated"
        visual = "not_performed"

        vlm = self.settings.get("VLM_MODEL")
        client = self._vision_client()
        if self.ctx.live and vlm and client is not None:
            sys_p = self.lib.system("2.1")
            user_p = self.lib.render_user(
                "2.1", {"style": brief.style, "acceptance": shot.acceptance}, strict=False
            )
            try:
                vision = getattr(client, "chat_vision", None)
                if vision is None:
                    raise RuntimeError("当前模型客户端不支持图像输入")
                from .common import extract_json

                parsed = extract_json(vision(sys_p, user_p, image, temperature=0.1))
                if not isinstance(parsed, dict):
                    raise RuntimeError("视觉评审返回的不是 JSON 对象")
                payload = parsed
                mode = "vlm"
                visual = "performed" if technical.get("ok") else "failed"
            except Exception as exc:  # noqa: BLE001
                self.log.error("vlm", f"视觉评审失败，改用模拟评审（结果会明确标注）：{exc}")
                payload = None

        if payload is None:
            payload = json.loads(_offline_description(shot, attempt))
            mode = "simulated" if technical.get("ok") else "technical_failed"
            visual = "not_performed" if technical.get("ok") else "failed"

        # 技术检查用的是真实图片数据，无论走哪条评审路径都覆盖进去。
        merged = dict(payload.get("technical") or {})
        merged.update(technical)
        payload["technical"] = merged
        if not technical.get("ok"):
            # 图都读不出来时，任何"评审结论"都不成立。
            mode, visual = "technical_failed", "failed"
        payload["review_mode"] = mode
        payload["visual_check"] = visual
        payload["review_rule"] = REVIEW_RULE_VERSION
        payload["image_sha256"] = technical.get("sha256") or ""

        if mode == "simulated" and not any("模拟评审" in note for note in self.ctx.notes):
            self.ctx.notes.append(
                "质检为模拟评审：规则引擎依据镜头规格与真实技术检查（尺寸/亮度/对比度）"
                "打分，未做语义级画面评审；分数不代表视觉质量已通过"
            )
        return json.dumps(payload, ensure_ascii=False)

    def _candidate_evidence(
        self,
        shot: Shot,
        image: Path,
        desc: str,
        review: dict,
        score: int,
        verdict: str,
        defect: str,
        provider: str,
        attempt: int,
    ) -> dict[str, Any]:
        """一个候选的完整证据。选定后整包绑定，不能用最后一次循环状态代替。"""
        try:
            info = json.loads(desc)
        except (TypeError, ValueError):
            info = {}
        technical = info.get("technical") if isinstance(info, dict) else None
        sha = str(
            (info.get("image_sha256") if isinstance(info, dict) else "")
            or (technical or {}).get("sha256")
            or ""
        )
        mode = str(
            (review.get("review_mode") if isinstance(review, dict) else "")
            or info.get("review_mode")
            or "simulated"
        )
        visual = str(
            (review.get("visual_check") if isinstance(review, dict) else "")
            or info.get("visual_check")
            or "not_performed"
        )
        evidence_id = hashlib.sha256(
            f"{shot.id}:{attempt}:{sha}:{provider}:{score}:{mode}".encode("utf-8")
        ).hexdigest()[:16]
        return {
            "evidence_id": evidence_id,
            "attempt": attempt,
            "asset_id": sha[:16] if sha else "",
            "path": str(image),
            "sha256": sha,
            "provider": str(provider or ""),
            "quality": int(score),
            "verdict": str(verdict or ""),
            "defect": str(defect or ""),
            "review_mode": mode,
            "visual_check": visual,
            "review_rule": str(info.get("review_rule") or REVIEW_RULE_VERSION),
            "technical": technical if isinstance(technical, dict) else None,
            "detail": info,
        }

    def _apply_evidence(self, shot: Shot, evidence: dict[str, Any]) -> None:
        """把选中候选的整包证据写回镜头，保证图、哈希、技术检查来自同一张图。"""
        if not evidence:
            return
        shot.evidence_id = str(evidence.get("evidence_id") or "")
        shot.frame_source = str(evidence.get("provider") or "")
        shot.frame_is_clean = shot.frame_source not in ("", "offline")
        sha = str(evidence.get("sha256") or "")
        shot.image_sha256 = sha
        shot.review_mode = str(evidence.get("review_mode") or "")
        shot.visual_check = str(evidence.get("visual_check") or "")
        shot.review_rule = str(evidence.get("review_rule") or "")
        technical = evidence.get("technical") if isinstance(evidence.get("technical"), dict) else {}
        if technical:
            shot.tech_ok = bool(technical.get("ok"))
            shot.tech_reason = str(technical.get("reason") or "")
            shot.image_width = int(technical.get("width") or 0)
            shot.image_height = int(technical.get("height") or 0)
            shot.image_mean = round(float(technical.get("mean") or 0.0), 2)
            shot.image_contrast = round(float(technical.get("contrast") or 0.0), 2)

    def _provider_identity(self) -> list[str]:
        """出图 provider 的规范化身份，供生成缓存使用。绝不包含 API Key。"""
        out: list[str] = []
        for provider in self.image_chain:
            name = str(getattr(provider, "name", "") or "")
            if name == "comfy":
                out.append(
                    f"comfy:{getattr(provider, 'base_url', '')}:"
                    f"{getattr(provider, 'template', '')}:{getattr(provider, 'max_edge', '')}"
                )
            elif name == "cloud":
                out.append(
                    f"cloud:{getattr(provider, 'base_url', '')}:{getattr(provider, 'model', '')}"
                )
            else:
                out.append(f"{name}:{getattr(provider, 'provider_id', '')}")
        return out

    def _workflow_fingerprint(self) -> str:
        """当前工作流目录的内容摘要。只改文件名不算，内容变了才算。"""
        if self._workflow_hash is not None:
            return self._workflow_hash
        digest = hashlib.sha256()
        root = resource_dir("workflows")
        try:
            files = sorted(p for p in root.rglob("*") if p.is_file())
        except OSError:
            files = []
        for path in files:
            digest.update(path.name.encode("utf-8"))
            try:
                digest.update(path.read_bytes())
            except OSError:
                continue
        self._workflow_hash = digest.hexdigest()[:20]
        return self._workflow_hash

    def _reference_hashes(self, shot: Shot, board: Storyboard) -> dict[str, str]:
        """被当前 provider 实际消费的参考图摘要。

        当前所有 provider 的 `reference_capability()` 都是 False，因此返回空；
        将来接入参考条件生成时，这里会自动把真正参与出图的素材纳入缓存依赖。
        """
        consumes = False
        for provider in self.image_chain:
            try:
                ok, _why = provider.reference_capability()
            except Exception:  # noqa: BLE001
                ok = False
            consumes = consumes or bool(ok)
        if not consumes or not shot.subject_refs:
            return {}
        by_id = {str(s.id): s for s in board.subjects}
        out: dict[str, str] = {}
        for sid in shot.subject_refs:
            subject = by_id.get(str(sid))
            asset = str(((subject.reference or {}) if subject else {}).get("asset") or "")
            if not asset:
                continue
            rel = Path(asset)
            candidates = [
                rel if rel.is_absolute() else self.ctx.project_dir / "plan" / rel,
                self.ctx.project_dir / rel,
            ]
            for path in candidates:
                try:
                    if path.is_file():
                        out[str(sid)] = hashlib.sha256(path.read_bytes()).hexdigest()[:20]
                        break
                except OSError:
                    continue
        return out

    def _frame_inputs(self, shot: Shot, board: Storyboard) -> dict[str, Any]:
        """离线占位帧真正吃进去的绘制输入。

        真实出图（ComfyUI/云端）只吃提示词，改文案不该让缓存失效，所以文案
        只在 provider 链里含 offline 时纳入。
        `start`/`end` 故意不纳入：改时间轴只该重合成，不该重出图。代价是离线
        占位帧右下角的时间码可能停在旧值——正式出图不受影响，属已知边界。
        """
        inputs: dict[str, Any] = {
            "guides": bool(self.guides),
            "scale": round(float(self.fixture_scale), 4),
            "total_shots": int(len(board.shots)),
        }
        if any(str(getattr(p, "name", "")) == "offline" for p in self.image_chain):
            inputs["text"] = {
                "visual": str(shot.visual or ""),
                "act": str(shot.act or ""),
                "camera": str(shot.camera or ""),
                "acceptance": str(shot.acceptance or ""),
                "caption": caption_of(shot),
            }
        return inputs

    def _gen_signature(
        self, shot: Shot, brief: ProjectBrief, board: Storyboard,
        candidates: int, gen_shot: Shot, locks: list[str], seed: int,
    ) -> str:
        return gcache.signature(
            shot_id=shot.id,
            prompt=gen_shot.prompt_image,
            visual_locks=locks,
            brief_key=json.dumps(brief.to_dict(), ensure_ascii=False, sort_keys=True),
            genre=self.genre.key,
            palette=self.palette_id,
            preview=self.preview,
            size=generation_size(brief.aspect_ratio),
            seed=seed,
            candidates=candidates,
            provider_identity=self._provider_identity(),
            workflow_fingerprint=self._workflow_fingerprint(),
            reference_hashes=self._reference_hashes(shot, board),
            frame_inputs=self._frame_inputs(shot, board),
        )

    def _restore_generation(self, shot: Shot, record: dict[str, Any]) -> bool:
        """把生成缓存恢复成“选中候选 + 完整证据”。"""
        image = Path(str(record.get("image") or ""))
        if not image.is_file():
            return False
        evidence = record.get("evidence") if isinstance(record.get("evidence"), dict) else {}
        if not evidence:
            return False
        shot.frame_path = str(image)
        shot.quality = int(record.get("quality") or 0)
        shot.status = str(record.get("status") or "approved")
        shot.verdict = str(evidence.get("verdict") or shot.status)
        shot.defect_type = str(evidence.get("defect") or "none")
        shot.attempts = int(evidence.get("attempt") or 1)
        shot.provider = str(record.get("provider") or shot.provider)
        shot.candidate_evidence = list(record.get("candidates") or [])
        self._apply_evidence(shot, evidence)
        # 缓存图本身就是选中图；保证来源 provider 与干净属性一起恢复。
        if not shot.frame_source:
            shot.frame_source = str(record.get("provider") or "")
            shot.frame_is_clean = shot.frame_source not in ("", "offline")
        return True

    def _clip_key(self, shot: Shot, brief: ProjectBrief, board: Storyboard) -> str:
        """这一镜片段的内容指纹。见 pipeline/clipcache.py 的字段说明。"""
        gen_shot, locks = self._locked_prompt(shot, board)
        return build_clip_key(
            shot, brief, board,
            genre=self.genre.key,
            palette=self.palette_id,
            preview=self.preview,
            work_scale=self.work_scale,
            still_scale=self.still_scale,
            guides=self.guides,
            providers=[
                f"{getattr(p, 'name', '')}:{getattr(p, 'provider_id', '')}"
                for p in self.video_chain
            ],
            video_size=self._video_size,
            image_sha256=shot.image_sha256,
            effective_prompt=gen_shot.prompt_image,
            workflow_fingerprint=self._workflow_fingerprint(),
            reference_hashes=self._reference_hashes(shot, board),
        )

    def _make_clip(self, shot: Shot, brief: ProjectBrief, board: Storyboard) -> Path | None:
        clip_path = self.ctx.work_dir / "clips" / f"shot{shot.id:02d}.mp4"
        key = self._clip_key(shot, brief, board)
        # 输入没变就复用旧片段。单镜重拍的目标镜永远重画——用户点它的目的
        # 就是"换一张画面"，即使规格没变也不该被缓存挡住。
        if not self.no_cache and shot.id != self.only_shot:
            if clip_state(clip_path, key) == "current":
                self.log.event(
                    "clip",
                    f"镜 {shot.id} 复用缓存片段（画面输入与渲染规格未变）",
                    shot=shot.id,
                    cached=True,
                )
                return clip_path
        # 真实出图（ComfyUI/云端）产出的就是干净背景，本地运动视频必须用它；
        # 以前这里无条件重画占位背景，导致“评审过的图”和“进入视频的图”不是
        # 同一张。离线占位帧自带文字，才需要重画无字背景。
        selected = Path(shot.frame_path) if shot.frame_path else None
        if shot.frame_is_clean and selected is not None and selected.is_file():
            clean = selected
            self.log.event(
                "clip", f"镜 {shot.id} 使用已选生成图作为运动背景",
                shot=shot.id, source=shot.frame_source, file=str(selected),
            )
        else:
            clean = render_still(
                shot, brief, self.genre,
                self.ctx.work_dir / "frames" / f"shot{shot.id:02d}_clean.png",
                total_shots=len(board.shots), show_guides=False, with_text=False,
                scale=self.still_scale,
            )
        # 文字层不再烘进片段：片段只保留干净背景，字幕/标题在最终合成时叠加。
        # 这样改字幕、调偏移、清空字幕都只需重合成，不会出现“SRT 改了、画面还是旧字”。
        for provider in self.video_chain:
            ok, why = provider.available()
            if not ok:
                continue
            # 付费 provider 先过 2.8 护栏
            if provider.name == "cloud":
                cost = CLOUD_COST_CNY * shot.duration
                guard = self._decide(
                    "tool_guard",
                    self.decider.tool_guard,
                    tool="call_paid_video_api",
                    action=f"Generate {shot.duration:.0f}s video for shot {shot.id}, estimated cost {cost:.2f} CNY",
                    arguments_summary=[f"shot_id={shot.id}", f"provider={provider.provider_id or 'cloud'}", f"duration={shot.duration}s"],
                    side_effects=["Consumes paid API quota", "Adds about 60s to pipeline runtime"],
                    safeguards=[
                        f"Frame passed Jev quality review at {shot.quality}/4",
                        "Budget check passed" if self.ctx.budget_ok else "Budget exceeded",
                        f"stakes={'high' if shot.route == 'deep_review' else 'medium'}",
                    ],
                    policy=["Shots with stakes=high require a confirm decision before spending"],
                    reversibility="not_reversible",
                )
                verdict = str(guard.get("decision") or guard.get("status") or "deny")
                if verdict != "allow":
                    self.log.event(
                        "guard",
                        f"镜 {shot.id} 付费调用被 {verdict}，降级本地生成",
                        shot=shot.id,
                        verdict=verdict,
                        guidance=guard.get("guidance"),
                    )
                    continue
                self.ctx.cost_used_cny += cost
            try:
                t0 = time.perf_counter()
                if provider.name == "kenburns":
                    # 出片前自查输入：ffmpeg 报「No such file」时很难看出是渲染没落盘
                    # 还是路径/权限问题，这里先明确报缺哪个文件（实测踩过）。
                    missing = [str(clean)] if not clean.exists() else []
                    if missing:
                        raise FileNotFoundError(
                            "出片输入缺失：" + "、".join(missing)
                            + "（若文件确实存在，说明 ffmpeg 无权读取该路径，"
                            + "常见于受限终端/沙箱/杀软拦截；请在普通命令行或 PowerShell 中重试）"
                        )
                    provider.make_clip(shot, clean, clip_path, shot.duration, work=self.work_scale)
                else:
                    provider.make_clip(shot, Path(shot.frame_path), clip_path, shot.duration)
                shot.clip_path = str(clip_path)
                write_clip_key(clip_path, key, provider=provider.name)
                if provider.name == "cloud":
                    # 只在调用成功后记一条：失败/降级不应出现在费用台账里。
                    # 台账落在项目根（不是 preview 命名空间），费用看板才能汇总。
                    record_cost(
                        self.ctx.project_dir,
                        kind="video",
                        provider=str(getattr(provider, "provider_id", "") or "cloud-video"),
                        amount_cny=cost,
                        units=shot.duration,
                        unit="s",
                        rate_cny=CLOUD_COST_CNY,
                        note=f"镜 {shot.id} 云端图生视频（按 {CLOUD_COST_CNY} 元/秒估价）",
                    )
                self.log.event(
                    "clip",
                    f"镜 {shot.id} 片段完成（{provider.name}，{shot.duration:.1f}s）",
                    shot=shot.id,
                    provider=provider.name,
                    ms=round((time.perf_counter() - t0) * 1000, 1),
                )
                return clip_path
            except Exception as exc:  # noqa: BLE001
                self.log.error(f"clip:shot{shot.id}", f"{provider.name} 出片失败：{exc}")
                continue
        return None

    # ------------------------------------------------------------ 终检与配音
    def _final_check(self, board: Storyboard) -> list[Shot]:
        """交付前终检（2.7）。返回**达标镜头清单**，未达标的一律不进成片。

        旧实现在这里只写一行 note 就继续合成，被拒镜头照样剪进 final.mp4，
        与模块铁律「未过终检不许交付」以及代码里那句"已禁止带伤交付"自相矛盾。
        现在由返回值决定剪什么，闸门是真的。
        """
        approved = [s for s in board.shots if s.status == "approved" and s.clip_path]
        dropped = [s.id for s in board.shots if s not in approved]
        evidence = (
            f"{len(approved)}/{len(board.shots)} shots delivered with approved frames and clips; "
            + ("no open defects" if not dropped else f"dropped shots: {dropped}")
        )
        objective = (
            f"Deliver all {len(board.shots)} shots of '{board.project.get('title')}' "
            f"({board.project.get('total_duration')}s) with approved frames and clips"
        )
        res = self._decide("completion", self.decider.completion, objective=objective, evidence=evidence)
        status = str(res.get("status") or ("complete" if res.get("complete") else "incomplete"))
        self.log.event(
            "check",
            f"完成度终检：{status}",
            evidence=evidence,
            guidance=res.get("guidance"),
            approved=len(approved),
            dropped=dropped,
        )
        return approved

    def _voiceover(self, board: Storyboard, only: list[Shot] | None = None,
                   *, reuse: bool = False) -> tuple[Path | None, str]:
        """配音。**只为真正进成片的镜头配音**——否则被剔除镜头的配音会留在音轨里，
        导致此后所有镜头音画整体前移（历史 bug）。

        ``reuse``：时间轴重合成时用。台词一个字都没变，重新合成一遍纯属浪费
        （云端 TTS 还要走网络、还可能失败）；直接从 `work/vo/shotNN_raw.wav` 取
        上次的原始配音，只**重新 fit** 到新镜长——镜长变了，拉伸与留白也得跟着变，
        不重 fit 的话音画从这一镜起就错位。原始配音不在时才退回重新合成。
        """
        targets = only if only is not None else list(board.shots)
        provider = next((p for p in self.tts_chain if p.available()[0]), None)
        vo_dir = self.ctx.work_dir / "vo"
        # 复用模式下即使没有可用的 TTS provider 也要继续：原始配音已经在磁盘上，
        # 这一步根本不联网。否则一旦 TTS 端点挂了，改个时长就再也合不成片。
        can_reuse = reuse and vo_dir.is_dir()
        if (provider is None or provider.name == "silent") and not can_reuse:
            return None, "silent"
        parts: list[Path] = []
        failures = 0
        used = provider.name if provider is not None else "reuse"
        for shot in targets:
            if not (shot.narration or "").strip():
                continue
            raw = vo_dir / f"shot{shot.id:02d}_raw.wav"
            fitted = vo_dir / f"shot{shot.id:02d}.wav"
            try:
                reused = reuse and raw.is_file()
                if not reused:
                    if provider is None or provider.name == "silent":
                        # 没有 provider 又没原始配音：这一镜只能空着，交给下面的
                        # 静音兜底，别把整片判失败。
                        raise RuntimeError("没有可用的 TTS provider，且无原始配音可复用")
                    provider.speak(_speakable(shot.narration), raw)
                _, dur, note = fit_audio(raw, fitted, shot.duration)
                parts.append(fitted)
                if reused:
                    note = f"复用上次配音，重新对齐到 {shot.duration:.2f}s"
                # 配音超过镜头时长且已到限速上限 → 结尾会与画面错位，必须显式上报
                if dur > shot.duration + 0.05:
                    self.ctx.notes.append(
                        f"镜 {shot.id} 配音 {dur:.2f}s 超过镜头 {shot.duration:.2f}s（已到 {MAX_TEMPO}× 限速），"
                        f"该镜结尾会与画面错位，建议缩短解说词或延长镜长"
                    )
                self.log.event(
                    "tts",
                    f"镜 {shot.id} 配音 {dur:.2f}s（{note}）",
                    shot=shot.id,
                    provider=used,
                    duration=round(dur, 2),
                    note=note,
                    overrun=round(max(0.0, dur - shot.duration), 2),
                )
            except Exception as exc:  # noqa: BLE001
                failures += 1
                self.log.error(f"tts:shot{shot.id}", f"配音失败：{exc}")
                gap = self.ctx.work_dir / "vo" / f"shot{shot.id:02d}_gap.wav"
                try:
                    import subprocess

                    from .video import FFMPEG

                    subprocess.run(
                        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                         "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                         "-t", f"{shot.duration:.3f}", str(gap)],
                        check=True, timeout=120,
                    )
                    parts.append(gap)
                except Exception:  # noqa: BLE001
                    pass
        if not parts:
            return None, "silent"
        out = self.ctx.work_dir / "vo" / "voiceover.m4a"
        concat_audio(parts, out)
        # 如实上报：有镜头配音失败时必须区分"部分静音/全静音"，不能一律报"配音：有"
        if failures == 0:
            return out, used
        if failures >= len(parts):
            self.ctx.notes.append(f"全部 {failures} 个镜头配音合成失败，成片只有静音轨（配音来源：{used}，实际全静音）")
            return out, "tts_failed"
        self.ctx.notes.append(f"{failures} 个镜头配音合成失败，这些镜头为静音（配音来源：{used}）")
        return out, f"{used}+{failures}静音"

    # ------------------------------------------------------------ 辅助
    def _attach_genre(self, board: Storyboard) -> None:
        for shot in board.shots:
            shot._genre = self.genre  # type: ignore[attr-defined]
            shot._total = len(board.shots)  # type: ignore[attr-defined]

    def _model_candidates(self) -> list[dict[str, str]]:
        """可用候选清单：来自实际配置 + 本地能力，而不是写死的列表。"""
        cands: list[dict[str, str]] = []
        for prov in self.image_chain:
            ok, why = prov.available()
            if not ok:
                continue
            if prov.name == "comfy":
                # 描述写实际探测到的信息，而不是硬编码 "Local SDXL"
                cands.append(
                    {
                        "id": "comfy-local",
                        "description": f"Local ComfyUI via HTTP, real text-to-image generation, cost near zero ({why})",
                    }
                )
            elif prov.name == "offline":
                cands.append({"id": "local-placeholder", "description": "Local placeholder frame compositor, ~0.3s per image, free, layout-accurate but not photoreal"})
        for prov in self.video_chain:
            if prov.name == "cloud":
                cands.append({"id": "cloud-video-api", "description": "Cloud image-to-video API, billed per clip, high quality"})
        return cands or [{"id": "local-placeholder", "description": "Local placeholder frame compositor, free"}]

    def _switch_provider(self, current: str) -> str:
        ids = [c["id"] for c in self._model_candidates()]
        if not ids:
            return current
        try:
            idx = ids.index(current)
        except ValueError:
            return ids[0]
        return ids[(idx + 1) % len(ids)]


# ------------------------------------------------------------------ 纯函数
def _seed(theme: str, shot_id: int, attempt: int) -> int:
    return stable_hash(f"{theme}|{shot_id}|{attempt}") % (2**31 - 1)


def _failure_reason(desc: str) -> str:
    try:
        data = json.loads(desc)
        defects = data.get("defects") or []
        return "; ".join(str(d) for d in defects) or "unspecified"
    except Exception:  # noqa: BLE001
        return desc[:120]


def _short_args(args: tuple, kwargs: dict) -> Any:
    """决策日志里只留关键参数，避免把整段提示词塞进日志。"""

    def brief(value: Any, depth: int = 0) -> Any:
        if depth > 2:
            return "…"
        if isinstance(value, dict):
            return {k: brief(v, depth + 1) for k, v in list(value.items())[:12]}
        if isinstance(value, (list, tuple)):
            return [brief(v, depth + 1) for v in value[:4]] + (["…"] if len(value) > 4 else [])
        if isinstance(value, str):
            return value if len(value) <= 160 else value[:159] + "…"
        return value

    if not args and not kwargs:
        return {}
    payload: dict[str, Any] = {}
    if args:
        payload["args"] = brief(list(args))
    if kwargs:
        payload["kwargs"] = brief(kwargs)
    return payload


def _rewrite(prompt: str, focus: str) -> str:
    """按 focus 改写提示词：加约束词而不是重写整句，保持可追溯。"""
    add = {
        "subject": "subject intact, symmetrical composition",
        "lighting": "even soft lighting, warm tone, no harsh highlights",
        "composition": "centered composition, correct perspective",
        "negative": "clean surfaces, no artifacts, no text",
    }.get(focus, "clean, well-formed subject")
    if add in prompt:
        return prompt
    return f"{prompt}, {add}"


def _review_label(mode: str, visual: str) -> str:
    """日志里的一句话结论。模拟评审不能显示成"质检通过"。"""
    if mode == "vlm":
        return "视觉评审" if visual == "performed" else f"视觉评审·{visual}"
    if mode == "technical_failed":
        return "技术检查失败"
    return "模拟评审·未做视觉检查"


def _offline_description(shot: Shot, attempt: int) -> str:
    """离线模式下的画面文字描述。

    规则明确写在这里，避免"看似智能其实随机"：
    - 第 1 次尝试有 35% 概率带一处具体缺陷（由镜头 id 决定，可复现）
    - 第 2 次起按重试动作改善，第 2 次仍有 12% 概率带缺陷
    - 缺陷类型由镜头信息决定，保证同一镜头缺陷稳定（才能触发升级规则）
    """
    genre_hint = "warm documentary tone"
    defects: list[str] = []
    roll = stable_hash(f"defect|{shot.id}|{shot.visual}|{attempt}") % 100
    if attempt == 1 and roll < 35:
        defects.append(_defect_for(shot))
    elif attempt == 2 and roll < 12:
        defects.append(_defect_for(shot))
    steam = "steam visible and rising" if "热气" in shot.acceptance or "汤" in shot.visual else "no steam"
    desc = {
        "subject": f"{shot.visual}（主体居中，动作明确）",
        "composition": f"{shot.camera}，主体占画面主体区域，留白均衡",
        "lighting": f"{genre_hint}, consistent with project style, exposure balanced",
        "defects": defects,
        "style_match": "strong — matches project tone and palette",
        "technical": {
            "sharpness": "sharp on subject, slight softness in background",
            "steam": steam,
            "attempt": attempt,
        },
    }
    return json.dumps(desc, ensure_ascii=False)


def _defect_for(shot: Shot) -> str:
    pool = (
        "slight deformation on left rim of the bowl",
        "hand appears with five fingers plus one extra knuckle",
        "weak steam, barely visible against the background",
        "faint watermark-like text artifact in bottom-right corner",
        "tone shifted cold, inconsistent with warm project palette",
        "subject placed slightly off-center, framing loose",
    )
    return pool[stable_hash(f"{shot.id}|{shot.visual}") % len(pool)]


def _speakable(text: str) -> str:
    """把解说词处理成适合 TTS 的文本：去掉书名号等不该读出来的符号。"""
    out = text.strip()
    for ch in "《》「」【】<>[]*#":
        out = out.replace(ch, "")
    return out or text
