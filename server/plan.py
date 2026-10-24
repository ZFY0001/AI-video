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

import json
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from app.engine import Engine, RunOptions
from pipeline.config import ROOT
from pipeline.models import MAX_SHOT_SECONDS, MIN_SHOT_SECONDS
from pipeline.video import probe_duration

from .auth import optional_user, record_run_owner
# 可见性规则只写一份：拆解文件与成片是同一份数据的两个阶段，
# 成片墙挡住的东西拆解页也必须挡住，否则换个入口就能绕过。
from .history import _can_view
from .jobs import BusyError
from .media import read_audio_meta, resolve_audio
from .registry import REGISTRY
from .schemas import (
    ReorderRequest,
    ReplanRequest,
    RenderRequest,
    RunRequest,
    ScriptPatch,
    ShotPatch,
)

router = APIRouter(prefix="/api")

PLAN_FILES = ("brief.json", "script.json", "storyboard.json", "meta.json")


def _safe(run_id: str, user_id: int | None = None) -> Path:
    if not run_id or run_id in (".", "..") or "/" in run_id or "\\" in run_id:
        raise HTTPException(status_code=400, detail="非法的运行 ID")
    root = (ROOT / "runs").resolve()
    path = (root / run_id).resolve()
    if path.parent != root or not path.is_dir():
        raise HTTPException(status_code=404, detail="运行不存在")
    if not _can_view(run_id, user_id):
        raise HTTPException(status_code=404, detail="运行不存在")
    return path


def _plan_dir(run_id: str, user_id: int | None = None) -> Path:
    plan_dir = _safe(run_id, user_id) / "plan"
    if not plan_dir.is_dir():
        raise HTTPException(status_code=404, detail="这次运行没有拆解信息")
    return plan_dir


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"拆解文件损坏：{path.name}（{exc}）") from exc


def _write(path: Path, payload: dict) -> None:
    """原子写：先落临时文件再替换。

    拆解文件是拍摄阶段的唯一输入，写到一半被打断就等于这次拆解作废。
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _touch_meta(plan_dir: Path, *, shot_id: int | None = None) -> dict:
    """记下"哪些是你改过的"。

    没有这个字段，重跑之后就分不清"这是模型的判断还是我的修改"——
    再次拆解会覆盖文件，人的工作痕迹就彻底没了。
    """
    meta = _read(plan_dir / "meta.json")
    edited = meta.setdefault("edited", {})
    edited.setdefault("script", False)
    edited.setdefault("shots", [])
    if shot_id is None:
        edited["script"] = True
    else:
        if shot_id not in edited["shots"]:
            edited["shots"].append(int(shot_id))
    meta["edited_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _write(plan_dir / "meta.json", meta)
    return meta


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


def _opts(req: RunRequest, *, audio: str = "", duration: int | None = None) -> RunOptions:
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
    )


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
                user: dict | None = Depends(optional_user)) -> dict:
    """同步走完简报→剧本→分镜。一次大模型往返，通常 5–20 秒。

    归属不在这里写：run_id 由管线自己生成，和 `POST /runs` 一样只能在
    完成之后落库（见 `Job._record_owner`）。
    """
    if not req.theme.strip():
        raise HTTPException(status_code=422, detail="主题不能为空")
    audio, duration, music = _resolve_music(req)
    result = Engine().plan(_opts(req, audio=audio, duration=duration))
    run_id = str(result.get("run_id") or "")

    # 音轨信息必须跟着拆解一起落盘。否则用户从拆解页点「开始拍摄」时，
    # `web/src/App.tsx` 的 onRender 只传 run_id，拍出来是一条无声的片子——
    # 而且不会有任何报错，因为"没有音轨"本来就是合法状态。
    if music and run_id:
        plan_dir = ROOT / "runs" / run_id / "plan"
        if plan_dir.is_dir():
            meta = _read(plan_dir / "meta.json")
            meta["music"] = music
            _write(plan_dir / "meta.json", meta)

    # 拆解阶段就记归属：否则一份还没拍的拆解是"公共"的，
    # 别人可以先拿它拍一条，归属就记到别人名下了。
    if user:
        record_run_owner(run_id, int(user["id"]))
    return result


@router.get("/plan/{run_id}")
def read_plan(run_id: str,
              user: dict | None = Depends(optional_user)) -> dict:
    plan_dir = _plan_dir(run_id, int(user["id"]) if user else None)

    def load(name: str) -> dict:
        return _read(plan_dir / name)

    board = load("storyboard.json")
    meta = load("meta.json")
    return {
        "run_id": run_id,
        "project": board.get("project", {}),
        "script": load("script.json"),
        "subjects": board.get("subjects", []),
        "shots": board.get("shots", []),
        "meta": meta,
        # 音轨信息（没有就是空字典）。拆解页据此显示「音轨 海阔天空.mp3 · 222.4s · MV」，
        # 这是用户从创作台跳过来之后唯一能确认"MV 还在"的地方。
        "music": meta.get("music") or {},
    }


# ------------------------------------------------------------------ 改
@router.patch("/plan/{run_id}/script")
def patch_script(run_id: str, patch: ScriptPatch,
                 user: dict | None = Depends(optional_user)) -> dict:
    """改剧本。**不重派生分镜**，只把受影响镜号标成 stale。"""
    plan_dir = _plan_dir(run_id, int(user["id"]) if user else None)
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
               user: dict | None = Depends(optional_user)) -> dict:
    """改单镜。时长越界当场拒绝，并说清是哪一镜、超了多少。"""
    plan_dir = _plan_dir(run_id, int(user["id"]) if user else None)
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

    return {
        "shot": target,
        "total_duration": total,
        "moved": moved,
        "meta": meta,
        "note": (f"镜 {shot_id} 已保存，总时长变为 {total:.2f}s；"
                 f"{len(moved)} 个镜头的起点跟着后移" if moved
                 else f"镜 {shot_id} 已保存，总时长 {total:.2f}s"),
    }


# ------------------------------------------------------------------ 重拆
@router.post("/plan/{run_id}/acts/{act_name}/replan")
def replan_act(run_id: str, act_name: str, req: ReplanRequest | None = None,
               user: dict | None = Depends(optional_user)) -> dict:
    """按场重拆：只重拆这一场，其余场次的镜头一字不动。

    **判定规则是粗放的**：剧本里改的是第 N 场，就只重拆第 N 场，不做语义级
    依赖分析。做不到准确的事，做了只会误导——但"同场的镜头"这个粒度已经
    覆盖了绝大多数实际改动。
    """
    plan_dir = _plan_dir(run_id, int(user["id"]) if user else None)
    req = req or ReplanRequest()

    script = _read(plan_dir / "script.json")
    board = _read(plan_dir / "storyboard.json")
    names = [str(a.get("name") or "") for a in (script.get("acts") or [])]
    if act_name not in names:
        raise HTTPException(
            status_code=404,
            detail=f"剧本里没有「{act_name}」这一场。现有场次："
                   f"{'、'.join(n for n in names if n) or '（无）'}",
        )

    meta = _read(plan_dir / "meta.json")
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
    )
    try:
        result = Engine().replan_act(opts, act_name, force=req.force)
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    storyboard = result.get("storyboard") or {}
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
    _write(plan_dir / "meta.json", meta)

    before_n, after_n = len(old_ids), len(result.get("shots") or [])
    total = float(result.get("total_duration") or 0.0)
    old_total = round(sum(float(s.get("duration") or 0) for s in (board.get("shots") or [])), 3)
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
                   user: dict | None = Depends(optional_user)) -> dict:
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
    return _start_render(run_id, req, user, recompose=True)


@router.post("/plan/{run_id}/shots/reorder")
def reorder_shots(run_id: str, req: ReorderRequest,
                  user: dict | None = Depends(optional_user)) -> dict:
    """重排镜头顺序。只改顺序，不改镜号、不改时长、不动任何文件。

    为什么不让前端改 `id` 来实现重排：镜号是 `work/clips/shotNN.mp4` 的文件名、
    是 `meta.edited.shots` 里"哪些被改过"的记录、也是单镜重拍路径上的那一节。
    改 id 等于把这三处一起改坏，而且改坏了不报错。顺序单独存，彼此不相干。
    """
    plan_dir = _plan_dir(run_id, int(user["id"]) if user else None)
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

    return {
        "shots": board["shots"],
        "order": want,
        "total_duration": total,
        "meta": meta,
        "note": (f"镜头顺序已改为 {' → '.join(str(i) for i in want)}，"
                 f"总时长 {total:.2f}s 不变。要看到成片变化请点「只重合成」"),
    }


# ------------------------------------------------------------------ 拍
@router.post("/plan/{run_id}/render", status_code=202)
def render_plan(run_id: str, req: RenderRequest | None = None,
                user: dict | None = Depends(optional_user)) -> dict:
    """按这份拆解出片。走与"一次跑完"相同的逐镜闭环与合成。"""
    return _start_render(run_id, req or RenderRequest(), user, only_shot=0)


@router.post("/plan/{run_id}/shots/{shot_id}/render", status_code=202)
def render_shot(run_id: str, shot_id: int, req: RenderRequest | None = None,
                user: dict | None = Depends(optional_user)) -> dict:
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
    return _start_render(run_id, req, user, only_shot=shot_id)


def _start_render(run_id: str, req: RenderRequest, user: dict | None,
                  *, only_shot: int = 0, recompose: bool = False) -> dict:
    """三个拍摄入口的公共前半段：校验 → 解析音轨 → 起任务。"""
    plan_dir = _plan_dir(run_id, int(user["id"]) if user else None)

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

    if recompose:
        # 只重合成的前提是"这些镜头都拍过"。在这里逐个查片段，是为了**当场**给出
        # 缺哪几镜，而不是起一条任务、等它跑进 `_recompose` 才报"片段不在
        # work/clips"——那时前端已经切到进度页，用户只会看到一句像是在说文件丢了的
        # 错误，还得自己猜是哪一镜。
        meta0 = _read(plan_dir / "meta.json")
        if not meta0.get("rendered"):
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

    # 记下"这份拆解已经进过拍摄"。成片墙靠这个标记区分「只拆没拍」与「拍失败」。
    meta = _read(plan_dir / "meta.json")
    meta["rendered"] = True
    meta["rendered_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    if only_shot:
        meta["last_single_shot"] = {
            "shot": only_shot,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    if recompose:
        meta["last_recompose"] = {"at": time.strftime("%Y-%m-%d %H:%M:%S")}
    _write(plan_dir / "meta.json", meta)

    # 音轨：请求里没带就回落到拆解时存下的那一份。
    #
    # 为什么回落而不是"没传就是没有"：拆解页的「开始拍摄」只带 run_id
    # （`web/src/App.tsx` 的 onRender），没有第二个地方能重新填歌词和节拍。
    # 不回落的话，凡是从拆解页拍的 MV 都是无声的——而且不报错，
    # 因为"没有音轨"本来就是合法状态，这是最难查的一类缺陷。
    saved = meta.get("music") or {}
    if req.audio.strip():
        audio_token = req.audio.strip()
        lyrics = req.lyrics
        role = req.music_role
        bpm, offset, auto = req.bpm, req.beat_offset, req.auto_beats
    else:
        # 整组都取存档值：只取 token 而让 role 用请求默认值（"song"），
        # 会把一份配乐模式的拆解悄悄拍成 MV。
        audio_token = str(saved.get("token") or "")
        lyrics = str(saved.get("lyrics") or "")
        role = str(saved.get("role") or "song")
        bpm = float(saved.get("bpm") or 0.0)
        offset = float(saved.get("beat_offset") or 0.0)
        auto = bool(saved.get("auto_beats", True))

    opts = RunOptions(
        theme=theme,
        duration=int(brief.get("total_duration") or 15),
        aspect=str(brief.get("aspect_ratio") or "16:9"),
        style=str(brief.get("style") or ""),
        live=req.live,
        painted=req.painted,
        painted_fps=req.painted_fps,
        painted_quality=req.painted_quality,
        clean_frames=req.clean_frames,
        audio=str(resolve_audio(audio_token)) if audio_token else "",
        lyrics=lyrics,
        music_role="bgm" if role == "bgm" else "song",
        bpm=bpm,
        beat_offset=offset,
        auto_beats=auto,
        plan_dir=str(plan_dir),
        only_shot=only_shot,
        recompose=recompose,
    )
    try:
        job = REGISTRY.create(theme, opts,
                              owner_id=int(user["id"]) if user else 0)
    except BusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"job_id": job.job_id, "run_id": run_id, "theme": theme,
            "running": True, "only_shot": only_shot, "recompose": recompose}
