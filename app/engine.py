"""一言片场 · 运行引擎桥接层。

把管线包成一个可在后台线程里跑的引擎，通过回调把日志、进度、结果推给 GUI。
GUI 只负责显示，不含任何业务逻辑——这样命令行与图形界面共用同一套管线。

线程模型：
- 主线程：Tkinter 界面，只做显示
- 工作线程：跑 Pipeline，通过 queue 传递事件
- 界面用 after() 轮询 queue，保证线程安全（Tkinter 不是线程安全的）
"""

from __future__ import annotations

import json
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from pipeline.config import (
    ENV_FILE,
    ROOT,
    Settings,
    existing_run_context,
    load_env_file,
    new_run_context,
    resource_dir,
)
from pipeline.decide import JevClient, RuleDecider
from pipeline.llm import CachedClient, CreativeLayer, OpenAIChatClient
from pipeline.models import MusicInput
from pipeline.pipeline import Pipeline
from pipeline.prompts import CATALOG, PromptLibrary
from pipeline.runlog import RunLog
from pipeline.images import provider_status
from pipeline.tts import build_tts_chain
from pipeline.video import FFMPEG as FFMPEG_BIN
from pipeline.video import FFPROBE as FFPROBE_BIN
from pipeline.video import ffmpeg_available, ffmpeg_filesystem_accessible

# ROOT / ENV_FILE 统一由 pipeline.config 解析（它区分源码运行与 EXE 运行）
# 进程内可替换，便于测试
SETTINGS_PATH = ENV_FILE

# 管线日志 kind → 界面显示级别
_LEVEL_MAP = {
    "error": "error",
    "guard": "warn",
    "retry": "warn",
    "check": "warn",
}


def _level_of(kind: str) -> str:
    if kind in _LEVEL_MAP:
        return _LEVEL_MAP[kind]
    if kind in ("done", "compose", "brief", "script", "storyboard"):
        return "stage"
    return "info"


# ------------------------------------------------------------------ 事件协议
@dataclass
class RunOptions:
    """一次运行的用户输入。"""

    theme: str
    duration: int = 15
    aspect: str = "16:9"
    style: str = ""
    shot_len: float = 5.0
    max_shots: int | None = None
    live: bool = False
    guides: bool = False
    no_cache: bool = False
    #: 用 p5.js 水彩引擎逐帧绘制（代码绘制后端）。与出图 provider 正交：
    #: 它自己产出整段视频，不走 Pillow / ComfyUI 的单帧链路。
    painted: bool = False
    #: MV 模式：音乐文件路径。给了就以音频为总时长权威、当作成片音轨、不生成配音。
    audio: str = ""
    #: 歌词原文（可带 LRC 时间戳）。为空则上屏文字退回解说词。
    lyrics: str = ""
    #: 音乐角色：song=音乐即音轨（MV，不生成配音）；bgm=背景配乐（保留配音，音乐压低）
    music_role: str = "song"
    #: 手工指定的 BPM / 首拍（0 表示自动估计）
    bpm: float = 0.0
    beat_offset: float = 0.0
    #: 是否自动估计节拍并据此吸附镜头切点
    auto_beats: bool = True
    #: 只允许真图：勾选后不挂占位帧兜底。默认 False（验收/断网演示需要兜底）。
    require_real_image: bool = False
    #: 代码绘制的帧率。水彩逐帧约 1 秒/帧，长片靠降帧率换时间（12fps 观感仍可接受）
    painted_fps: int = 24
    #: 编码成功后清掉帧缓存（3 分钟 1080p 约 2.4 GB）。会失去断点续渲，但成片已产出
    clean_frames: bool = False
    #: 水彩画质档位：high（出厂 754ms/帧）/ medium（695）/ fast（544）。
    #: 只在 painted=True 时有意义。降档是目前唯一有效的提速手段
    #: （加线程几乎无效：1→8 页吞吐只从 0.81 涨到 1.16 fps）。
    painted_quality: str = "medium"
    #: 从拆解文件拍摄：指向 `runs/<run_id>/plan`。非空时走 `render_from_plan()`，
    #: 不再重新调用大模型——拍什么由文件决定，不由内存决定。
    plan_dir: str = ""
    #: 单镜重拍：只重拍这一镜（镜号从 1 开始），其余镜头复用磁盘上的旧片段。
    #: 0 = 整片拍摄。只在 `plan_dir` 非空时有意义——没有拆解就没有"第几镜"。
    only_shot: int = 0
    #: 只重合成：不重新出图，按 `storyboard.json` 里现在的镜长与顺序把
    #: `work/clips` 的片段对齐后重拼。时间轴编辑走这条路。
    #: 与 `only_shot` 互斥——重拍一镜是"重画一镜"，重合成是"一镜都不重画"。
    recompose: bool = False


class Engine:
    """管线引擎。所有回调都在工作线程里被调用。"""

    def __init__(
        self,
        on_log: Callable[[str, str], None] | None = None,
        on_progress: Callable[[str, float], None] | None = None,
        on_env: Callable[[dict], None] | None = None,
        on_done: Callable[[dict], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ):
        self.on_log = on_log or (lambda level, msg: None)
        self.on_progress = on_progress or (lambda stage, pct: None)
        self.on_env = on_env or (lambda data: None)
        self.on_done = on_done or (lambda result: None)
        self.on_error = on_error or (lambda msg: None)
        self._thread: threading.Thread | None = None
        self._running = False
        self._cancel = threading.Event()

    @property
    def running(self) -> bool:
        return self._running

    def cancel(self) -> None:
        """请求停止。

        不是强杀线程：管线会在下一个镜头边界自行退出（见 Pipeline._stop_requested），
        这样不会留下半截视频文件，已跑完的镜头还能正常进成片。
        """
        self._cancel.set()

    # ------------------------------------------------------------ 环境体检
    def _refresh_env_bg(self, settings: Settings) -> None:
        """后台跑体检并回填界面。失败静默——体检只是显示，不是出片前提。"""
        try:
            self.on_env(self.check_env(settings))
        except Exception:  # noqa: BLE001
            pass

    def check_env(self, settings: Settings | None = None, fresh: bool = False) -> dict:
        """同步体检，供界面在按钮回调里直接调用。

        三态而非两态：`ok` / `warn` / `bad`。
        「ComfyUI 没启动」「云端 Key 没配」属于**设计内的降级**，标 warn 而不是 bad——
        否则界面会一直显示"环境有问题"，把正常状态说成故障。

        fresh=True 绕过 provider_status 的 30 秒缓存（用户主动点体检时用）。
        """
        settings = settings or Settings(values=load_env_file())
        data: dict[str, Any] = {"settings": settings, "checks": [], "ready": True, "degraded": []}
        try:
            lib = PromptLibrary.load()
            audit = lib.audit()
            data["checks"].append(
                {
                    "name": "提示词库",
                    "state": "bad" if audit["problems"] else "ok",
                    "detail": f"{audit['sections']} 章节 / {audit['slots']} 槽位"
                    + ("；" + "；".join(audit["problems"]) if audit["problems"] else ""),
                }
            )
            if audit["problems"]:
                data["ready"] = False
        except Exception as exc:  # noqa: BLE001
            data["checks"].append({"name": "提示词库", "state": "bad", "detail": str(exc)})
            data["ready"] = False

        ff_ok, ff_detail = ffmpeg_available()
        data["checks"].append(
            {"name": "FFmpeg", "state": "ok" if ff_ok else "bad", "detail": ff_detail}
        )
        data["ready"] &= ff_ok

        # 版本探测通过 ≠ 能出片：受限环境里 ffmpeg 可执行但被禁止文件 I/O。
        # 这一项独立成条，避免真跑到出片阶段才发现（实测踩过，报错极具误导性）。
        if ff_ok:
            io_ok, io_detail = ffmpeg_filesystem_accessible()
            data["checks"].append(
                {"name": "FFmpeg 写盘", "state": "ok" if io_ok else "bad", "detail": io_detail}
            )
            data["ready"] &= io_ok
            if not io_ok:
                data["degraded"].append("FFmpeg 无法读写磁盘 → 本机出片不可用")

        # 代码绘制后端（p5.js 水彩引擎）。它是**可选**后端，缺失只降级不影响出片，
        # 所以状态缺失时给 warn 而不是 bad——否则没装 Node 的用户会以为项目坏了。
        try:
            from pipeline import painted as painted_backend
            pst = painted_backend.check_env()
            data["checks"].append({
                "name": "代码绘制",
                "state": "ok" if pst.ok else "warn",
                "detail": pst.detail if pst.ok else f"不可用（不影响出片）：{pst.detail}",
            })
            if not pst.ok:
                data["degraded"].append("代码绘制后端不可用 → 该模式已停用，可用 Pillow / ComfyUI")
        except Exception as exc:  # noqa: BLE001
            data["checks"].append({"name": "代码绘制", "state": "warn",
                                    "detail": f"模块加载失败（不影响出片）：{exc}"})

        for item in provider_status(settings, resource_dir("workflows"), fresh=fresh):
            prov = item["provider"]
            if prov == "comfy":
                state = "ok" if item["available"] == "是" else "warn"
                if state == "warn":
                    data["degraded"].append("ComfyUI 未运行 → 出图降级为离线占位帧")
            elif prov == "cloud":
                # 云端出图本来就没接入，且用户没配端点——这不是故障，
                # 显示成「!」会让人以为环境有问题。未配置就整行不显示。
                if not (settings.has("VIDEO_API_BASE_URL") or settings.has("VIDEO_API_KEY")):
                    continue
                state = "warn"
            else:
                state = "ok" if item["available"] == "是" else "warn"
            data["checks"].append(
                {"name": f"出图 {prov}", "state": state, "detail": item["detail"]}
            )

        for prov in build_tts_chain(settings):
            ok, why = prov.available()
            if ok:
                data["checks"].append({"name": f"配音 {prov.name}", "state": "ok", "detail": why})
            else:
                data["checks"].append({"name": f"配音 {prov.name}", "state": "warn", "detail": why})
                data["degraded"].append("配音不可用 → 成片为静音轨")

        data["keys"] = {
            "LLM_API_KEY": settings.masked("LLM_API_KEY"),
            "JEV_API_KEY": settings.masked("JEV_API_KEY"),
            "VLM_MODEL": settings.get("VLM_MODEL") or "(未配置)",
        }
        data["live_ready"] = settings.has("LLM_API_KEY")
        data["jev_ready"] = settings.has("JEV_API_KEY")
        self._last_llm_ok = data["live_ready"]

        # 配了 Key 就真发一次最小请求验证。之前只看「Key 非空」，于是密钥打错、
        # 端点漏 /v1、模型名不存在都要等跑片时才炸，用户只看到一句看不懂的
        # 「语言大模型调用失败（deepseek-chat）：HTTP 401」。这里提前到体检里，
        # 直接把可执行的修复建议摆出来。
        if data["live_ready"]:
            probe = OpenAIChatClient(
                base_url=settings.get("LLM_BASE_URL"),
                api_key=settings.get("LLM_API_KEY"),
                model=settings.get("LLM_MODEL"),
            )
            llm_ok, llm_detail = probe.diagnose()
            data["checks"].append(
                {"name": "大模型连通", "state": "ok" if llm_ok else "bad", "detail": llm_detail}
            )
            data["ready"] &= llm_ok
            self._last_llm_ok = llm_ok
            if not llm_ok:
                data["degraded"].append("大模型不可用 → 创意层自动降级为离线引擎")
        else:
            data["checks"].append(
                {"name": "大模型连通", "state": "warn",
                 "detail": "未配置 LLM_API_KEY，创意层使用离线引擎（不影响出片）"}
            )
        return data

    # ------------------------------------------------------------ 启动运行
    def _apply_painted(self, result: dict, opts: RunOptions) -> dict:
        """用代码绘制后端重出成片，就地替换 result["compose"]。

        逻辑已抽到 ``pipeline.painted.apply_painted``，与命令行共用同一条通路
        （否则命令行没法复现 MV 演示，而这段逻辑又长到不该复制两份）。
        这里只做转发：把界面的 log/progress 回调传进去。
        """
        from pipeline import painted as pb

        return pb.apply_painted(result, log=self.on_log, progress=self.on_progress,
                                fps=max(4, int(opts.painted_fps or 24)),
                                clean_frames_after=bool(opts.clean_frames),
                                quality=getattr(opts, "painted_quality", "medium"))

    # ------------------------------------------------------------ 只拆解
    def plan(self, opts: RunOptions, settings: Settings | None = None) -> dict[str, Any]:
        """只跑创意层三段（简报→剧本→分镜），落盘 plan/，不渲染。

        与 `_worker` 前半段同源：都走 `Pipeline.build_plan()`，所以"先拆解"和
        "一次跑完"拿到的是同一份分镜。这里不做日志中继——调用方一次性拿结果。
        """
        settings = settings or Settings(values=load_env_file())
        ctx = new_run_context(opts.theme, settings, live=opts.live, dry_run=False, verbose=False)
        ctx.prepare()
        pipe, creative, live = self._make_pipeline(ctx, opts, settings)

        brief, script, board = pipe.build_plan(
            opts.theme, opts.duration, opts.aspect, opts.style, opts.max_shots
        )

        plan_dir = ctx.project_dir / "plan"
        plan_dir.mkdir(parents=True, exist_ok=True)
        for name, payload in (
            ("brief.json", brief.to_dict()),
            ("script.json", script.to_dict()),
            ("storyboard.json", board.to_dict()),
        ):
            (plan_dir / name).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        (plan_dir / "meta.json").write_text(
            json.dumps(
                {
                    "run_id": ctx.run_id,
                    "live": live,
                    "model": settings.get("LLM_MODEL") if live else "",
                    "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "degradations": list(getattr(creative, "degradations", [])),
                    "edited": {"script": False, "shots": []},
                    # 只拆解、还没拍摄。成片墙据此把这类记录排除掉——
                    # 它连一个镜头都没出，判成「失败」是错的。
                    "rendered": False,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        return {
            "run_id": ctx.run_id,
            "project": board.project,
            "script": script.to_dict(),
            "subjects": [s.__dict__ for s in board.subjects],
            "shots": [shot.to_dict() for shot in board.shots],
            "notes": list(ctx.notes),
            "live": live,
        }

    def replan_act(
        self,
        opts: RunOptions,
        act_name: str,
        force: bool = False,
        settings: Settings | None = None,
    ) -> dict[str, Any]:
        """只重拆一场。不渲染，只改拆解文件里的分镜。

        复用原来的 run 目录（与"从拆解文件拍摄"同理）：重拆的日志、降级原因
        都要和这份拆解待在一起，否则用户点完按钮找不到任何痕迹。
        """
        settings = settings or Settings(values=load_env_file())
        if not opts.plan_dir:
            raise RuntimeError("按场重拆需要指定拆解目录")
        plan_dir = Path(opts.plan_dir)
        ctx = existing_run_context(plan_dir.parent.name, settings, live=opts.live)
        ctx.prepare()
        pipe, creative, live = self._make_pipeline(ctx, opts, settings)
        result = pipe.replan_act(plan_dir, act_name, force=force)
        result.update(
            {
                "run_id": ctx.run_id,
                "live": live,
                "degradations": list(getattr(creative, "degradations", [])),
                "notes": list(ctx.notes),
            }
        )
        return result

    def _make_pipeline(
        self, ctx: Any, opts: RunOptions, settings: Settings
    ) -> tuple[Pipeline, CreativeLayer, bool]:
        """按选项装配管线。`plan()` 与 `replan_act()` 共用这一份。

        两处各写一遍的话，重拆时漏带 no_cache / guides 就会表现为"同样的设置，
        重拆出来的东西和初次拆解不一样"，而且不报错，很难查。
        """
        lib = PromptLibrary.load()

        client = None
        if opts.live and settings.has("LLM_API_KEY"):
            inner = OpenAIChatClient(
                base_url=settings.get("LLM_BASE_URL"),
                api_key=settings.get("LLM_API_KEY"),
                model=settings.get("LLM_MODEL"),
            )
            client = inner if opts.no_cache else CachedClient(inner, ROOT / "cache" / "llm")

        decider = (
            JevClient(base_url=settings.get("JEV_BASE_URL"), api_key=settings.get("JEV_API_KEY"), lib=lib)
            if settings.has("JEV_API_KEY")
            else RuleDecider(lib)
        )
        creative = CreativeLayer(lib, client)
        pipe = Pipeline(
            settings, lib, ctx, RunLog(ctx.log_path, verbose=False), creative, decider,
            avg_shot_len=opts.shot_len, guides=opts.guides,
            require_real_image=opts.require_real_image,
            progress=lambda stage, pct: None,
            should_stop=lambda: False,
        )
        return pipe, creative, bool(client)

    # -------------------------------------------------------------- 自检
    def start(self, opts: RunOptions, settings: Settings | None = None) -> bool:
        if self._running:
            self.on_error("已有任务在运行，请等待当前任务完成")
            return False
        self._cancel.clear()
        self._running = True
        self._thread = threading.Thread(target=self._worker, args=(opts, settings), daemon=True)
        self._thread.start()
        return True

    def _worker(self, opts: RunOptions, settings: Settings | None) -> None:
        try:
            settings = settings or Settings(values=load_env_file())
            lib = PromptLibrary.load()
            plan_dir = Path(opts.plan_dir) if opts.plan_dir else None
            if plan_dir and opts.only_shot:
                # 代码绘制是**按整段分镜编译**成 JS 场景再逐帧渲染的，没有"只重画
                # 一镜"的入口。让它跑下去的结果是把整段重画一遍（几十分钟），
                # 与"只重拍一镜"的预期正相反，而且中途不会有任何提示。
                # 在动手之前就拒掉，别等用户等完一小时才发现白等。
                if opts.painted:
                    raise RuntimeError(
                        "代码绘制模式不支持单镜重拍（它是按整段分镜编译的），"
                        "请改用「开始拍摄」整片重拍"
                    )
            if plan_dir and opts.recompose and opts.painted:
                # 代码绘制把整段分镜编译成一条视频，产物落在 out/ 而不是
                # work/clips/shotNN.mp4，没有"按镜重拼"的素材。硬跑下去会在
                # `_recompose` 里报"片段不在 work/clips"——那句话看着像文件被删了，
                # 实际是这条路根本不适用，得在起任务前说清楚。
                raise RuntimeError(
                    "代码绘制模式的产物是一整段视频，没有逐镜片段可以重拼，"
                    "不支持只重合成；请改用「开始拍摄」整片重拍"
                )
            if plan_dir:
                # 从拆解文件拍：**复用原来的 run 目录**，让 plan 与成片待在一起。
                ctx = existing_run_context(plan_dir.parent.name, settings, live=opts.live)
            else:
                ctx = new_run_context(opts.theme, settings, live=opts.live, dry_run=False, verbose=False)
            ctx.prepare()
            log = RunLog(ctx.log_path, verbose=False)

            # 让管线日志冒泡到界面。用包装器而非直接替换，保留原返回值
            # （Pipeline 依赖 event() 的返回记录，runlog 内部也用它做计数）。
            orig_event = log.event
            orig_error = log.error

            def relay_event(kind: str, message: str, **fields: Any):
                self.on_log(_level_of(kind), message)
                return orig_event(kind, message, **fields)

            def relay_error(where: str, msg: str, **fields: Any):
                self.on_log("error", f"{where}：{msg}")
                return orig_error(where, msg, **fields)

            log.event = relay_event  # type: ignore[method-assign]
            log.error = relay_error  # type: ignore[method-assign]

            client = None
            if opts.live and settings.has("LLM_API_KEY"):
                inner = OpenAIChatClient(
                    base_url=settings.get("LLM_BASE_URL"),
                    api_key=settings.get("LLM_API_KEY"),
                    model=settings.get("LLM_MODEL"),
                )
                client = inner if opts.no_cache else CachedClient(inner, ROOT / "cache" / "llm")
                self.on_log("env", f"创意层：真实模型 {inner.model}（端点 {inner._endpoint()}）")
            elif opts.live:
                # 之前这里写成了未定义的 relay(...)，勾选「真实模型」又没配 Key 时
                # 会抛 NameError，整次运行直接判失败——属于必现崩溃。
                self.on_log("warn", "已选真实模型，但未配置 LLM_API_KEY，本次仍走离线创意引擎。")

            decider = (
                JevClient(base_url=settings.get("JEV_BASE_URL"), api_key=settings.get("JEV_API_KEY"), lib=lib)
                if settings.has("JEV_API_KEY")
                else RuleDecider(lib)
            )
            creative = CreativeLayer(lib, client)
            pipe = Pipeline(
                settings, lib, ctx, log, creative, decider,
                avg_shot_len=opts.shot_len, guides=opts.guides,
            require_real_image=opts.require_real_image,
                # 真实阶段进度由管线推；不再在启动前一次性推完假进度
                progress=self.on_progress,
                should_stop=self._cancel.is_set,
            )

            self.on_progress("准备", 0.02)
            # 体检**不阻塞出片**：它的作用只是刷新界面上的环境面板。
            # 之前是同步调用，而体检里有真实网络探测（ComfyUI 未启动时要走到
            # 连接超时），于是每次点「开始生成」都先白等才动手——用户感知就是
            # "程序慢"。改成后台线程 + 30 秒缓存，出片立刻开始。
            threading.Thread(target=self._refresh_env_bg, args=(settings,),
                             daemon=True).start()

            music = (
                MusicInput(path=opts.audio, lyrics=opts.lyrics, role=opts.music_role,
                           bpm=opts.bpm, offset=opts.beat_offset,
                           auto_beats=opts.auto_beats)
                if opts.audio else None
            )
            if plan_dir:
                # 先拆后拍：分镜从 plan/ 读，时长按用户改过的值重算。
                # 时间轴编辑走 `recompose_from_plan`——同一份 plan，一个像素都不重画。
                if opts.recompose:
                    result = pipe.recompose_from_plan(plan_dir, music=music)
                else:
                    result = pipe.render_from_plan(plan_dir, music=music,
                                                   only_shot=int(opts.only_shot or 0))
            else:
                result = pipe.run(
                    opts.theme,
                    total_duration=opts.duration,
                    aspect_ratio=opts.aspect,
                    style=opts.style or None,
                    max_shots=opts.max_shots,
                    # MV 模式：给了音频就以音频为总时长权威，并把它当成片音轨。
                    # 音频不存在时早失败（在管线里抛），不静默退回解说模式。
                    music=music,
                )

            # 代码绘制后端：用 p5.js 水彩引擎重出整段视频，替换掉占位帧版本。
            # 放在 pipe.run() 之后：前面的创意/决策/分镜完全复用，省掉一整轮 LLM 调用。
            if opts.painted:
                result = self._apply_painted(result, opts)

            self.on_progress("完成", 1.0)
            self.on_done(result)
        except Exception as exc:  # noqa: BLE001
            detail = traceback.format_exc()[-1200:]
            self.on_error(f"{exc}\n\n{detail}")
        finally:
            self._running = False


# ------------------------------------------------------------------ 自检
def selftest(full: bool = True, timeout: int = 300) -> dict:
    """打包后自检：验证「从 EXE 运行时」的关键路径是否成立。

    打包形态最容易坏在四处，逐一验证：
    1. ROOT 是否指向 EXE 所在目录（而不是 PyInstaller 临时目录，否则成片会丢）
    2. prompts.yaml 能否从随包目录读到
    3. ffmpeg/ffprobe 能否定位
    4. 管线能否真的跑出成片

    结果写入 `self_test_report.txt`（windowed EXE 无控制台，只能落盘）。
    """
    import time

    lines: list[str] = []
    report: dict[str, Any] = {}

    def say(text: str) -> None:
        """收集到缓冲区；windowed EXE 下 stdout 为 None，print 会抛异常，必须容错。

        每条都实时落盘：EXE 无控制台，报告文件是唯一的可观测通道；
        万一中途卡死或崩溃，已写入的内容就是排查依据。
        """
        lines.append(text)
        try:
            print(text)
        except Exception:  # noqa: BLE001
            pass
        flush()

    def flush() -> None:
        """把缓冲区落盘。EXE 无控制台，这是唯一的可观测通道，异常时也要留下痕迹。"""
        try:
            (ROOT / "self_test_report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    say("=== 一言片场 · 打包形态自检 ===")
    say(f"冻结运行(frozen) = {getattr(sys, 'frozen', False)}")
    say(f"ROOT            = {ROOT}")
    say(f"提示词库路径    = {CATALOG}")
    say(f"提示词库存在    = {CATALOG.exists()}")
    say(f"FFmpeg          = {FFMPEG_BIN}")
    say(f"FFprobe         = {FFPROBE_BIN}")
    say("")
    report["frozen"] = bool(getattr(sys, "frozen", False))
    report["root"] = str(ROOT)
    report["catalog"] = str(CATALOG)
    report["catalog_ok"] = CATALOG.exists()

    eng = Engine()
    data = eng.check_env()
    say("--- 环境体检 ---")
    for c in data.get("checks", []):
        mark = {"ok": "✓", "warn": "!", "bad": "✗"}.get(c.get("state", "ok"), "·")
        say(f"{mark} {c['name']:<14} {c['detail']}")
    for d in dict.fromkeys(data.get("degraded", [])):
        say(f"  · 降级：{d}")
    say("")
    report["env_ready"] = bool(data.get("ready"))

    if full:
        say("--- 跑通一条 15 秒片 ---")
        ui_result: list[dict] = []
        errors: list[str] = []
        done_flag = threading.Event()

        def on_done(res: dict) -> None:
            ui_result.append(res)
            done_flag.set()

        def on_error(msg: str) -> None:
            errors.append(msg)
            done_flag.set()

        eng2 = Engine(on_done=on_done, on_error=on_error)
        eng2.start(RunOptions(theme="给我的家乡方城烩面做一条 15 秒宣传片", duration=15))
        finished = done_flag.wait(timeout)
        say(f"运行结束 = {finished}（耗时 ≤ {timeout}s）")
        if errors:
            say(f"异常：{errors[0][:400]}")
        if ui_result:
            comp = ui_result[0].get("compose", {})
            video = Path(comp.get("video", ""))
            say(f"成片          = {video}")
            say(f"成片存在      = {video.exists()}")
            say(f"成片体积      = {comp.get('size_mb')} MB")
            say(f"决策次数      = {ui_result[0].get('decisions')}")
            # 关键：成片必须落在 EXE 同级目录，而不是 PyInstaller 临时目录。
            # 路径结构为 ROOT/runs/<run_id>/output/final.mp4
            same_dir = str(video).startswith(str(ROOT)) if video else False
            say(f"成片落在 ROOT = {same_dir}（必须为 True，否则退出后成片会丢）")
            report["video"] = str(video)
            report["video_ok"] = video.exists()
            report["video_in_root"] = same_dir
            report["decisions"] = ui_result[0].get("decisions", 0)
        else:
            report["video_ok"] = False
        report["errors"] = errors[:1]
        _ = time

    ok = (
        bool(report.get("catalog_ok"))
        and bool(report.get("env_ready"))
        and bool(report.get("video_ok", True))
        and bool(report.get("video_in_root", True))
    )
    report["pass"] = ok
    say("")
    say(f"结论：{'通过' if ok else '未通过'}")
    flush()
    report["report_path"] = str(ROOT / "self_test_report.txt")
    return report


# ------------------------------------------------------------------ 历史记录
@dataclass
class RunRecord:
    """一次历史运行的摘要。"""

    run_id: str
    path: Path
    title: str = ""
    created: str = ""
    video: Path | None = None
    subtitle: Path | None = None
    storyboard: Path | None = None
    duration: float = 0.0
    shots: int = 0
    notes: int = 0
    size_mb: float = 0.0


def scan_runs(limit: int = 200) -> list[RunRecord]:
    """扫描 runs/ 目录，读取每次运行的产出摘要。倒序（最新在前）。"""
    import json

    runs_dir = ROOT / "runs"
    if not runs_dir.exists():
        return []
    records: list[RunRecord] = []
    for item in sorted(runs_dir.iterdir(), reverse=True):
        if not item.is_dir():
            continue
        rec = RunRecord(run_id=item.name, path=item)
        out = item / "output"
        rec.video = out / "final.mp4"
        rec.subtitle = out / "subtitles.srt"
        sb = out / "storyboard.json"
        rec.storyboard = sb
        if rec.video.exists():
            rec.size_mb = rec.video.stat().st_size / (1024 * 1024)
        if sb.exists():
            try:
                data = json.loads(sb.read_text(encoding="utf-8"))
                rec.title = str(data.get("project", {}).get("title", "")) or item.name.split("-", 2)[-1]
                rec.duration = round(float(data.get("project", {}).get("total_duration", 0)), 1)
                rec.shots = len(data.get("shots", []))
            except Exception:  # noqa: BLE001
                rec.title = item.name.split("-", 2)[-1]
        else:
            rec.title = item.name.split("-", 2)[-1]
        log = item / "run.jsonl"
        if log.exists():
            # 只数「人工关注」事件。曾经用 '"note"' 子串匹配，把配音日志里的
            # "note":"原速" 也算进去，备注数虚高。
            try:
                for line in log.read_text(encoding="utf-8").splitlines():
                    if '"human_note"' in line:
                        rec.notes += 1
            except Exception:  # noqa: BLE001
                pass
        records.append(rec)
        if len(records) >= limit:
            break
    return records