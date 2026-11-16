"""一言片场 · Web API。

路由只有六个，P0 范围：创建任务、订阅事件、取消、查状态、取成片、静态托管。
业务逻辑全部在 `app.engine` 与 `pipeline/`，这里只做协议转换。
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

# 允许 `python server/app.py` 直接跑（不经 -m），也允许 `uvicorn server.app:app`
SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.engine import RunOptions
from app.version import TAGLINE, __version__
from pipeline.config import ROOT, Settings, allocate_run_id, load_env_file, resource_dir

from .auth import (
    OwnerConflict,
    OwnershipUnavailable,
    Principal,
    init_db,
    optional_principal,
    optional_principal_media,
    record_owner,
    require_principal,
)
from .auth import router as auth_router
from .branding import router as branding_router
from .exports import router as exports_router
from .history import router as history_router
from .plan import _start_render, router as plan_router
from .jobs import BusyError
from .media import resolve_audio, video_response
from .media import router as media_router
from .registry import REGISTRY
from .schemas import ASPECTS, Meta, RenderRequest, RunAccepted, RunRequest
from .taskstore import (
    INTERRUPTED_NOTE,
    TaskStoreUnavailable,
    get_task,
    init_tasks,
    list_tasks,
)
from .usage import guard_budget
from .usage import router as usage_router

#: 与 app/gui.py 的 PRESETS 保持一致——同一批示例，两套界面不该给不同的入口
PRESETS = [
    {"theme": "给我的家乡方城烩面做一条 15 秒宣传片", "duration": 15,
     "aspect": "16:9", "style": "烟火气、暖色调、纪实风"},
    {"theme": "讲一位老木匠的故事", "duration": 15,
     "aspect": "16:9", "style": "温暖、胶片感、人物纪实"},
    {"theme": "城市夜景宣传片", "duration": 30,
     "aspect": "16:9", "style": "电影感、冷色调、壮阔"},
    {"theme": "新品手机发布", "duration": 15,
     "aspect": "9:16", "style": "商业产品、干净、科技感"},
    {"theme": "介绍一处古建筑", "duration": 30,
     "aspect": "16:9", "style": "国风、水墨、沉稳"},
]

QUALITY_ITEMS = [
    {"key": "fast", "label": "快", "hint": "最省时间"},
    {"key": "medium", "label": "均衡", "hint": "默认"},
    {"key": "high", "label": "精细", "hint": "最慢"},
]

app = FastAPI(
    title="一言片场 · API",
    description="一句话进，成片出。",
    version=__version__,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)

# 首次启动建表（幂等）。放在模块级：uvicorn 导入即完成，不依赖启动事件
init_db()
# 任务表同时做一次"重启收尾"：上一进程留下的 running 一律改判中断。
init_tasks()

app.add_middleware(
    CORSMiddleware,
    # 只放开发服务器；生产态前端由本服务自己托管，同源，不走 CORS
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------------------------------------------ 元信息
@app.get("/api/meta", response_model=Meta)
def meta() -> dict:
    return {
        "version": __version__,
        "tagline": TAGLINE,
        "presets": PRESETS,
        "aspects": list(ASPECTS),
        "qualities": QUALITY_ITEMS,
    }


# ------------------------------------------------------------------ 运行
@app.post("/api/runs", response_model=RunAccepted, status_code=202)
def create_run(req: RunRequest,
               principal: Principal = Depends(require_principal)) -> dict:
    """创建任务。游客也能用，但必须有访客凭据——否则任务没有归属，谁都能看。

    前端在 localStorage 里生成并携带 `X-Guest-Id`，注册用户则走 Bearer token。
    """
    # 音轨 token 在这里换成真实路径。引擎只认路径（它不关心 HTTP），
    # 所以解析必须发生在服务端，不能让客户端直接把路径塞进 RunOptions。
    audio = str(resolve_audio(req.audio)) if req.audio.strip() else ""
    if req.lyrics.strip() and not audio:
        # 填了歌词却没音轨，基本只有一个成因：前端把 token 弄丢了。
        # 静默忽略会得到一条"歌词没上屏"的片子，比直接拒绝难查得多。
        raise HTTPException(status_code=422, detail="填了歌词但没有音轨，请先上传音频")

    # 超预算的付费拍摄在提交前就拒绝，而不是跑进管线才被逐镜护栏挡下。
    # 云端视频未配置/未接入时估价为 0，不会挡住本机免费出片。
    guard_budget(req.duration, Settings(values=load_env_file()), painted=bool(req.painted))

    # 先预留槽位，再分配目录与登记归属：被 Registry 拒绝的请求不该留下
    # 一个空项目，也不该出现“目录已建、归属未写”的半成品。
    try:
        reservation = REGISTRY.reserve()
    except BusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    # 分配目录也可能失败（ID 连续碰撞、磁盘满、权限）。它同样在预留之后，
    # 失败必须归还槽位，否则此后所有拍摄都会被"正在接纳"挡死，只能重启进程。
    try:
        run_id, project_dir = allocate_run_id(req.theme)
    except Exception as exc:
        REGISTRY.release(reservation)
        raise HTTPException(
            status_code=503, detail=f"无法分配项目目录，已拒绝起任务：{exc}"
        ) from exc
    try:
        record_owner(run_id, principal)
    except OwnerConflict as exc:
        REGISTRY.release(reservation)
        shutil.rmtree(project_dir, ignore_errors=True)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OwnershipUnavailable as exc:
        REGISTRY.release(reservation)
        shutil.rmtree(project_dir, ignore_errors=True)
        raise HTTPException(
            status_code=503, detail="归属登记失败，已拒绝起任务，请稍后重试"
        ) from exc

    opts = RunOptions(
        theme=req.theme,
        duration=req.duration,
        aspect=req.aspect,
        style=req.style,
        palette=req.palette,
        shot_len=req.shot_len,
        max_shots=req.max_shots,
        live=req.live,
        guides=req.guides,
        no_cache=req.no_cache,
        painted=req.painted,
        painted_fps=req.painted_fps,
        painted_quality=req.painted_quality,
        clean_frames=req.clean_frames,
        audio=audio,
        lyrics=req.lyrics,
        music_role=req.music_role,
        subtitle_mode=req.subtitle_mode,
        bpm=req.bpm,
        beat_offset=req.beat_offset,
        auto_beats=req.auto_beats,
        run_id=run_id,
    )
    try:
        job = REGISTRY.create(req.theme, opts, principal=principal,
                              reservation=reservation)
    except BusyError as exc:  # pragma: no cover - 预留在手时不应发生
        REGISTRY.release(reservation)
        shutil.rmtree(project_dir, ignore_errors=True)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except BaseException:
        REGISTRY.release(reservation)
        shutil.rmtree(project_dir, ignore_errors=True)
        raise
    return {"job_id": job.job_id, "theme": job.theme, "running": True}


def _stored_task(job_id: str, principal: Principal | None) -> dict | None:
    """内存里没有时查任务表。归属不符按不存在处理，失败则拒绝访问。"""
    if principal is None:
        return None
    try:
        task = get_task(job_id)
    except TaskStoreUnavailable as exc:
        # 与归属查询同一原则：读不到 ≠ 没记录，不能静默降级成"不存在"。
        raise HTTPException(status_code=503, detail="任务记录暂不可读，请稍后重试") from exc
    if not task or task.get("owner_key") != principal.key:
        return None
    return task


def _settled(status: str, error: str) -> tuple[str, str]:
    """重启后残留的 running 没有对应执行进程，如实改判中断。"""
    if status == "running":
        return "interrupted", error or INTERRUPTED_NOTE
    return status or "failed", error


def _task_snapshot(task: dict) -> dict:
    """把落库记录转成与 `Job.snapshot()` 同构的响应。"""
    status, error = _settled(str(task.get("status") or ""), str(task.get("error") or ""))
    return {
        "job_id": task.get("job_id", ""),
        "run_id": task.get("run_id", ""),
        "theme": task.get("theme", ""),
        "kind": task.get("kind", "create"),
        "status": status,
        "running": False,
        "finished": True,
        "failed": status in ("failed", "interrupted"),
        "error": error,
        "created": task.get("created", 0),
        "updated": task.get("updated", 0),
        "has_video": bool(task.get("video")),
        "cursor": 0,
        "video": task.get("video", ""),
        "result": task.get("result"),
        "options": task.get("options") or {},
    }


def _task_item(task: dict) -> dict:
    """列表摘要：不返回结果体与本地路径。"""
    status, error = _settled(str(task.get("status") or ""), str(task.get("error") or ""))
    return {
        "job_id": task.get("job_id", ""),
        "run_id": task.get("run_id", ""),
        "theme": task.get("theme", ""),
        "kind": task.get("kind", "create"),
        "status": status,
        "error": error,
        "running": False,
        "finished": True,
        "failed": status in ("failed", "interrupted"),
        "has_video": bool(task.get("has_video")),
        "created": task.get("created", 0),
        "updated": task.get("updated", 0),
        "options": task.get("options") or {},
    }


@app.get("/api/runs/{job_id}")
def job_state(job_id: str,
              principal: Principal | None = Depends(optional_principal)) -> dict:
    job = REGISTRY.get(job_id)
    if job and job.visible_to(principal):
        return job.snapshot()
    task = _stored_task(job_id, principal)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在（服务重启前的运行可能没有记录）")
    return _task_snapshot(task)


@app.get("/api/runs")
def recent_runs(limit: int = 60,
                principal: Principal | None = Depends(optional_principal)) -> dict:
    if principal is None:
        return {"jobs": []}
    size = max(1, min(int(limit), 200))
    live = REGISTRY.recent(principal)
    seen = {item["job_id"] for item in live}
    try:
        stored = list_tasks(principal.key, limit=size)
    except TaskStoreUnavailable as exc:
        raise HTTPException(status_code=503, detail="任务记录暂不可读，请稍后重试") from exc
    jobs = live + [_task_item(task) for task in stored if task["job_id"] not in seen]
    jobs.sort(key=lambda item: item.get("created") or 0, reverse=True)
    return {"jobs": jobs[:size]}


@app.post("/api/runs/{job_id}/cancel")
def cancel_run(job_id: str,
               principal: Principal | None = Depends(optional_principal)) -> dict:
    job = REGISTRY.get(job_id)
    if job and job.visible_to(principal):
        if job.finished:
            return {"ok": True, "detail": "任务已结束，无需停止"}
        job.cancel()
        return {"ok": True, "detail": "已请求停止，将在下一处镜头边界退出"}
    task = _stored_task(job_id, principal)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在（服务重启前的运行可能没有记录）")
    # 能查到就说明进程已经不在了：不假装停止成功，也不报"任务不存在"。
    return {"ok": True, "detail": "这次运行已结束（或随服务重启中断），没有可停止的进程"}


#: 能从任务记录原样重发的种类。都是"按项目拆解拍"的入口，参数只依赖项目当前状态。
_RETRY_KINDS = {"plan", "shot", "recompose", "preview", "preview_shot"}


def _opt_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _opt_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@app.post("/api/runs/{job_id}/retry")
def retry_run(job_id: str,
              principal: Principal | None = Depends(optional_principal)) -> dict:
    """重试一条失败/中断的任务。

    **只重发操作，不重放旧参数**：拍摄类任务的画面内容由项目当前的拆解决定，
    重试会按现在的最新版本重新冻结快照。这样才能把"失败前刚改好的分镜"带进去，
    而不是拿一份过时的快照再跑一遍。

    一句话成片（create）不在此列：它依赖创作台表单里的时长/风格/音轨等参数，
    这些没有完整落库，硬重试只会得到一条参数不对的片子——明确拒绝更诚实。
    """
    task = _stored_task(job_id, principal)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在或没有记录")
    status, _error = _settled(str(task.get("status") or ""), str(task.get("error") or ""))
    live = REGISTRY.get(job_id)
    if live and live.visible_to(principal) and live.running:
        raise HTTPException(status_code=409, detail="任务还在运行，先停止或等它结束")
    if status == "done":
        raise HTTPException(status_code=409, detail="任务已完成，不需要重试；要重拍请从项目页发起")
    if status == "canceled":
        raise HTTPException(status_code=409, detail="任务是被手动停止的；要重跑请从项目页发起")
    if status != "failed" and status != "interrupted":
        raise HTTPException(status_code=409, detail=f"当前状态（{status}）不能重试")

    kind = str(task.get("kind") or "create")
    if kind not in _RETRY_KINDS:
        raise HTTPException(
            status_code=422,
            detail="一句话成片的完整创作参数没有保存，无法原样重试；请回创作台重新发起",
        )
    run_id = str(task.get("run_id") or "")
    if not run_id:
        raise HTTPException(status_code=422, detail="任务没有关联项目，找不到可重拍的对象")

    options = task.get("options") if isinstance(task.get("options"), dict) else {}
    try:
        req = RenderRequest(
            live=_opt_bool(options.get("live")),
            painted=_opt_bool(options.get("painted")),
            painted_fps=_opt_int(options.get("painted_fps")),
            painted_quality=options.get("painted_quality")
            if isinstance(options.get("painted_quality"), str) else None,
            clean_frames=_opt_bool(options.get("clean_frames")),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"任务记录里的拍摄参数不可用，无法重试：{exc}"
        ) from exc

    only_shot = _opt_int(options.get("only_shot")) or 0
    if kind in ("shot", "preview_shot") and only_shot <= 0:
        raise HTTPException(status_code=422, detail="任务记录缺少镜号，无法重试单镜操作")

    if kind == "preview_shot":
        out = _start_render(run_id, req, principal, only_shot=only_shot, preview=True)
    elif kind == "preview":
        out = _start_render(run_id, req, principal, preview=True)
    elif kind == "recompose":
        out = _start_render(run_id, req, principal, recompose=True)
    elif kind == "shot":
        out = _start_render(run_id, req, principal, only_shot=only_shot)
    else:
        out = _start_render(run_id, req, principal)
    return {**out, "retried_from": job_id}


def _sse(event: str, data: dict, seq: int | None = None) -> str:
    lines = []
    if seq is not None and seq >= 0:
        lines.append(f"id: {seq}")
    lines.append(f"event: {event}")
    lines.append(f"data: {json.dumps(data, ensure_ascii=False)}")
    return "\n".join(lines) + "\n\n"


_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


@app.get("/api/runs/{job_id}/events")
def run_events(job_id: str, cursor: int = 0,
               principal: Principal | None = Depends(optional_principal_media)) -> StreamingResponse:
    """SSE 事件流。

    同步生成器交给 Starlette 在线程池里迭代，所以 `Job.stream()` 里的阻塞等待
    不会占住事件循环——这一点很关键，否则一个订阅者就能让整个 API 失去响应。
    """
    job = REGISTRY.get(job_id)
    if job and job.visible_to(principal):
        def generator():
            yield ": connected\n\n"
            for seq, kind, data in job.stream(cursor):
                yield _sse(kind, data, seq)

        return StreamingResponse(generator(), media_type="text/event-stream",
                                 headers=_SSE_HEADERS)

    # 内存里没有 = 进程已经结束过。重启前跑完（或中断）的任务仍要能给出终态，
    # 否则刷新页面后前端只会看到一条永远不关闭的流。
    task = _stored_task(job_id, principal)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在（服务重启前的运行可能没有记录）")
    view = _task_snapshot(task)

    def stored_generator():
        yield ": connected\n\n"
        if view.get("result"):
            yield _sse("done", {"result": view["result"]}, 0)
        else:
            yield _sse("error", {"message": view.get("error") or "这次运行没有产出"}, 0)

    return StreamingResponse(stored_generator(), media_type="text/event-stream",
                             headers=_SSE_HEADERS)


@app.get("/api/runs/{job_id}/video")
def run_video(job_id: str, request: Request,
              principal: Principal | None = Depends(optional_principal_media)):
    job = REGISTRY.get(job_id)
    if job and job.visible_to(principal):
        video = job.video
        if not video:
            raise HTTPException(status_code=404, detail="任务尚未产出成片")
    else:
        task = _stored_task(job_id, principal)
        if task is None:
            raise HTTPException(status_code=404, detail="任务不存在（服务重启前的运行可能没有记录）")
        video = str(task.get("video") or "")
        if not video:
            raise HTTPException(status_code=404, detail="这次运行没有产出成片")

    path = Path(video).resolve()
    runs_root = (ROOT / "runs").resolve()
    # 只允许读 runs/ 下的文件，避免任务结果被构造成任意路径读取。
    # 用 is_relative_to 而不是字符串前缀：`runs-backup/` 会通过前缀检查。
    if not path.is_relative_to(runs_root) or not path.is_file():
        raise HTTPException(status_code=404, detail="成片文件不存在或不在允许目录内")
    return video_response(path, request)


# ------------------------------------------------------------------ 成片墙与环境
app.include_router(auth_router)
app.include_router(branding_router)
app.include_router(exports_router)
app.include_router(history_router)
app.include_router(plan_router)
app.include_router(media_router)
app.include_router(usage_router)


# ------------------------------------------------------------------ 前端托管
# **必须走 resource_dir**：打包后前端产物在 PyInstaller 的解包目录（sys._MEIPASS），
# 而 ROOT 是 EXE 同级的用户数据区。写死 ROOT / "web" / "dist" 的结果是
# 源码运行正常、打包后整个页面 404 —— 这个项目在 workflows 上已经踩过一次同款。
DIST = resource_dir("web") / "dist"

if (DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")


@app.get("/{full_path:path}", include_in_schema=False)
def spa(full_path: str):
    """把前端构建产物托管在根路径，并为客户端路由做 index.html 兜底。"""
    if full_path.startswith("api"):
        raise HTTPException(status_code=404, detail="Not Found")
    target = (DIST / full_path).resolve()
    # 同上：`web/dist-backup/x` 会通过字符串前缀检查。resolve 之后按路径包含判断。
    if full_path and target.is_file() and target.is_relative_to(DIST.resolve()):
        return FileResponse(target)
    index = DIST / "index.html"
    if index.is_file():
        return FileResponse(index)
    return JSONResponse(
        {
            "detail": "前端尚未构建",
            "hint": "在 web/ 目录执行 npm install && npm run build，或直接用 npm run dev",
        },
        status_code=503,
    )
