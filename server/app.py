"""一言片场 · Web API。

路由只有六个，P0 范围：创建任务、订阅事件、取消、查状态、取成片、静态托管。
业务逻辑全部在 `app.engine` 与 `pipeline/`，这里只做协议转换。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

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
from pipeline.config import ROOT, resource_dir

from .auth import init_db, optional_user
from .auth import router as auth_router
from .history import router as history_router
from .plan import router as plan_router
from .jobs import BusyError
from .media import resolve_audio, video_response
from .media import router as media_router
from .registry import REGISTRY
from .schemas import ASPECTS, Meta, RunAccepted, RunRequest

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


# ------------------------------------------------------------------ 设备
@app.get("/api/device")
def device() -> dict:
    """设备能力检测：决定"开不开放本地模型"。

    返回真实探测到的显卡 / 显存 / 内存 / ComfyUI 安装情况，并给出档位结论
    （能跑 SDXL / 只能跑 SD1.5 / 跑不动）和下一步引导。

    为什么做成接口而不是让前端猜：显存、模型是否存在这些只有后端能查；
    让用户自己对照"8 GB 能跑什么"的参数表，就是又一次上手成本。
    """
    from pipeline.hardware import detect

    return detect()


@app.get("/api/models")
def models() -> dict:
    """模型目录：有什么可选、多少钱、有什么限制，以及当前生效的组合。"""
    from pipeline import model_registry as mr
    from pipeline.config import load_env_file

    settings = load_env_file()
    current: dict = {}
    for kind in ("image", "video"):
        prov, mid, meta = mr.resolve(settings, kind)
        current[kind] = {
            "provider": prov,
            "model": mid,
            "label": meta.get("provider_label"),
            "price_note": mr.price_note(kind, prov, mid),
            "fallback": bool(meta.get("fallback")),
            "note": meta.get("note", ""),
        }
    return {"current": current, "catalog": mr.catalog()}


@app.get("/api/device/report")
def device_report() -> dict:
    """读上次保存的体检报告；没有就现场检测一份（不落盘）。

    和设备检测接口的区别：那个是**实时**探测（每次调都跑一遍），
    这个是**存档**——用户想留一份、或者想发给别人看的时候用。
    """
    from pipeline.config import load_env_file
    from pipeline.hardware import detect, load_report

    saved = load_report(ROOT / "work")
    if saved:
        return {"saved": True, "report": saved}
    return {"saved": False, "report": detect(load_env_file())}


@app.post("/api/device/report")
def device_report_save() -> dict:
    """重新检测并保存报告（json + markdown）。「更新报告」按钮走这里。"""
    from pipeline.config import load_env_file
    from pipeline.hardware import detect, save_report

    data = detect(load_env_file())
    paths = save_report(ROOT / "work", data, __version__)
    return {"saved": True, "report": data, "paths": paths}


@app.get("/api/device/report.md")
def device_report_markdown():
    """直接下载 markdown 版报告（浏览器里另存为即可）。"""
    from fastapi.responses import PlainTextResponse

    from pipeline.config import load_env_file
    from pipeline.hardware import detect, load_report, report_markdown

    data = load_report(ROOT / "work") or detect(load_env_file())
    return PlainTextResponse(report_markdown(data, __version__))


# ------------------------------------------------------------------ 运行
@app.post("/api/runs", response_model=RunAccepted, status_code=202)
def create_run(req: RunRequest,
               user: dict | None = Depends(optional_user)) -> dict:
    """创建任务。**不强制登录**——游客也能用创作台，只是成片不归属任何人。"""
    # 音轨 token 在这里换成真实路径。引擎只认路径（它不关心 HTTP），
    # 所以解析必须发生在服务端，不能让客户端直接把路径塞进 RunOptions。
    audio = str(resolve_audio(req.audio)) if req.audio.strip() else ""
    if req.lyrics.strip() and not audio:
        # 填了歌词却没音轨，基本只有一个成因：前端把 token 弄丢了。
        # 静默忽略会得到一条"歌词没上屏"的片子，比直接拒绝难查得多。
        raise HTTPException(status_code=422, detail="填了歌词但没有音轨，请先上传音频")

    opts = RunOptions(
        theme=req.theme,
        duration=req.duration,
        aspect=req.aspect,
        style=req.style,
        shot_len=req.shot_len,
        max_shots=req.max_shots,
        live=req.live,
        guides=req.guides,
        no_cache=req.no_cache,
        painted=req.painted,
        painted_fps=req.painted_fps,
        painted_quality=req.painted_quality,
        clean_frames=req.clean_frames,
        require_real_image=req.require_real_image,
        audio=audio,
        lyrics=req.lyrics,
        music_role=req.music_role,
        bpm=req.bpm,
        beat_offset=req.beat_offset,
        auto_beats=req.auto_beats,
    )
    try:
        job = REGISTRY.create(req.theme, opts,
                              owner_id=int(user["id"]) if user else 0)
    except BusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"job_id": job.job_id, "theme": job.theme, "running": True}


@app.get("/api/runs/{job_id}")
def job_state(job_id: str) -> dict:
    job = REGISTRY.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在（服务重启后旧任务会失效）")
    return job.snapshot()


@app.get("/api/runs")
def recent_runs() -> dict:
    return {"jobs": REGISTRY.recent()}


@app.post("/api/runs/{job_id}/cancel")
def cancel_run(job_id: str) -> dict:
    job = REGISTRY.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    if job.finished:
        return {"ok": True, "detail": "任务已结束，无需停止"}
    job.cancel()
    return {"ok": True, "detail": "已请求停止，将在下一处镜头边界退出"}


def _sse(event: str, data: dict, seq: int | None = None) -> str:
    lines = []
    if seq is not None and seq >= 0:
        lines.append(f"id: {seq}")
    lines.append(f"event: {event}")
    lines.append(f"data: {json.dumps(data, ensure_ascii=False)}")
    return "\n".join(lines) + "\n\n"


@app.get("/api/runs/{job_id}/events")
def run_events(job_id: str, cursor: int = 0) -> StreamingResponse:
    """SSE 事件流。

    同步生成器交给 Starlette 在线程池里迭代，所以 `Job.stream()` 里的阻塞等待
    不会占住事件循环——这一点很关键，否则一个订阅者就能让整个 API 失去响应。
    """
    job = REGISTRY.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")

    def generator():
        yield ": connected\n\n"
        for seq, kind, data in job.stream(cursor):
            yield _sse(kind, data, seq)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/runs/{job_id}/video")
def run_video(job_id: str, request: Request):
    job = REGISTRY.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not job.video:
        raise HTTPException(status_code=404, detail="任务尚未产出成片")

    path = Path(job.video).resolve()
    runs_root = (ROOT / "runs").resolve()
    # 只允许读 runs/ 下的文件，避免任务结果被构造成任意路径读取
    if not str(path).startswith(str(runs_root)) or not path.is_file():
        raise HTTPException(status_code=404, detail="成片文件不存在或不在允许目录内")
    return video_response(path, request)


# ------------------------------------------------------------------ 成片墙与环境
app.include_router(auth_router)
app.include_router(history_router)
app.include_router(plan_router)
app.include_router(media_router)


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
    if full_path and target.is_file() and str(target).startswith(str(DIST.resolve())):
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
