"""拆解层接口：拆、改、重拆、拍。

四段职责分开，因为代价差着量级：

- **拆**（`POST /api/plan`）：一次大模型往返，5–20 秒，可反复跑；
- **改**（`PATCH .../script`、`PATCH .../shots/{id}`）：纯文件操作，毫秒级；
- **重拆**（`POST .../acts/{act_name}/replan`）：只对一场再问一次模型，秒级；
- **拍**（`POST .../render`）：逐镜出图 + 合成，分钟级，不可反复。

改剧本**不自动**重派生分镜，只标 `stale` 并列出受影响镜号。看起来"智能"，
实际上会把用户刚调好的镜头整体冲掉——重拆必须由人按场发起，且那一场若有
手改过的镜头，要先确认（`force`）。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from app.engine import Engine, RunOptions, render_profile_of
from pipeline import palette as palettes
from pipeline import subtitle_style as sstyle
from pipeline.compose import BGM_GAIN, TRANSITIONS
from pipeline.config import ROOT, allocate_run_id
from pipeline.models import MAX_SHOT_SECONDS, MIN_SHOT_SECONDS
from pipeline.tts import parse_srt
from pipeline.video import probe_duration
from pipeline.waveform import peaks as waveform_peaks

from .auth import (
    OwnerConflict,
    OwnershipUnavailable,
    Principal,
    optional_principal,
    optional_principal_media,
    record_owner,
    require_principal,
)
# 可见性规则只写一份：拆解文件与成片是同一份数据的两个阶段，
# 成片墙挡住的东西拆解页也必须挡住，否则换个入口就能绕过。
from .history import _can_view
from .usage import guard_budget
from .jobs import BusyError
from .media import read_audio_meta, resolve_audio
from .registry import REGISTRY
from .schemas import (
    ReorderRequest,
    ReplanRequest,
    RenderRequest,
    RunRequest,
    PaletteBody,
    ScriptPatch,
    ShotPatch,
    SubjectPatch,
    SubtitleStyleBody,
    SubtitlesBody,
)

router = APIRouter(prefix="/api")

PLAN_FILES = ("brief.json", "script.json", "storyboard.json", "meta.json")


def _safe(run_id: str, principal: Principal | None = None) -> Path:
    if not run_id or run_id in (".", "..") or "/" in run_id or "\\" in run_id:
        raise HTTPException(status_code=400, detail="非法的运行 ID")
    root = (ROOT / "runs").resolve()
    path = (root / run_id).resolve()
    if path.parent != root or not path.is_dir():
        raise HTTPException(status_code=404, detail="运行不存在")
    if not _can_view(run_id, principal):
        raise HTTPException(status_code=404, detail="运行不存在")
    return path


def _plan_dir(run_id: str, principal: Principal | None = None) -> Path:
    plan_dir = _safe(run_id, principal) / "plan"
    if not plan_dir.is_dir():
        raise HTTPException(status_code=404, detail="这次运行没有拆解信息")
    return plan_dir


# ------------------------------------------------------------------ 并发与版本
#: 每个项目一把可重入锁。同一项目的"读 → 改 → 写"必须在锁内完成，
#: 否则两个请求都读到旧版本，后写的那个会覆盖先写的那个（实测丢更新）。
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


@contextmanager
def _project_lock(run_id: str) -> Iterator[None]:
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(run_id, threading.RLock())
    with lock:
        yield


def _revision_of(meta: dict) -> int:
    try:
        return max(0, int(meta.get("revision") or 0))
    except (TypeError, ValueError):
        return 0


def _check_revision(meta: dict, expected: int | None) -> None:
    """请求带了期望版本就严格比对；冲突回 409，让前端刷新后重试。

    不带期望版本时仍然有项目锁兜底——并发不会丢修改，只是"最后写入者赢"。
    前端会带上它，从而把"隐形的覆盖"变成用户可见的冲突。
    """
    if expected is None:
        return
    current = _revision_of(meta)
    if expected != current:
        raise HTTPException(
            status_code=409,
            detail=(
                f"这份拆解已被修改（当前版本 {current}，你基于版本 {expected} 提交）。"
                "刷新页面后再改，避免把别人的修改覆盖掉。"
            ),
        )


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"拆解文件损坏：{path.name}（{exc}）") from exc


def _write(path: Path, payload: dict) -> None:
    """原子写：先落**唯一**临时文件再替换。

    拆解文件是拍摄阶段的唯一输入，写到一半被打断就等于这次拆解作废。
    临时文件名必须唯一——固定 `.json.tmp` 会让两个并发写入争用同一个文件，
    最终落盘的内容可能是一半一半。
    """
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _touch_meta(plan_dir: Path, *, shot_id: int | None = None,
                subject_id: str | None = None) -> dict:
    """记下"哪些是你改过的"，并把版本号 +1。

    没有 edited 字段，重跑之后就分不清"这是模型的判断还是我的修改"——
    再次拆解会覆盖文件，人的工作痕迹就彻底没了。版本号是并发冲突的判据。
    """
    meta = _read(plan_dir / "meta.json")
    edited = meta.setdefault("edited", {})
    edited.setdefault("script", False)
    edited.setdefault("shots", [])
    edited.setdefault("subjects", [])
    if subject_id is not None:
        if subject_id not in edited["subjects"]:
            edited["subjects"].append(subject_id)
    elif shot_id is None:
        edited["script"] = True
    else:
        if shot_id not in edited["shots"]:
            edited["shots"].append(int(shot_id))
    meta["edited_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    meta["revision"] = _revision_of(meta) + 1
    _write(plan_dir / "meta.json", meta)
    return meta


#: 保留多少个编辑版本。每次改镜/改剧本都存一份完整 JSON，草稿本身很小；
#: 20 版足够回溯到"改坏之前"，又不会让 runs/ 悄悄膨胀。
VERSION_KEEP = 20


def _archive_version(plan_dir: Path) -> int:
    """把当前拆解存成一个可恢复版本，返回版本号。

    版本目录与拍摄快照分开：快照是"这次拍的输入"，版本是"我改到哪一步"。
    恢复只动剧本/分镜/简报，音轨、渲染配置、已拍标记仍以当前 meta 为准——
    否则恢复一次旧分镜会把后来换掉的音轨一起回滚。
    """
    meta = _read(plan_dir / "meta.json")
    revision = _revision_of(meta)
    root = plan_dir / "versions"
    dest = root / f"rev-{revision:06d}"
    if not dest.is_dir():
        dest.mkdir(parents=True, exist_ok=True)
        for name in PLAN_FILES:
            src = plan_dir / name
            if src.is_file():
                shutil.copyfile(src, dest / name)
    try:
        versions = sorted(p for p in root.iterdir() if p.is_dir())
        for stale in versions[:-VERSION_KEEP]:
            shutil.rmtree(stale, ignore_errors=True)
    except OSError:
        pass
    return revision


def _resync(board: dict) -> tuple[float, list[dict]]:
    """重算每个镜头的 `start` 与总时长，返回 (总时长, 起点变了的镜头)。

    用户改了某镜时长，后面所有镜头的起点都会跟着挪；不重算，字幕时间码与
    合成时的裁剪点就全错位了。
    """
    shots = board.get("shots") or []
    before = {int(s.get("id", 0)): float(s.get("start") or 0.0) for s in shots}
    t = 0.0
    moved: list[dict] = []
    for s in shots:
        sid = int(s.get("id", 0))
        s["start"] = round(t, 3)
        t += float(s.get("duration") or 0.0)
        if abs(s["start"] - before.get(sid, 0.0)) > 1e-6:
            moved.append({"id": sid, "start": s["start"]})
    total = round(t, 3)
    board.setdefault("project", {})["total_duration"] = total
    return total, moved


def _shot_bounds(board: dict) -> list[str]:
    """越界镜头的说明列表。空列表 = 全部合规。

    下限不是写死的 `MIN_SHOT_SECONDS`，而是和 `build_timeline` 用同一个公式
    `min(MIN_SHOT_SECONDS, 总时长 / 镜数)`。两处各写一套的结果是：一段 4 秒的
    音频被拆成 3 镜，每镜 1.33 秒——`build_timeline` 认为这已经是最优（总时长
    装不下 3 个 2 秒镜头，它主动压低了 floor），而这里却把这份拆解判成"不合规"，
    于是**拆得出来、拍不了**。MV 模式下音频可以短到几秒，这个矛盾立刻暴露。
    """
    shots = board.get("shots") or []
    parsed: list[tuple[int, float]] = []
    bad: list[str] = []
    for s in shots:
        sid = int(s.get("id", 0))
        try:
            parsed.append((sid, float(s.get("duration") or 0.0)))
        except (TypeError, ValueError):
            bad.append(f"镜 {sid} 的时长不是数字：{s.get('duration')!r}")

    total = sum(d for _, d in parsed)
    # 下限也要按落盘精度取整：镜头时长存的是 3 位小数（1.333），
    # 拿未取整的 1.33333 去比，会把自己刚写出来的合法值判成越界。
    floor = round(min(MIN_SHOT_SECONDS, total / len(parsed)), 3) if parsed else MIN_SHOT_SECONDS
    for sid, d in parsed:
        if d < floor - 1e-6:
            bad.append(f"镜 {sid} 时长 {d:g}s 短于下限 {floor:g}s")
        elif d > MAX_SHOT_SECONDS + 1e-6:
            bad.append(f"镜 {sid} 时长 {d:g}s 超出上限 {MAX_SHOT_SECONDS:g}s")
    return bad


def _opts(req: RunRequest, *, audio: str = "", duration: int | None = None,
          run_id: str = "") -> RunOptions:
    """与 `server.app.create_run` 保持同一份字段映射。

    刻意不抽公共函数：那边是"开跑"，这边是"只拆"，两处参数若将来分化
    （比如拆解不需要 painted），各留一份比强行共用一个更清楚。

    `audio` 与 `duration` 走关键字参数，是因为它们**不是原样透传**：
    前者要把 token 换成服务端路径，后者在 MV 模式下要被音频探测值覆盖。
    """
    return RunOptions(
        theme=req.theme,
        duration=duration if duration is not None else req.duration,
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
        audio=audio,
        lyrics=req.lyrics,
        music_role=req.music_role,
        bpm=req.bpm,
        beat_offset=req.beat_offset,
        auto_beats=req.auto_beats,
        run_id=run_id,
    )


def _store_audio(project_dir: Path, source: Path) -> str:
    """把上传音轨复制进项目资产区，返回项目内相对路径。

    上传目录是暂存区，会按 TTL 清理；只把 token 记进 meta 的话，几天后重拍
    就会因为"音轨已被清理"而失败，哪怕项目目录里其实还留着副本。
    """
    assets = project_dir / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    suffix = source.suffix.lower() or ".bin"
    target = assets / f"music{suffix}"
    try:
        shutil.copyfile(source, target)
    except OSError as exc:
        raise HTTPException(
            status_code=500, detail=f"音轨复制到项目失败，请重试（{exc}）"
        ) from exc
    return f"assets/{target.name}"


def _saved_audio_path(project_dir: Path, saved: dict) -> Path | None:
    """项目内音轨优先，回落到上传 token。

    优先项目资产的原因不是性能，而是**可用性**：上传暂存被清理后，项目副本
    仍然能让这份拆解继续拍摄、重拍、只重合成。
    """
    rel = str(saved.get("asset") or "").strip()
    if rel:
        root = project_dir.resolve()
        path = (root / rel).resolve()
        if path.is_file() and path.is_relative_to(root):
            return path
    token = str(saved.get("token") or "").strip()
    if token:
        return resolve_audio(token)
    return None


# ------------------------------------------------------------------ 角色资产
#: 参考图上限。参考图只做素材存档与人工对照，不参与出图，8MB 足够；
#: 再大只会让项目目录悄悄膨胀。
MAX_REFERENCE_BYTES = 8 * 1024 * 1024
_REFERENCE_EXTS = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}
#: 明确的能力说明。参考图能力是各 provider 的属性，这里只负责告诉用户
#: "存了参考图之后它到底会不会影响画面"——当前答案是不会。
REFERENCE_NOTE = (
    "参考图只作项目素材存档与人工对照，当前出图后端不消费它；"
    "跨镜一致性靠角色卡里的「视觉锁定」文字注入提示词，仍需人工验收。"
)


def _subject_of(board: dict, subject_id: str) -> dict:
    for subject in board.get("subjects") or []:
        if str(subject.get("id")) == str(subject_id):
            return subject
    raise HTTPException(status_code=404, detail=f"没有角色 {subject_id}")


def _reference_path(plan_dir: Path, subject: dict) -> Path | None:
    """参考图落盘路径。只接受项目内的相对路径，越界一律当作没有。"""
    rel = str((subject.get("reference") or {}).get("asset") or "").strip()
    if not rel:
        return None
    root = plan_dir.resolve()
    path = (root / rel).resolve()
    if path.is_file() and path.is_relative_to(root):
        return path
    return None


def _inspect_reference(data: bytes) -> tuple[str, int, int, str]:
    """校验并识别参考图，返回 (sha256, 宽, 高, 扩展名)。

    只做"能不能当图片用"的硬检查，不评判好不好看——参考图是给人对照的，
    审美判断由人工复核完成。
    """
    import io

    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as im:
            fmt = str(im.format or "").upper()
            im.load()
            width, height = im.size
    except Exception as exc:  # noqa: BLE001 —— 损坏文件、未知格式都在这一层
        raise HTTPException(status_code=422, detail=f"参考图无法解码：{str(exc)[:120]}") from exc
    ext = _REFERENCE_EXTS.get(fmt)
    if not ext:
        raise HTTPException(status_code=422, detail="参考图只支持 PNG / JPEG / WebP")
    if width < 64 or height < 64:
        raise HTTPException(
            status_code=422, detail=f"参考图太小（{width}x{height}），至少 64x64"
        )
    return hashlib.sha256(data).hexdigest(), width, height, ext


def _prune_subject_assets(plan_dir: Path) -> None:
    """删掉当前版本与所有编辑版本都不再引用的参考图。

    文件名按内容哈希寻址，替换参考图时旧文件先留着——否则恢复到旧版本时
    角色卡会指向一张已被删除的图。清理以"所有版本引用"为界，不会误删。
    """
    root = plan_dir / "assets" / "subjects"
    if not root.is_dir():
        return
    sources = [plan_dir / "storyboard.json"]
    versions = plan_dir / "versions"
    if versions.is_dir():
        sources += [p / "storyboard.json" for p in versions.iterdir() if p.is_dir()]
    keep: set[str] = set()
    for src in sources:
        try:
            board = json.loads(src.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for subject in board.get("subjects") or []:
            rel = str((subject.get("reference") or {}).get("asset") or "").strip()
            if rel:
                keep.add(Path(rel).name)
    try:
        for entry in root.iterdir():
            if entry.is_file() and entry.name not in keep:
                entry.unlink(missing_ok=True)
    except OSError:
        pass


def _resolve_music(req: RunRequest) -> tuple[str, int, dict]:
    """把音轨 token 换成路径，并按需覆盖总时长。返回 (路径, 时长, meta 段)。

    **时长覆盖只对 `song` 生效。** 配乐模式（`bgm`）的时长权威是解说，
    音乐只负责垫底、短了循环铺满——拿一首 3 分钟的歌把 60 秒解说拉长到
    3 分钟，正是 `MusicInput` 文档里点名要避免的事。
    """
    token = req.audio.strip()
    if not token:
        if req.lyrics.strip():
            # 填了歌词却没音轨，基本只有一个成因：前端把 token 弄丢了。
            # 静默忽略会得到一条"歌词没上屏"的片子，比直接拒绝难查得多。
            raise HTTPException(status_code=422, detail="填了歌词但没有音轨，请先上传音频")
        return "", req.duration, {}

    path = resolve_audio(token)
    probed = probe_duration(path)
    duration = req.duration
    if probed > 0 and req.music_role == "song":
        # MV 的立身之本：画面服从音乐。拆解阶段就得按音频长度排分镜——
        # 若按界面上的 15 秒排，拍摄阶段再按 222 秒重分配，3 个镜头撑不满
        # （单镜上限 10 秒），只能靠拉长硬凑，等于没拆。
        duration = max(4, int(round(probed)))

    meta = {
        "token": token,
        "name": str(read_audio_meta(token).get("name") or token),
        "duration": round(probed, 3),
        "role": req.music_role,
        "lyrics": req.lyrics,
        "bpm": req.bpm,
        "beat_offset": req.beat_offset,
        "auto_beats": req.auto_beats,
    }
    return str(path), duration, meta


# ------------------------------------------------------------------ 拆
@router.post("/plan")
def create_plan(req: RunRequest,
                principal: Principal = Depends(require_principal)) -> dict:
    """同步走完简报→剧本→分镜。一次大模型往返，通常 5–20 秒。

    **先分配 ID、先登记归属，再生成**。以前 run_id 由管线在工作线程里生成，
    归属只能等成功后补写；一旦生成失败/取消，或补写失败，这份拆解就是无主的
    ——任何能访问本机服务的人都能先拿它去拍，归属就记到别人名下了。
    """
    if not req.theme.strip():
        raise HTTPException(status_code=422, detail="主题不能为空")
    audio, duration, music = _resolve_music(req)

    run_id, project_dir = allocate_run_id(req.theme)
    try:
        record_owner(run_id, principal)
    except OwnerConflict as exc:
        shutil.rmtree(project_dir, ignore_errors=True)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OwnershipUnavailable as exc:
        shutil.rmtree(project_dir, ignore_errors=True)
        raise HTTPException(
            status_code=503, detail="归属登记失败，已取消创建，请稍后重试"
        ) from exc

    # 失败时保留目录：run.jsonl 与已写出的部分拆解是排查材料，而且归属已登记，
    # 不会因为"没生成完"就变成公共数据。
    result = Engine().plan(_opts(req, audio=audio, duration=duration, run_id=run_id))

    # 音轨信息必须跟着拆解一起落盘。否则用户从拆解页点「开始拍摄」时，
    # `web/src/App.tsx` 的 onRender 只传 run_id，拍出来是一条无声的片子——
    # 而且不会有任何报错，因为"没有音轨"本来就是合法状态。
    if music:
        with _project_lock(run_id):
            # 先落项目资产，再写 meta：meta 一旦引用了 asset，就必须保证它存在。
            if audio:
                music["asset"] = _store_audio(project_dir, Path(audio))
            plan_dir = project_dir / "plan"
            if plan_dir.is_dir():
                meta = _read(plan_dir / "meta.json")
                meta["music"] = music
                _write(plan_dir / "meta.json", meta)
    plan_dir = project_dir / "plan"
    if plan_dir.is_dir():
        _archive_version(plan_dir)
    return result


@router.get("/plan/{run_id}")
def read_plan(run_id: str,
              principal: Principal | None = Depends(optional_principal)) -> dict:
    plan_dir = _plan_dir(run_id, principal)

    def load(name: str) -> dict:
        return _read(plan_dir / name)

    board = load("storyboard.json")
    meta = load("meta.json")
    shots = board.get("shots", [])
    # 哪些镜头已有正式片段。只读目录、不读片段内容，代价可以忽略；
    # 界面据此说清"只重合成会复用几镜"，而不是让用户猜画面会不会变。
    clips_dir = plan_dir.parent / "work" / "clips"
    cached = [
        int(s.get("id", 0)) for s in shots
        if (clips_dir / f"shot{int(s.get('id', 0)):02d}.mp4").is_file()
    ]
    return {
        "run_id": run_id,
        "project": board.get("project", {}),
        "script": load("script.json"),
        "subjects": board.get("subjects", []),
        "shots": shots,
        "clips": {"available": cached},
        "meta": meta,
        # 音轨信息（没有就是空字典）。拆解页据此显示「音轨 海阔天空.mp3 · 222.4s · MV」，
        # 这是用户从创作台跳过来之后唯一能确认"MV 还在"的地方。
        "music": meta.get("music") or {},
    }


@router.get("/palettes")
def list_palettes() -> dict:
    """可用色板预设。只影响绘制配色，不改题材与文案。"""
    return {"items": palettes.catalog()}


@router.put("/plan/{run_id}/palette")
def set_palette(run_id: str, body: PaletteBody,
                principal: Principal | None = Depends(optional_principal)) -> dict:
    """保存项目色板。

    色板属于"这一版怎么拍"，所以递增项目版本并留编辑版本；但它不改变分镜。
    已拍片段的指纹含色板 id，改色板后单镜重拍/只重合成会明确拒绝，必须整片
    重拍——否则会出现"改了配色但成片还是旧色"的静默错误。
    """
    with _project_lock(run_id):
        plan_dir = _plan_dir(run_id, principal)
        meta = _read(plan_dir / "meta.json")
        _check_revision(meta, body.expected_revision)
        meta["palette"] = body.palette
        profile = dict(meta.get("render_profile") or {})
        profile["palette"] = body.palette
        meta["render_profile"] = profile
        meta["revision"] = _revision_of(meta) + 1
        meta["edited_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _write(plan_dir / "meta.json", meta)
        _archive_version(plan_dir)
    return {
        "run_id": run_id,
        "palette": body.palette,
        "revision": meta["revision"],
        "note": "色板已保存。需要整片重拍才会应用到画面；只重合成不会改配色。",
    }


@router.get("/plan/{run_id}/versions")
def list_versions(run_id: str,
                  principal: Principal | None = Depends(optional_principal)) -> dict:
    """可恢复的编辑版本。每次改剧本/改镜/重排/重拆都会留一份。"""
    plan_dir = _plan_dir(run_id, principal)
    current = _revision_of(_read(plan_dir / "meta.json"))
    items: list[dict] = []
    root = plan_dir / "versions"
    if root.is_dir():
        for path in root.iterdir():
            if not path.is_dir() or not path.name.startswith("rev-"):
                continue
            try:
                revision = int(path.name.split("-", 1)[1])
            except ValueError:
                continue
            meta = _read(path / "meta.json")
            items.append({
                "revision": revision,
                "at": str(meta.get("edited_at") or meta.get("created") or ""),
                "edited": meta.get("edited") or {},
                "restored_from": meta.get("restored_from"),
            })
    items.sort(key=lambda item: item["revision"], reverse=True)
    return {"current": current, "versions": items}


@router.post("/plan/{run_id}/versions/{revision}/restore")
def restore_version(run_id: str, revision: int,
                    principal: Principal | None = Depends(optional_principal)) -> dict:
    """恢复到某个编辑版本。

    只恢复剧本/分镜/简报；音轨、渲染配置继续用当前值。恢复后的内容还没拍过，
    所以 `rendered` 置回 False——不能让「只重合成」拿旧片段配新分镜。
    """
    if revision < 0:
        raise HTTPException(status_code=422, detail="版本号不能为负")
    with _project_lock(run_id):
        plan_dir = _plan_dir(run_id, principal)
        source = plan_dir / "versions" / f"rev-{revision:06d}"
        if not source.is_dir():
            raise HTTPException(status_code=404, detail=f"没有版本 {revision}")
        current = _read(plan_dir / "meta.json")
        target = _read(source / "meta.json")

        for name in ("brief.json", "script.json", "storyboard.json"):
            file = source / name
            if file.is_file():
                _write(plan_dir / name, _read(file))

        merged = dict(current)
        merged["edited"] = target.get("edited") or {"script": False, "shots": []}
        merged["stale_shots"] = target.get("stale_shots") or []
        merged["replanned"] = target.get("replanned") or []
        merged["revision"] = _revision_of(current) + 1
        merged["edited_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        merged["restored_from"] = int(revision)
        merged["rendered"] = False
        _write(plan_dir / "meta.json", merged)
        _archive_version(plan_dir)

        board = _read(plan_dir / "storyboard.json")
        script = _read(plan_dir / "script.json")

    return {
        "run_id": run_id,
        "project": board.get("project", {}),
        "script": script,
        "subjects": board.get("subjects", []),
        "shots": board.get("shots", []),
        "meta": merged,
        "music": merged.get("music") or {},
        "note": f"已恢复到版本 {revision}。内容还没拍过，要出片请重新拍摄。",
    }


# ------------------------------------------------------------------ 改
@router.patch("/plan/{run_id}/script")
def patch_script(run_id: str, patch: ScriptPatch,
                 principal: Principal | None = Depends(optional_principal)) -> dict:
    """改剧本。**不重派生分镜**，只把受影响镜号标成 stale。"""
    with _project_lock(run_id):
        plan_dir = _plan_dir(run_id, principal)
        meta = _read(plan_dir / "meta.json")
        _check_revision(meta, patch.expected_revision)

        script = _read(plan_dir / "script.json")
        if not script:
            raise HTTPException(status_code=404, detail="这次运行没有剧本文件")

        changes = patch.model_dump(exclude_none=True)
        if not changes:
            raise HTTPException(status_code=422, detail="没有要改的字段")

        # 哪几场的文字被动了？只有这些场对应的镜头会被标 stale。
        # 这是**粗放**判定：按场次名对齐，不做语义级依赖分析——
        # 做不到准确的事，做了反而误导。
        changed_acts: list[str] = []
        if "acts" in changes:
            old = {str(a.get("name") or ""): a for a in (script.get("acts") or [])}
            new = {str(a.get("name") or ""): a for a in (changes["acts"] or [])}
            changed_acts = [n for n, a in new.items() if old.get(n) != a]
            changed_acts += [n for n in old if n not in new]  # 整场被删

        script.update(changes)
        _write(plan_dir / "script.json", script)

        board = _read(plan_dir / "storyboard.json")
        shots = board.get("shots") or []
        if "narration_full" in changes:
            # 解说词是整片的，任何一句改动都可能落到任意一镜上
            affected = [int(s.get("id", 0)) for s in shots]
        else:
            affected = [int(s.get("id", 0)) for s in shots
                        if str(s.get("act") or "") in changed_acts]

        meta = _touch_meta(plan_dir)
        stale = sorted(set(int(x) for x in (meta.get("stale_shots") or [])) | set(affected))
        meta["stale_shots"] = stale
        _write(plan_dir / "meta.json", meta)
        _archive_version(plan_dir)

    return {
        "script": script,
        "stale_shots": affected,
        "meta": meta,
        "note": (
            f"剧本已保存。{len(affected)} 个镜头可能需要重拆（{affected}），"
            "但没有自动改它们——改好的分镜被系统冲掉比不支持编辑更糟。"
            "要重拆就在左侧那一场点「重拆这一场」。"
        ) if affected else "剧本已保存。这次改动没有波及任何已拆分的镜头。",
    }


@router.patch("/plan/{run_id}/shots/{shot_id}")
def patch_shot(run_id: str, shot_id: int, patch: ShotPatch,
               principal: Principal | None = Depends(optional_principal)) -> dict:
    """改单镜。时长越界当场拒绝，并说清是哪一镜、超了多少。

    整段读改写都在项目锁里：两个镜头同时修改时，后到的请求必须看到先到的结果，
    否则后写的那份会把另一份的修改整个覆盖掉（探针已复现）。
    """
    with _project_lock(run_id):
        plan_dir = _plan_dir(run_id, principal)
        meta = _read(plan_dir / "meta.json")
        _check_revision(meta, patch.expected_revision)

        board = _read(plan_dir / "storyboard.json")
        shots = board.get("shots") or []
        target = next((s for s in shots if int(s.get("id", -1)) == shot_id), None)
        if target is None:
            raise HTTPException(status_code=404, detail=f"没有镜 {shot_id}")

        changes = patch.model_dump(exclude_none=True)
        if not changes:
            raise HTTPException(status_code=422, detail="没有要改的字段")

        if "duration" in changes:
            d = float(changes["duration"])
            if d < MIN_SHOT_SECONDS - 1e-6 or d > MAX_SHOT_SECONDS + 1e-6:
                raise HTTPException(
                    status_code=422,
                    detail=(f"镜 {shot_id} 时长 {d:g}s 超出 "
                            f"[{MIN_SHOT_SECONDS:g}, {MAX_SHOT_SECONDS:g}] 秒范围"),
                )

        target.update(changes)
        total, moved = _resync(board)
        _write(plan_dir / "storyboard.json", board)
        meta = _touch_meta(plan_dir, shot_id=shot_id)
        _archive_version(plan_dir)

    return {
        "shot": target,
        "total_duration": total,
        "moved": moved,
        "meta": meta,
        "note": (f"镜 {shot_id} 已保存，总时长变为 {total:.2f}s；"
                 f"{len(moved)} 个镜头的起点跟着后移" if moved
                 else f"镜 {shot_id} 已保存，总时长 {total:.2f}s"),
    }


# ------------------------------------------------------------------ 角色卡
@router.patch("/plan/{run_id}/subjects/{subject_id}")
def patch_subject(run_id: str, subject_id: str, patch: SubjectPatch,
                  principal: Principal | None = Depends(optional_principal)) -> dict:
    """改角色卡（名称 / 视觉锁定）。

    **不重写任何镜头的提示词**：锁定在出图时拼接（`Pipeline._locked_prompt`），
    原稿始终是用户/模型写的那一份。已经出过的画面不会自动变——要换画面
    只能单镜重拍，这条必须说清楚。
    """
    with _project_lock(run_id):
        plan_dir = _plan_dir(run_id, principal)
        meta = _read(plan_dir / "meta.json")
        _check_revision(meta, patch.expected_revision)

        changes = patch.model_dump(exclude_none=True)
        if not changes:
            raise HTTPException(status_code=422, detail="没有要改的字段")

        board = _read(plan_dir / "storyboard.json")
        subject = _subject_of(board, subject_id)
        subject.update(changes)
        _write(plan_dir / "storyboard.json", board)
        meta = _touch_meta(plan_dir, subject_id=subject_id)
        _archive_version(plan_dir)

        affected = [
            int(s.get("id", 0)) for s in (board.get("shots") or [])
            if str(subject_id) in {str(r) for r in (s.get("subject_refs") or [])}
        ]

    note = f"角色「{subject.get('name') or subject_id}」已保存。"
    if affected:
        note += (
            f"引用它的 {len(affected)} 个镜头（{affected}）下次出图会带上新的视觉锁定；"
            "已经拍好的画面不会自动更新，要换画面请单镜重拍。"
        )
    else:
        note += "目前没有镜头引用这个角色。"
    return {
        "subject": subject,
        "affected_shots": affected,
        "meta": meta,
        "note": note,
        "capability": REFERENCE_NOTE,
    }


@router.post("/plan/{run_id}/subjects/{subject_id}/reference")
def upload_subject_reference(run_id: str, subject_id: str,
                             file: UploadFile = File(...),
                             expected_revision: int | None = Query(default=None),
                             principal: Principal | None = Depends(optional_principal)) -> dict:
    """上传角色参考图。只存档，不喂给出图后端。

    存成内容哈希文件名（`assets/subjects/<sha16>.<ext>`）：同一张图重复上传
    不重复占盘，替换参考图时旧文件也先留着，恢复旧版本还能看到当时的图。
    """
    data = file.file.read(MAX_REFERENCE_BYTES + 1)
    if not data:
        raise HTTPException(status_code=422, detail="上传的是空文件")
    if len(data) > MAX_REFERENCE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"参考图超过 {MAX_REFERENCE_BYTES // (1024 * 1024)} MB 上限",
        )
    sha, width, height, ext = _inspect_reference(data)

    with _project_lock(run_id):
        plan_dir = _plan_dir(run_id, principal)
        meta = _read(plan_dir / "meta.json")
        _check_revision(meta, expected_revision)

        board = _read(plan_dir / "storyboard.json")
        subject = _subject_of(board, subject_id)

        asset_dir = plan_dir / "assets" / "subjects"
        asset_dir.mkdir(parents=True, exist_ok=True)
        target = asset_dir / f"{sha[:16]}.{ext}"
        if not target.is_file():
            tmp = asset_dir / f".{target.name}.{time.time_ns()}.part"
            try:
                tmp.write_bytes(data)
                tmp.replace(target)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise

        subject["reference"] = {
            "asset": f"assets/subjects/{target.name}",
            "sha256": sha,
            "width": width,
            "height": height,
            "bytes": len(data),
            "name": Path(file.filename or "reference").name[:80],
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        _write(plan_dir / "storyboard.json", board)
        meta = _touch_meta(plan_dir, subject_id=subject_id)
        _archive_version(plan_dir)
        _prune_subject_assets(plan_dir)

    return {
        "subject": subject,
        "meta": meta,
        "note": "参考图已存档。" + REFERENCE_NOTE,
        "capability": REFERENCE_NOTE,
    }


@router.delete("/plan/{run_id}/subjects/{subject_id}/reference")
def delete_subject_reference(run_id: str, subject_id: str,
                             expected_revision: int | None = Query(default=None),
                             principal: Principal | None = Depends(optional_principal)) -> dict:
    """移除角色参考图。文件不立即删除：旧版本可能还引用着它。"""
    with _project_lock(run_id):
        plan_dir = _plan_dir(run_id, principal)
        meta = _read(plan_dir / "meta.json")
        _check_revision(meta, expected_revision)

        board = _read(plan_dir / "storyboard.json")
        subject = _subject_of(board, subject_id)
        had = bool(subject.pop("reference", None))
        _write(plan_dir / "storyboard.json", board)
        meta = _touch_meta(plan_dir, subject_id=subject_id)
        _archive_version(plan_dir)
        _prune_subject_assets(plan_dir)

    return {
        "subject": subject,
        "meta": meta,
        "note": "参考图已移除。" if had else "这个角色本来就没有参考图。",
        "capability": REFERENCE_NOTE,
    }


@router.get("/plan/{run_id}/subjects/{subject_id}/reference")
def read_subject_reference(run_id: str, subject_id: str,
                           principal: Principal | None = Depends(optional_principal_media)):
    """读取角色参考图。`<img>` 带不了请求头，所以走媒体主体（`?token=`/`?guest=`）。"""
    plan_dir = _plan_dir(run_id, principal)
    board = _read(plan_dir / "storyboard.json")
    subject = _subject_of(board, subject_id)
    path = _reference_path(plan_dir, subject)
    if path is None:
        raise HTTPException(status_code=404, detail="这个角色没有参考图（或文件已被清理）")
    return FileResponse(path)


# ------------------------------------------------------------------ 重拆
@router.post("/plan/{run_id}/acts/{act_name}/replan")
def replan_act(run_id: str, act_name: str, req: ReplanRequest | None = None,
               principal: Principal | None = Depends(optional_principal)) -> dict:
    """按场重拆：只重拆这一场，其余场次的镜头一字不动。

    **判定规则是粗放的**：剧本里改的是第 N 场，就只重拆第 N 场，不做语义级
    依赖分析。做不到准确的事，做了只会误导——但"同场的镜头"这个粒度已经
    覆盖了绝大多数实际改动。
    """
    req = req or ReplanRequest()
    # 重拆是一次长调用（模型往返数秒），整段都在项目锁里：期间到达的镜长/文字
    # 修改会排队，不会出现"重拆基于旧分镜、却覆盖了刚改好的镜头"。
    with _project_lock(run_id):
        plan_dir = _plan_dir(run_id, principal)
        meta = _read(plan_dir / "meta.json")
        _check_revision(meta, req.expected_revision)

        script = _read(plan_dir / "script.json")
        board = _read(plan_dir / "storyboard.json")
        names = [str(a.get("name") or "") for a in (script.get("acts") or [])]
        if act_name not in names:
            raise HTTPException(
                status_code=404,
                detail=f"剧本里没有「{act_name}」这一场。现有场次："
                       f"{'、'.join(n for n in names if n) or '（无）'}",
            )

        edited = [int(x) for x in ((meta.get("edited") or {}).get("shots") or [])]
        old_ids = [int(s.get("id", 0)) for s in (board.get("shots") or [])
                   if str(s.get("act") or "") == act_name]
        clash = [i for i in old_ids if i in edited]
        # 先问一次，不默认覆盖：这一场里那些时长、画面、台词是人逐镜调出来的，
        # 重拆等于把它们换成模型新写的。冲掉用户的工作比"不支持重拆"更糟。
        if clash and not req.force:
            raise HTTPException(
                status_code=409,
                detail=(f"这一场有 {len(clash)} 个镜头是你改过的（{clash}）。"
                        "重拆会把它们换成模型重新写的镜头，改动不会保留。"
                        "确认要重拆就再提交一次，带上 force。"),
            )

        brief = _read(plan_dir / "brief.json")
        opts = RunOptions(
            theme=str(brief.get("theme") or run_id),
            duration=int(brief.get("total_duration") or 15),
            aspect=str(brief.get("aspect_ratio") or "16:9"),
            style=str(brief.get("style") or ""),
            # 用这份拆解当初的 live 档：离线拆出来的分镜，重拆也走离线，
            # 否则用户会看到"只重拆一场却用上了真实模型"这种不一致。
            live=bool(meta.get("live")),
            plan_dir=str(plan_dir),
            run_id=run_id,
        )
        try:
            result = Engine().replan_act(opts, act_name, force=req.force)
        except RuntimeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        storyboard = result.get("storyboard") or {}
        # 重拆换的是这一场的镜头；角色卡是用户资产，不能被模型新返回的
        # subjects 顺手冲掉。同 id 的参考图一定保留；名称/锁定只在用户
        # 手改过时才保留——没改过的字段让模型按新剧本更新是合理的。
        old_subjects = {str(s.get("id")): s for s in (board.get("subjects") or [])}
        edited_subjects = {str(x) for x in ((meta.get("edited") or {}).get("subjects") or [])}
        for sub in storyboard.get("subjects") or []:
            sid = str(sub.get("id"))
            old = old_subjects.get(sid)
            if not old:
                continue
            if old.get("reference"):
                sub["reference"] = old["reference"]
            if sid in edited_subjects:
                if "name" in old:
                    sub["name"] = old["name"]
                if "visual_lock" in old:
                    sub["visual_lock"] = old["visual_lock"]
        _write(plan_dir / "storyboard.json", storyboard)

        # 镜号整体可能顺移，`edited` / `stale` 存的都是镜号，必须跟着映射，
        # 否则"改过"的徽章会标到别的镜头上——而徽章错了比没有更糟。
        id_map = {int(k): int(v) for k, v in (result.get("id_map") or {}).items()}
        edited_after = sorted({id_map[i] for i in edited if i in id_map})
        stale_before = [int(x) for x in (meta.get("stale_shots") or [])]
        # 这一场刚重拆过，它名下的 stale 自然消解；别的场的 stale 继续留着。
        stale_after = sorted({id_map[i] for i in stale_before
                              if i in id_map and i not in old_ids})
        meta["edited"] = {**(meta.get("edited") or {}), "shots": edited_after}
        meta["edited"].setdefault("script", False)
        meta["stale_shots"] = stale_after
        history = list(meta.get("replanned") or [])
        history.append({
            "act": act_name,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "before": len(old_ids),
            "after": len(result.get("shots") or []),
        })
        meta["replanned"] = history[-20:]
        meta["edited_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        meta["revision"] = _revision_of(meta) + 1
        _write(plan_dir / "meta.json", meta)
        _archive_version(plan_dir)

        before_n, after_n = len(old_ids), len(result.get("shots") or [])
        total = float(result.get("total_duration") or 0.0)
        old_total = round(sum(float(s.get("duration") or 0)
                              for s in (board.get("shots") or [])), 3)
        dropped = [i for i in edited if i in old_ids]
        if before_n == 0:
            # 原本没镜头的场：补出来的时长是从剧本的 time_range 读的，全片会变长。
            # 这必须明说，否则用户会以为"重拆不该改总时长"这条被打破了。
            note = (f"「{act_name}」原来没有镜头，现在补了 {after_n} 个"
                    f"（{float(result.get('target') or 0):.1f}s），"
                    f"全片时长 {old_total:.2f}s → {total:.2f}s。其余场次没有改动。")
        else:
            note = (
                f"「{act_name}」已重拆：{before_n} 镜 → {after_n} 镜，"
                f"其余场次没有改动，全片总时长 {total:.2f}s。"
            )
            if dropped:
                note += f"其中 {len(dropped)} 个你改过的镜头（{dropped}）被新镜头替换了。"

    return {
        "act": act_name,
        "storyboard": storyboard,
        "shots": result.get("shots") or [],
        "total_duration": total,
        "old_ids": old_ids,
        "stale_shots": stale_after,
        "meta": meta,
        "note": note,
    }


@router.post("/plan/{run_id}/recompose", status_code=202)
def recompose_plan(run_id: str, req: RenderRequest | None = None,
                   principal: Principal | None = Depends(optional_principal)) -> dict:
    """只重合成：不重新出图，按现在的镜长与顺序把已有片段重拼一遍。

    这是时间轴编辑的落地口。改镜长、挪顺序都不该让画面重画一次——一条 222 秒的
    MV 有 45 镜，改一次等一小时，那"能编辑时间轴"就只是能改数字而已。

    与 `/shots/{id}/render` 的分界：那条是"这一镜的画面要重画"，这条是
    "画面全都不动，只改怎么拼"。两条路的代价差一个数量级，不能合成一条。
    """
    req = req or RenderRequest()
    if req.painted:
        # 代码绘制整段编译成一条视频，没有逐镜片段可重拼。在起任务前拒掉，
        # 否则用户等半天才在 `_recompose` 里看到"片段不在 work/clips"——
        # 那句话像是在说文件丢了，实际是这条路不适用。
        raise HTTPException(
            status_code=422,
            detail="代码绘制模式的产物是一整段视频，没有逐镜片段可重拼，"
                   "不支持只重合成；请用「开始拍摄」整片重拍",
        )
    return _start_render(run_id, req, principal, recompose=True)


@router.post("/plan/{run_id}/shots/reorder")
def reorder_shots(run_id: str, req: ReorderRequest,
                  principal: Principal | None = Depends(optional_principal)) -> dict:
    """重排镜头顺序。只改顺序，不改镜号、不改时长、不动任何文件。

    为什么不让前端改 `id` 来实现重排：镜号是 `work/clips/shotNN.mp4` 的文件名、
    是 `meta.edited.shots` 里"哪些被改过"的记录、也是单镜重拍路径上的那一节。
    改 id 等于把这三处一起改坏，而且改坏了不报错。顺序单独存，彼此不相干。
    """
    with _project_lock(run_id):
        plan_dir = _plan_dir(run_id, principal)
        meta0 = _read(plan_dir / "meta.json")
        _check_revision(meta0, req.expected_revision)

        board = _read(plan_dir / "storyboard.json")
        shots = board.get("shots") or []
        if not shots:
            raise HTTPException(status_code=422, detail="分镜为空，没有可排的镜头")

        ids = [int(s.get("id", 0)) for s in shots]
        want = [int(x) for x in req.order]
        if sorted(want) != sorted(ids):
            # 只接受**完整置换**。允许缺项就等于允许"悄悄删镜头"，那是最难查的一类
            # 数据丢失：成片少了两镜，日志上一切正常。
            raise HTTPException(
                status_code=422,
                detail=f"顺序必须是现有全部镜号的一个排列。现有 {ids}，收到 {want}",
            )

        by_id = {int(s.get("id", 0)): s for s in shots}
        board["shots"] = [by_id[i] for i in want]
        total, _moved = _resync(board)
        _write(plan_dir / "storyboard.json", board)
        meta = _touch_meta(plan_dir)
        meta["reordered_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _write(plan_dir / "meta.json", meta)
        _archive_version(plan_dir)

    return {
        "shots": board["shots"],
        "order": want,
        "total_duration": total,
        "meta": meta,
        "note": (f"镜头顺序已改为 {' → '.join(str(i) for i in want)}，"
                 f"总时长 {total:.2f}s 不变。要看到成片变化请点「只重合成」"),
    }


# ------------------------------------------------------------------ 字幕轨
def _subtitle_duration(board: dict) -> float:
    return float((board.get("project") or {}).get("total_duration") or 0.0)


def _validate_subtitles(lines: list[dict], duration: float) -> None:
    """字幕轨校验：顺序、重叠、越界。

    单独抽出来是因为"保存时放过一次坏数据"会一直坏到成片里：SRT 重叠在
    播放器上表现为字幕闪烁，越界则让最后一句停在黑屏上。
    """
    prev_end = -1.0
    for index, line in enumerate(lines, start=1):
        start = float(line.get("start") or 0.0)
        end = float(line.get("end") or 0.0)
        text = str(line.get("text") or "").strip()
        if not text:
            raise HTTPException(status_code=422, detail=f"第 {index} 行字幕为空")
        if end <= start:
            raise HTTPException(
                status_code=422, detail=f"第 {index} 行结束时间必须大于开始时间"
            )
        if duration > 0 and end > duration + 0.05:
            raise HTTPException(
                status_code=422,
                detail=f"第 {index} 行结束于 {end:.2f}s，超出成片时长 {duration:.2f}s",
            )
        if start < prev_end - 0.01:
            raise HTTPException(
                status_code=422, detail=f"第 {index} 行与上一行重叠（上一行到 {prev_end:.2f}s）"
            )
        prev_end = end


def _plan_subtitles(plan_dir: Path, meta: dict, board: dict) -> dict:
    """当前生效的字幕轨：人工编辑 > 成片 SRT > 按分镜推导。"""
    duration = _subtitle_duration(board)
    revision = _revision_of(meta)
    mode = _audio_edit(meta)["subtitle_mode"]
    saved = meta.get("subtitle_lines")
    if isinstance(saved, list):
        return {"source": "edited", "lines": saved, "duration": duration,
                "revision": revision, "subtitle_mode": mode,
                "note": "这是你保存的字幕轨，重合成后生效"}

    srt = plan_dir.parent / "output" / "subtitles.srt"
    if srt.is_file():
        try:
            parsed = parse_srt(srt.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            parsed = []
        if parsed:
            # 成片里的 SRT 已经叠加过项目整体偏移；编辑界面必须显示**原始时间**，
            # 否则保存后再重合成会把偏移叠加两次。
            offset = _audio_edit(meta)["subtitle_offset"]
            lines = []
            for line in parsed:
                start = max(0.0, round(line["start"] - offset, 3))
                end = round(line["end"] - offset, 3)
                if end > start:
                    lines.append({"start": start, "end": end, "text": line["text"]})
            return {"source": "rendered", "lines": lines, "duration": duration,
                    "revision": revision, "subtitle_mode": mode,
                    "note": "读取自上次成片字幕；整体偏移已从时间中扣除"}

    lines = []
    for shot in board.get("shots") or []:
        text = str(shot.get("caption") or shot.get("narration") or "").strip()
        if not text:
            continue
        lines.append({
            "start": round(float(shot.get("start") or 0.0), 3),
            "end": round(float(shot.get("start") or 0.0) + float(shot.get("duration") or 0.0), 3),
            "text": text,
        })
    note = ""
    if str((meta.get("music") or {}).get("role") or "") == "song":
        note = "MV 模式的上屏歌词在拍摄后才生成，建议先拍一次再编辑字幕"
    return {"source": "plan", "lines": lines, "duration": duration,
            "revision": revision, "subtitle_mode": mode, "note": note}


@router.get("/plan/{run_id}/subtitles")
def list_subtitles(run_id: str,
                   principal: Principal | None = Depends(optional_principal)) -> dict:
    plan_dir = _plan_dir(run_id, principal)
    return _plan_subtitles(plan_dir, _read(plan_dir / "meta.json"),
                           _read(plan_dir / "storyboard.json"))


@router.put("/plan/{run_id}/subtitles")
def save_subtitles(run_id: str, body: SubtitlesBody,
                   principal: Principal | None = Depends(optional_principal)) -> dict:
    """保存整条字幕轨。`lines=None` 恢复自动生成，`[]` 表示不要字幕。"""
    with _project_lock(run_id):
        plan_dir = _plan_dir(run_id, principal)
        meta = _read(plan_dir / "meta.json")
        _check_revision(meta, body.expected_revision)
        board = _read(plan_dir / "storyboard.json")

        if body.lines is None:
            meta.pop("subtitle_lines", None)
            note = "已恢复为自动生成字幕；重合成后生效"
        else:
            lines = [
                {"start": round(float(line.start), 3),
                 "end": round(float(line.end), 3),
                 "text": line.text.strip()}
                for line in body.lines
            ]
            _validate_subtitles(lines, _subtitle_duration(board))
            meta["subtitle_lines"] = lines
            note = f"已保存 {len(lines)} 行字幕；重合成后生效"

        mode = body.subtitle_mode
        if mode is not None:
            edit = _audio_edit(meta)
            edit["subtitle_mode"] = mode
            meta["audio_edit"] = edit
            note += {
                "burn": "；烧录进画面",
                "sidecar": "；只出外置字幕，不烧录",
                "none": "；关闭字幕",
            }[mode]

        meta["revision"] = _revision_of(meta) + 1
        meta["subtitle_edited_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _write(plan_dir / "meta.json", meta)
        _archive_version(plan_dir)

    return {
        "source": "edited" if body.lines is not None else "auto",
        "lines": None if body.lines is None else meta["subtitle_lines"],
        "revision": meta["revision"],
        "subtitle_mode": _audio_edit(meta)["subtitle_mode"],
        "note": note,
    }


@router.get("/plan/{run_id}/subtitle-style")
def get_subtitle_style(run_id: str,
                       principal: Principal | None = Depends(optional_principal)) -> dict:
    """当前字幕样式 + 6 套预置 + 按现有字幕算出的缺字情况。"""
    plan_dir = _plan_dir(run_id, principal)
    meta = _read(plan_dir / "meta.json")
    board = _read(plan_dir / "storyboard.json")
    return {
        "current": _subtitle_style_state(meta),
        "presets": sstyle.style_catalog(),
        "revision": _revision_of(meta),
        "coverage": _subtitle_style_coverage(plan_dir, meta, board),
    }


@router.put("/plan/{run_id}/subtitle-style")
def save_subtitle_style(run_id: str, body: SubtitleStyleBody,
                        principal: Principal | None = Depends(optional_principal)) -> dict:
    """换一套整句字幕样式。只重合成，不重出图；绘制后端不受此设置影响。"""
    preset = str(body.preset or "").strip()
    try:
        style = sstyle.style_of(preset)
    except KeyError as exc:
        options = "、".join(item["id"] for item in sstyle.style_catalog())
        raise HTTPException(
            status_code=422, detail=f"没有这个字幕样式（可选：{options}）"
        ) from exc

    with _project_lock(run_id):
        plan_dir = _plan_dir(run_id, principal)
        meta = _read(plan_dir / "meta.json")
        _check_revision(meta, body.expected_revision)
        board = _read(plan_dir / "storyboard.json")
        saved = meta.get("subtitle_style") or {}
        if str(saved.get("preset") or "") == style.id:
            return {
                "current": _subtitle_style_state(meta),
                "revision": _revision_of(meta),
                "coverage": _subtitle_style_coverage(plan_dir, meta, board),
                "note": f"已经是「{style.label}」，无需重复保存",
            }
        version = int(saved.get("version") or 0) + 1
        meta["subtitle_style"] = {
            "preset": style.id,
            "version": version,
            "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        meta["revision"] = _revision_of(meta) + 1
        _write(plan_dir / "meta.json", meta)
        _archive_version(plan_dir)
        revision = meta["revision"]
        current = _subtitle_style_state(meta)
        coverage = _subtitle_style_coverage(plan_dir, meta, board)

    return {
        "current": current,
        "revision": revision,
        "coverage": coverage,
        "note": f"已换成「{style.label}」；点「只重合成」后生效，不重新出图。",
    }


# ------------------------------------------------------------------ 波形
@router.get("/plan/{run_id}/waveform")
def plan_waveform(run_id: str, buckets: int = 1200,
                  principal: Principal | None = Depends(optional_principal)) -> dict:
    """项目音轨的峰值包络，供字幕轨按波形校准。

    只读接口，不加项目锁：缓存写入是"临时文件 + 原子替换"，
    两个人同时打开页面最多各算一遍，不会读写到半截文件。
    """
    project_dir = _safe(run_id, principal)
    plan_dir = _plan_dir(run_id, principal)
    meta = _read(plan_dir / "meta.json")
    music = meta.get("music") or {}
    path = _saved_audio_path(project_dir, music)
    if path is None:
        raise HTTPException(
            status_code=404,
            detail="这次拆解没有可用的音轨，波形无从谈起；MV/配乐项目才有音轨",
        )
    try:
        data = waveform_peaks(path, project_dir / "work" / "waveform", buckets)
    except Exception as exc:  # noqa: BLE001 —— 解码失败、超时、磁盘错误都归到这里
        raise HTTPException(status_code=422, detail=f"读不出音轨波形：{str(exc)[:200]}") from exc
    return {
        **data,
        "name": str(music.get("name") or path.name),
        "role": str(music.get("role") or ""),
    }


# ------------------------------------------------------------------ 拍
@router.post("/plan/{run_id}/render", status_code=202)
def render_plan(run_id: str, req: RenderRequest | None = None,
                principal: Principal | None = Depends(optional_principal)) -> dict:
    """按这份拆解出片。走与"一次跑完"相同的逐镜闭环与合成。"""
    return _start_render(run_id, req or RenderRequest(), principal, only_shot=0)


@router.post("/plan/{run_id}/preview", status_code=202)
def preview_plan(run_id: str, req: RenderRequest | None = None,
                 principal: Principal | None = Depends(optional_principal)) -> dict:
    """低清预览：半分辨率、绘制帧率封顶 12，产物落在 runs/<id>/preview/。

    与正式拍摄的分界是**产物与状态**：预览不覆盖 output/final.mp4、不写
    work/clips（单镜重拍复用），也不把项目标记为已拍。它只回答"时间轴、字幕、
    音轨对不对"，不回答"画质够不够"——后者必须看正式成片。
    """
    return _start_render(run_id, req or RenderRequest(), principal, preview=True)


@router.post("/plan/{run_id}/shots/{shot_id}/render", status_code=202)
def render_shot(run_id: str, shot_id: int, req: RenderRequest | None = None,
                principal: Principal | None = Depends(optional_principal)) -> dict:
    """只重拍一镜，其余镜头复用上次的片段，然后整片重新合成。

    **不重新拆解、不重新配音判断**：拍什么由 `storyboard.json` 决定，
    这里只是把"哪一镜要重拍"传下去（`RunOptions.only_shot`）。

    为什么要单开一条接口而不是给 `/render` 加个参数：`/render` 的语义是
    "按这份拆解出整片"，加个可选参数会让它的返回与副作用随参数变化，
    而前端只有一处调用（`App.tsx` 的 onRender），改签名要动不该动的地方。
    """
    req = req or RenderRequest()
    if req.painted:
        # 代码绘制按整段分镜编译，没有"只重画一镜"的入口。让它跑下去是把整段
        # 重画一遍，与"只重拍一镜"正相反——在起任务之前就拒掉。
        raise HTTPException(
            status_code=422,
            detail="代码绘制模式不支持单镜重拍（按整段分镜编译），请用「开始拍摄」整片重拍",
        )
    if shot_id <= 0:
        raise HTTPException(status_code=422, detail="镜号必须从 1 开始")
    return _start_render(run_id, req, principal, only_shot=shot_id)


@router.post("/plan/{run_id}/shots/{shot_id}/preview", status_code=202)
def preview_shot(run_id: str, shot_id: int, req: RenderRequest | None = None,
                 principal: Principal | None = Depends(optional_principal)) -> dict:
    """单镜低清预览：只出这一镜的片段，不重合成、不改正式产物。

    与「重拍这一镜」的区别：重拍会拿新片段和其余旧片段重拼成片（要等合成），
    预览只看这一镜现在长什么样，几秒到几十秒就够，正式成片与片段缓存都不动。
    """
    req = req or RenderRequest()
    if shot_id <= 0:
        raise HTTPException(status_code=422, detail="镜号必须从 1 开始")
    if req.painted:
        raise HTTPException(
            status_code=422,
            detail="代码绘制模式不支持单镜预览（按整段分镜编译），请用整片低清预览",
        )
    return _start_render(run_id, req, principal, only_shot=shot_id, preview=True)


def _render_profile(meta: dict, req: RenderRequest) -> tuple[dict, bool]:
    """解析本次拍摄真正使用的配置，返回 (配置, 是否有显式覆盖)。

    规则：**显式传了才覆盖**，其余继承项目保存的 `render_profile`。
    以前 `RenderRequest` 默认 `live=True`，从拆解页拍摄（只传 run_id）会把
    一条离线拆解悄悄拍成真实模型——用户的选择在换页之后就被默认值盖掉了。
    """
    saved = meta.get("render_profile") or {}
    profile = {
        # 老项目没有 render_profile，只有拆解时的 meta.live。用它兜底，
        # 而不是退回接口默认值——否则一份真实模型拆出来的分镜会按离线去拍。
        "live": bool(saved.get("live", meta.get("live", False))),
        "painted": bool(saved.get("painted", False)),
        "painted_fps": int(saved.get("painted_fps", 24) or 24),
        "painted_quality": str(saved.get("painted_quality") or "medium"),
        "clean_frames": bool(saved.get("clean_frames", False)),
        # 色板只影响配色，但属于"这一版怎么拍"的可见配置，随快照一起冻结。
        "palette": str(saved.get("palette") or meta.get("palette") or "auto"),
    }
    explicit = False
    for field in ("live", "painted", "painted_fps", "painted_quality",
                  "clean_frames", "palette"):
        value = getattr(req, field)
        if value is not None:
            profile[field] = value
            explicit = True
    return profile, explicit


def _audio_edit(meta: dict) -> dict:
    """本次混音/字幕/转场设置：项目保存值 → 接口显式覆盖（调用方再做）。

    这些值不改变分镜内容，但会改变成片声音、字幕或镜头衔接，所以和渲染配置
    一样必须随项目保存，否则"我只重合成"会把上次的调整全部忘掉。
    """
    saved = meta.get("audio_edit") or {}
    mode = str(saved.get("subtitle_mode") or meta.get("subtitle_mode") or "burn").lower()
    if mode not in ("burn", "sidecar", "none"):
        mode = "burn"
    kind = str(saved.get("transition") or meta.get("transition") or "none").lower()
    if kind not in TRANSITIONS:
        kind = "none"
    return {
        "voice_gain": float(saved.get("voice_gain", 1.0)),
        "music_gain": float(saved.get("music_gain", BGM_GAIN)),
        # 旧项目没有这个字段：默认关，行为和以前完全一致。
        "voice_duck": bool(saved.get("voice_duck", False)),
        "subtitle_offset": float(saved.get("subtitle_offset", 0.0)),
        "subtitle_mode": mode,
        "transition": kind,
        "text_anim": bool(saved.get("text_anim", False)),
    }


def _subtitle_style_state(meta: dict) -> dict:
    """项目当前字幕样式。未知/旧项目一律回退默认，不因一条坏配置拒绝拍摄。"""
    saved = meta.get("subtitle_style") or {}
    try:
        style = sstyle.style_of(str(saved.get("preset") or ""))
    except KeyError:
        style = sstyle.STYLES[sstyle.DEFAULT_STYLE_ID]
    payload = sstyle.style_payload(style, int(saved.get("version") or 0))
    payload["updated"] = str(saved.get("updated") or "")
    return payload


def _subtitle_style_coverage(plan_dir: Path, meta: dict, board: dict) -> dict:
    """按当前生效字幕检查样式字体缺字。没有字幕轨时返回 ok。"""
    try:
        lines = _plan_subtitles(plan_dir, meta, board).get("lines") or []
        texts = [str(line.get("text") or "") for line in lines]
        return sstyle.coverage(texts, _style_object(meta))
    except Exception:  # noqa: BLE001 —— 缺字检查失败不能拦住样式保存
        return {"ok": True, "missing": [], "missing_count": 0,
                "fallback": "", "uncovered": []}


def _style_object(meta: dict) -> sstyle.SubtitleStyle:
    try:
        return sstyle.style_of(_subtitle_style_state(meta)["id"])
    except KeyError:
        return sstyle.STYLES[sstyle.DEFAULT_STYLE_ID]


def _freeze_plan(plan_dir: Path, revision: int, profile: dict,
                 audio_edit: dict | None = None,
                 snapshot_id: str = "",
                 subtitle_style: dict | None = None) -> Path:
    """把这一版拆解与渲染配置冻结成**不可变**快照。

    目录以唯一 snapshot_id 命名，绝不复用 `rev-000000`：同一个 revision 下
    两次不同覆盖项的拍摄请求如果共用一个目录，后一次即使最终被 Registry
    拒绝，也已经把前一次任务的输入改写了。
    """
    sid = str(snapshot_id or uuid.uuid4().hex[:12])
    snap = plan_dir / "snapshots" / f"snap-{sid}"
    snap.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, object] = {
        "snapshot_id": sid,
        "revision": max(0, int(revision)),
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "files": {},
        "profile": profile,
    }
    for name in PLAN_FILES:
        src = plan_dir / name
        if src.is_file():
            target = snap / name
            shutil.copyfile(src, target)
            manifest["files"][name] = hashlib.sha256(target.read_bytes()).hexdigest()
    _write(snap / "render_profile.json", profile)
    manifest["files"]["render_profile.json"] = hashlib.sha256(
        (snap / "render_profile.json").read_bytes()
    ).hexdigest()
    if audio_edit is not None:
        # 混音/字幕设置也属于"这一版怎么拍"，快照里留一份便于复现。
        _write(snap / "audio_edit.json", audio_edit)
        manifest["files"]["audio_edit.json"] = hashlib.sha256(
            (snap / "audio_edit.json").read_bytes()
        ).hexdigest()
    if subtitle_style is not None:
        # 字幕样式不进片段缓存，但决定最终叠加的字幕卡；快照里固定一份，
        # 复查时能说清"这条成片用的是哪套样式"。
        _write(snap / "subtitle_style.json", subtitle_style)
        manifest["files"]["subtitle_style.json"] = hashlib.sha256(
            (snap / "subtitle_style.json").read_bytes()
        ).hexdigest()
    _write(snap / "snapshot.json", manifest)

    # 只清理无任务引用的旧快照。原来只按最近 10 个目录清理，长任务运行期间
    # 连续发起多次拍摄时，可能把活动任务正在读取的快照删掉。
    active_ids = (
        REGISTRY.active_snapshot_ids()
        if hasattr(REGISTRY, "active_snapshot_ids") else set()
    )
    protected = {f"snap-{sid}"} | {f"snap-{value}" for value in active_ids}
    try:
        versions = sorted(
            (p for p in (plan_dir / "snapshots").iterdir() if p.is_dir()),
            key=lambda p: p.stat().st_mtime,
        )
        stale = [p for p in versions if p.name not in protected]
        for old in stale[:-10]:
            shutil.rmtree(old, ignore_errors=True)
    except OSError:
        pass
    return snap


def _reserve_slot() -> str | None:
    """预留执行槽位。测试替身没有该方法时退回旧行为（不做预留）。"""
    reserve = getattr(REGISTRY, "reserve", None)
    return reserve() if callable(reserve) else None


def _release_slot(token: str | None) -> None:
    if token:
        release = getattr(REGISTRY, "release", None)
        if callable(release):
            release(token)


def _start_render(run_id: str, req: RenderRequest, principal: Principal | None,
                  *, only_shot: int = 0, recompose: bool = False,
                  preview: bool = False) -> dict:
    """三个拍摄入口的公共前半段：校验 → 冻结版本 → 解析音轨 → 起任务。

    快照必须在项目锁里做：校验、冻结、登记渲染状态之间若插进一次编辑，任务拿到的
    就是"校验过的旧分镜 + 冻结时的新分镜"这种自相矛盾的输入。
    """
    if preview and recompose:
        # 预览不重拼整片；与"只重合成"叠加会得到"把半分辨率预览当成正式成片"的
        # 结果。单镜预览是另一条明确的路（只出一镜片段），允许只带 only_shot。
        raise HTTPException(
            status_code=422, detail="低清预览不能与只重合成叠加"
        )
    with _project_lock(run_id):
        plan_dir = _plan_dir(run_id, principal)
        meta = _read(plan_dir / "meta.json")
        _check_revision(meta, req.expected_revision)
        revision = _revision_of(meta)

        # 拍之前先拦一次越界：用户可能直接改了 JSON 文件，绕过了 PATCH 的校验。
        # 越界不静默修正——他要的 12 秒被偷偷改成 10 秒，比直接报错更糟。
        board = _read(plan_dir / "storyboard.json")
        if not board.get("shots"):
            raise HTTPException(status_code=422, detail="分镜为空，无法拍摄")
        bad = _shot_bounds(board)
        if bad:
            raise HTTPException(
                status_code=422,
                detail="分镜校验未通过，请先修正再拍：\n" + "\n".join(bad),
            )

        if only_shot:
            ids = [int(s.get("id", 0)) for s in (board.get("shots") or [])]
            if only_shot not in ids:
                # 422 而不是 404：路径本身存在，是"这个镜号在分镜里没有"——
                # 前端据此提示"这一镜可能被删了，刷新一下"。
                raise HTTPException(
                    status_code=422,
                    detail=f"分镜里没有镜 {only_shot}（现有 {ids}），可能刚被改过，请刷新",
                )

        profile, explicit = _render_profile(meta, req)
        audio_edit = _audio_edit(meta)
        audio_explicit = False
        for field in ("voice_gain", "music_gain", "subtitle_offset"):
            value = getattr(req, field)
            if value is not None:
                audio_edit[field] = float(value)
                audio_explicit = True
        if req.voice_duck is not None:
            audio_edit["voice_duck"] = bool(req.voice_duck)
            audio_explicit = True
        if req.transition is not None:
            audio_edit["transition"] = str(req.transition)
            audio_explicit = True
        if req.text_anim is not None:
            audio_edit["text_anim"] = bool(req.text_anim)
            audio_explicit = True
        if req.subtitle_mode is not None:
            audio_edit["subtitle_mode"] = req.subtitle_mode
            audio_explicit = True
        if recompose and profile["painted"]:
            raise HTTPException(
                status_code=422,
                detail="代码绘制模式的产物是一整段视频，没有逐镜片段可重拼，"
                       "不支持只重合成；请用「开始拍摄」整片重拍",
            )
        if only_shot and profile["painted"]:
            raise HTTPException(
                status_code=422,
                detail="代码绘制模式不支持单镜重拍（按整段分镜编译），"
                       "请用「开始拍摄」整片重拍",
            )

        if recompose:
            # 只重合成的前提是"这些镜头都拍过"。在这里逐个查片段，是为了**当场**给出
            # 缺哪几镜，而不是起一条任务、等它跑进 `_recompose` 才报"片段不在
            # work/clips"——那时前端已经切到进度页，用户只会看到一句像是在说文件丢了的
            # 错误，还得自己猜是哪一镜。
            if not meta.get("rendered"):
                raise HTTPException(
                    status_code=422,
                    detail="这份拆解还没拍过，没有片段可以重拼。请先用「开始拍摄」拍一次",
                )
            clips_dir = plan_dir.parent / "work" / "clips"
            absent = [int(s.get("id", 0)) for s in (board.get("shots") or [])
                      if not (clips_dir / f"shot{int(s.get('id', 0)):02d}.mp4").is_file()]
            if absent:
                raise HTTPException(
                    status_code=422,
                    detail=f"镜 {absent} 的片段不在 work/clips 里，没法只重合成。"
                           f"work 目录可能被清理过，请用「开始拍摄」整片重拍",
                )

        brief = _read(plan_dir / "brief.json")
        theme = str(brief.get("theme") or run_id)

        # 整片正式拍摄才做提交前预算闸：单镜重拍、只重合成、预览都复用已有片段
        # 或不走视频 API，拿整片估价拦它们是误伤。云端视频未接入时估价为 0。
        if not only_shot:
            guard_budget(
                int(brief.get("total_duration") or 15),
                painted=bool(profile["painted"]),
                preview=preview,
                recompose=recompose,
            )

        # 音轨：请求里没带就回落到拆解时存下的那一份。
        #
        # 为什么回落而不是"没传就是没有"：拆解页的「开始拍摄」只带 run_id
        # （`web/src/App.tsx` 的 onRender），没有第二个地方能重新填歌词和节拍。
        # 不回落的话，凡是从拆解页拍的 MV 都是无声的——而且不报错，
        # 因为"没有音轨"本来就是合法状态，这是最难查的一类缺陷。
        saved = meta.get("music") or {}
        audio_path: Path | None = None
        if req.audio.strip():
            audio_path = resolve_audio(req.audio.strip())
            lyrics = req.lyrics
            role = req.music_role
            bpm, offset, auto = req.bpm, req.beat_offset, req.auto_beats
        else:
            # 整组都取存档值：只取 token 而让 role 用请求默认值（"song"），
            # 会把一份配乐模式的拆解悄悄拍成 MV。
            lyrics = str(saved.get("lyrics") or "")
            role = str(saved.get("role") or "song")
            bpm = float(saved.get("bpm") or 0.0)
            offset = float(saved.get("beat_offset") or 0.0)
            auto = bool(saved.get("auto_beats", True))
            # 项目内副本优先：上传暂存被清理后，重拍仍然可用。
            audio_path = _saved_audio_path(plan_dir.parent, saved)

        # 先预留执行槽位，再冻结快照。以前顺序相反：忙碌检查在 `REGISTRY.create`
        # 里，而那时快照已经覆盖写完——被 409 拒绝的请求也能改写活动任务的输入。
        try:
            reservation = _reserve_slot()
        except BusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        snapshot_id = uuid.uuid4().hex[:12]
        style_state = _subtitle_style_state(meta)
        try:
            snapshot = _freeze_plan(plan_dir, revision, profile, audio_edit, snapshot_id,
                                    subtitle_style=style_state)
            opts = RunOptions(
                theme=theme,
                duration=int(brief.get("total_duration") or 15),
                aspect=str(brief.get("aspect_ratio") or "16:9"),
                style=str(brief.get("style") or ""),
                palette=str(profile.get("palette") or "auto"),
                live=bool(profile["live"]),
                painted=bool(profile["painted"]),
                painted_fps=int(profile["painted_fps"]),
                painted_quality=str(profile["painted_quality"]),
                clean_frames=bool(profile["clean_frames"]),
                audio=str(audio_path) if audio_path else "",
                lyrics=lyrics,
                music_role="bgm" if role == "bgm" else "song",
                bpm=bpm,
                beat_offset=offset,
                auto_beats=auto,
                # 拍的是冻结快照，不是正在被编辑的 plan 目录。
                plan_dir=str(snapshot),
                run_id=run_id,
                snapshot_id=snapshot_id,
                only_shot=only_shot,
                recompose=recompose,
                preview=preview,
                voice_gain=audio_edit["voice_gain"],
                music_gain=audio_edit["music_gain"],
                voice_duck=bool(audio_edit.get("voice_duck", False)),
                subtitle_offset=audio_edit["subtitle_offset"],
                subtitle_mode=str(audio_edit.get("subtitle_mode") or "burn"),
                subtitle_style=str(style_state["id"]),
                transition=str(audio_edit.get("transition") or "none"),
                text_anim=bool(audio_edit.get("text_anim", False)),
                # None = 自动生成；列表 = 用户保存的字幕轨（空列表 = 明确不要字幕）。
                subtitle_lines=(list(meta.get("subtitle_lines"))
                                if isinstance(meta.get("subtitle_lines"), list) else None),
            )
        except BaseException:
            _release_slot(reservation)
            raise

    try:
        record_owner(run_id, principal)
    except OwnerConflict as exc:
        _release_slot(reservation)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OwnershipUnavailable as exc:
        _release_slot(reservation)
        raise HTTPException(
            status_code=503, detail="归属登记失败，已拒绝起任务，请稍后重试"
        ) from exc

    try:
        kind = (
            "preview_shot" if preview and only_shot
            else "preview" if preview
            else "recompose" if recompose
            else "shot" if only_shot
            else "plan"
        )
        if reservation is None:
            job = REGISTRY.create(theme, opts, principal=principal, kind=kind)
        else:
            job = REGISTRY.create(theme, opts, principal=principal, kind=kind,
                                  reservation=reservation)
    except BusyError as exc:
        _release_slot(reservation)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except BaseException:
        _release_slot(reservation)
        raise

    # 起任务成功之后才写渲染标记：被 Registry 拒掉（409 忙碌）的那次不该
    # 把项目标成"已拍过"——否则成片墙会显示"拍过但没成片"。
    with _project_lock(run_id):
        meta = _read(plan_dir / "meta.json")
        if preview:
            # 预览不是正式拍摄：不动 rendered / last_render_profile，
            # 否则成片墙会多出一条"已拍"记录，单镜重拍还会去复用预览片段。
            meta["last_preview_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            meta["last_preview_revision"] = revision
            if only_shot:
                meta["last_shot_preview"] = {
                    "shot": only_shot,
                    "at": meta["last_preview_at"],
                    "revision": revision,
                }
        else:
            meta["rendered"] = True
            meta["rendered_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            meta["last_revision"] = revision
            meta["last_render_profile"] = profile
            if explicit:
                # 用户明确改过的配置回写项目：下次拍摄沿用，不再被旧默认值盖掉。
                meta["render_profile"] = profile
            if only_shot:
                meta["last_single_shot"] = {
                    "shot": only_shot,
                    "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
            if recompose:
                meta["last_recompose"] = {"at": time.strftime("%Y-%m-%d %H:%M:%S")}
            if audio_explicit:
                # 混音/字幕设置随正式拍摄或重合成保存；预览不写项目配置。
                meta["audio_edit"] = audio_edit
        _write(plan_dir / "meta.json", meta)

    return {"job_id": job.job_id, "run_id": run_id, "theme": theme,
            "running": True, "only_shot": only_shot, "recompose": recompose,
            "preview": preview, "snapshot_id": snapshot_id,
            "revision": revision, "render_profile": profile,
            "audio_edit": audio_edit}
