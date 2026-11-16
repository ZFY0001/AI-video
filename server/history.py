"""成片墙与环境设置的数据接口。

只做「读磁盘 / 写 .env」两件事，业务逻辑仍在 `app.engine` 与 `pipeline/`。
所有路径都先过 `_safe()`，只允许 `runs/<一层目录>`，防止被构造成任意读。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from pipeline.config import ROOT, load_env_file
from pipeline.tts import srt_to_lrc

from . import artifacts
from .auth import (
    OwnershipUnavailable,
    Principal,
    optional_principal,
    optional_principal_media,
    require_principal,
    require_user,
    run_owner_key,
)
from .media import video_response

router = APIRouter(prefix="/api")

RUNS = ROOT / "runs"
TRASH = RUNS / "_trash"
ENV_PATH = ROOT / ".env"

#: 允许写回的键与类型。**白名单而不是格式检查**：只查"大写字母开头"等于允许
#: 调用方往 .env 里塞任意键，而 .env 会被其它模块读成运行配置。
ENV_WRITABLE: dict[str, str] = {
    "LLM_API_KEY": "secret",
    "LLM_BASE_URL": "url",
    "LLM_MODEL": "text",
    "VLM_MODEL": "text",
    "VLM_BASE_URL": "url",
    "VLM_API_KEY": "secret",
    "JEV_BASE_URL": "url",
    "JEV_API_KEY": "secret",
    "COMFYUI_BASE_URL": "url",
    "VIDEO_API_BASE_URL": "url",
    "VIDEO_API_KEY": "secret",
    "TTS_ENGINE": "text",
    "TTS_BASE_URL": "url",
    "TTS_API_KEY": "secret",
    "BUDGET_CNY": "number",
}

_ENV_MAX_VALUE = 5000


def _validate_env_value(key: str, value: str) -> str:
    kind = ENV_WRITABLE[key]
    text = value.strip()
    if len(text) > _ENV_MAX_VALUE:
        raise HTTPException(status_code=422, detail=f"{key} 的值过长")
    if kind == "url" and text:
        if not re.match(r"^https?://[^\s]+$", text):
            raise HTTPException(
                status_code=422, detail=f"{key} 必须是 http(s) 地址，或留空表示不启用"
            )
    elif kind == "number":
        try:
            number = float(text)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"{key} 必须是数字") from exc
        if not 0 <= number <= 100000:
            raise HTTPException(status_code=422, detail=f"{key} 超出合理范围（0–100000）")
    return text


# ------------------------------------------------------------------ 工具
def _owner_key_of(run_id: str) -> str | None:
    """这个成片归谁。None = 没有归属记录（账号体系上线前的旧数据，视为公共）。

    数据库故障抛 503，而不是返回 None：查询失败不等于"无主"，把两者混为一谈
    会让一次数据库抖动把所有私有项目变成公开的。
    """
    try:
        return run_owner_key(run_id)
    except OwnershipUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="权限校验暂不可用（归属数据库异常），请稍后重试",
        ) from exc


def _can_view(run_id: str, principal: Principal | None) -> bool:
    """可见性规则：无归属记录的旧数据谁都能看；有主的数据只有主人能看。"""
    owner = _owner_key_of(run_id)
    if owner is None:
        return True
    return principal is not None and owner == principal.key


def _safe(run_id: str, principal: Principal | None = None) -> Path:
    if not run_id or run_id in (".", "..") or "/" in run_id or "\\" in run_id:
        raise HTTPException(status_code=400, detail="非法的运行 ID")
    root = RUNS.resolve()
    path = (root / run_id).resolve()
    if path.parent != root or not path.is_dir():
        raise HTTPException(status_code=404, detail="运行不存在")
    # 不存在与无权限都回 404：不泄露"这个 id 是真实存在的别人的作品"
    if not _can_view(run_id, principal):
        raise HTTPException(status_code=404, detail="运行不存在")
    return path


def _storyboard(path: Path) -> dict:
    file = path / "output" / "storyboard.json"
    if not file.is_file():
        return {}
    try:
        return json.loads(file.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _master_view(master: dict | None) -> dict | None:
    """接口只暴露来源信息，不把绝对路径带出去。"""
    if not master:
        return None
    return {
        "artifact_id": master.get("artifact_id") or "",
        "kind": master.get("kind") or "legacy",
        "backend": master.get("backend") or "",
        "sha256": master.get("sha256") or "",
        "created": master.get("created") or 0,
        "job_id": master.get("job_id") or "",
        "snapshot_id": master.get("snapshot_id") or "",
        "legacy": bool(master.get("legacy")),
    }


def _json_file(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _created(run_id: str, path: Path) -> str:
    match = re.match(r"(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})", run_id)
    if match:
        y, mo, d, h, mi = match.groups()
        return f"{y}-{mo}-{d} {h}:{mi}"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime))


def _tags(style: str) -> list[str]:
    return [part.strip() for part in re.split(r"[、，,/]", style) if part.strip()][:3]


def _status(path: Path, has_video: bool) -> str:
    """三态：完成 / 中断 / 失败。

    「中断」靠 run.jsonl 里的「已停止」判断——管线在镜头边界退出时仍会合成
    已完成的部分，所以光看有没有成片区分不出来。
    """
    if not has_video:
        return "failed"
    log = path / "run.jsonl"
    if log.is_file():
        try:
            with log.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if "已停止" in line:
                        return "canceled"
        except Exception:  # noqa: BLE001
            pass
    return "ok"


def _still(path: Path, shot_id: int) -> Path | None:
    """镜头的**选中图**优先，退回过检图与第一张尝试图。

    选中图就是评审证据绑定的那张图；如果这里还按文件名猜，可能出现
    “质检面板展示候选 1、留存画面却是候选 3”的分裂。
    """
    board = _storyboard(path)
    shot = next(
        (s for s in (board.get("shots") or []) if int(s.get("id") or 0) == shot_id),
        None,
    )
    frame = str((shot or {}).get("frame_path") or "")
    if frame and bool((shot or {}).get("frame_is_clean")):
        candidate = Path(frame).resolve()
        if candidate.is_file() and candidate.is_relative_to(path.resolve()):
            return candidate
    frames = path / "work" / "frames"
    for name in (f"shot{shot_id:02d}_clean.png", f"shot{shot_id:02d}_a1.png"):
        file = frames / name
        if file.is_file():
            return file
    return None


def _plan_only(entry: Path) -> bool:
    """只拆解、还没拍摄的记录。

    `ctx.prepare()` 在拆解阶段就会把 output/ 建出来，所以按"有没有目录"判断
    永远为真——只能看标记。三条同时成立才算只拆没拍：

    1. 有 `plan/`；
    2. `meta.rendered` 不是 `True`（拍摄入口会把它置 True，失败也算拍过）；
    3. 没有成片。

    老记录没有 `rendered` 字段，靠第 3 条兜住：真出了片就一定有成片文件。
    """
    if not (entry / "plan").is_dir():
        return False
    if artifacts.current_video(entry) is not None:
        return False
    meta_file = entry / "plan" / "meta.json"
    if not meta_file.is_file():
        return True
    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return True
    return meta.get("rendered") is not True


# ------------------------------------------------------------------ 成片墙

@router.get("/history")
def list_history(limit: int = 60, mine: bool = False,
                 principal: Principal | None = Depends(optional_principal)) -> dict:
    if not RUNS.is_dir():
        return {"items": []}

    items: list[dict[str, Any]] = []
    for entry in sorted(RUNS.iterdir(), reverse=True):
        if not entry.is_dir() or entry.name.startswith("_"):
            continue
        if not _can_view(entry.name, principal):
            continue
        # mine=true 用于「个人中心 · 我的作品」：只保留确实归属本人的。
        # 不加这个开关的话，个人中心会把公共旧数据也列进来，和上面的统计
        # （只算自己的）对不上——同一屏两个数互相矛盾。
        if mine and (principal is None or _owner_key_of(entry.name) != principal.key):
            continue
        # 只拆解、没拍摄的记录不进成片墙：它一个镜头都没出，
        # 列在这里只会被三态判成「失败」——那是错的，它根本没失败，只是还没拍。
        if _plan_only(entry):
            continue
        board = _storyboard(entry)
        project = board.get("project") or {}
        shots = board.get("shots") or []
        master = artifacts.current_master(entry)
        video = Path(master["path"]) if master else None
        items.append(
            {
                "run_id": entry.name,
                "title": project.get("title") or entry.name.split("-", 2)[-1],
                "style": project.get("style", ""),
                "aspect": project.get("aspect_ratio", ""),
                "duration": round(float(project.get("total_duration") or 0), 1),
                "shots": len(shots),
                "size_mb": round(video.stat().st_size / 1048576, 2) if video else 0,
                "has_video": bool(video),
                "status": _status(entry, bool(video)),
                "created": _created(entry.name, entry),
                "tags": _tags(project.get("style", "")),
                "master": _master_view(master),
            }
        )
        if len(items) >= max(1, min(limit, 200)):
            break
    return {"items": items}


# ------------------------------------------------------------------ 项目库
@router.get("/projects")
def list_projects(state: str = "all", limit: int = 100,
                  principal: Principal | None = Depends(optional_principal)) -> dict:
    """草稿与已拍项目的统一入口。

    成片墙只看"有片子"的项目；只拆未拍的草稿在那里会被判成失败并藏起来。
    这里按项目状态列出，草稿可以直接回到拆解页继续改、继续拍。
    """
    if state not in ("all", "draft", "rendered"):
        raise HTTPException(status_code=422, detail="state 只能是 all / draft / rendered")
    if not RUNS.is_dir():
        return {"items": []}

    entries = [e for e in RUNS.iterdir() if e.is_dir() and not e.name.startswith("_")]
    entries.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    items: list[dict[str, Any]] = []
    for entry in entries:
        if not _can_view(entry.name, principal):
            continue
        plan_dir = entry / "plan"
        meta = _json_file(plan_dir / "meta.json")
        board = _json_file(plan_dir / "storyboard.json") or _storyboard(entry)
        brief = _json_file(plan_dir / "brief.json")
        project = board.get("project") or {}
        shots = board.get("shots") or []
        master = artifacts.current_master(entry)
        video = Path(master["path"]) if master else None

        is_draft = _plan_only(entry)
        if is_draft:
            status = "draft"
        elif video is not None:
            status = _status(entry, True)
        else:
            status = "failed"
        # 筛选语义：draft 只看未拍；rendered 看所有拍过的（含失败）。
        # 不能直接比 status，因为拍过的状态是 ok/canceled/failed。
        if state == "draft" and not is_draft:
            continue
        if state == "rendered" and is_draft:
            continue

        duration = sum(float(s.get("duration") or 0) for s in shots)
        if duration <= 0:
            duration = float(project.get("total_duration") or brief.get("total_duration") or 0)
        updated = (
            str(meta.get("edited_at") or "")
            or str(meta.get("rendered_at") or "")
            or time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.stat().st_mtime))
        )
        items.append({
            "run_id": entry.name,
            "title": str(brief.get("theme") or project.get("title") or entry.name.split("-", 2)[-1]),
            "state": status,
            "shots": len(shots),
            "duration": round(duration, 1),
            "revision": int(meta.get("revision") or 0),
            "has_video": video is not None,
            "updated": updated,
            "render_profile": meta.get("render_profile") or {},
            "master": _master_view(master),
        })
        if len(items) >= max(1, min(limit, 200)):
            break
    return {"items": items}


@router.get("/history/{run_id}")
def run_detail(run_id: str,
               principal: Principal | None = Depends(optional_principal)) -> dict:
    path = _safe(run_id, principal)
    board = _storyboard(path)
    project = board.get("project") or {}

    shots = []
    for shot in board.get("shots") or []:
        shots.append(
            {
                "id": shot.get("id"),
                "start": round(float(shot.get("start") or 0), 2),
                "duration": round(float(shot.get("duration") or 0), 2),
                "status": shot.get("status") or "",
                "quality": shot.get("quality"),
                "attempts": int(shot.get("attempts") or 0),
                "caption": shot.get("caption") or "",
                # 质检依据：界面据此区分"模拟评审"与真实视觉评审。
                "review_mode": shot.get("review_mode") or "",
                "visual_check": shot.get("visual_check") or "",
                "review_rule": shot.get("review_rule") or "",
                "evidence_id": shot.get("evidence_id") or "",
                "frame_source": shot.get("frame_source") or "",
            }
        )

    output = path / "output"
    master = artifacts.current_master(path)
    video = Path(master["path"]) if master else None
    return {
        "run_id": run_id,
        "title": project.get("title") or run_id.split("-", 2)[-1],
        "style": project.get("style", ""),
        "aspect": project.get("aspect_ratio", ""),
        "duration": round(float(project.get("total_duration") or 0), 1),
        "created": _created(run_id, path),
        "shots": shots,
        "has_video": video is not None,
        "has_subtitle": (output / "subtitles.srt").is_file(),
        "size_mb": round(video.stat().st_size / 1048576, 2) if video else 0,
        "tags": _tags(project.get("style", "")),
        "master": _master_view(master),
        # 列表页有「中断」，详情抽屉也得有：管线在镜头边界退出时仍会合成
        # 已完成的部分，只看 has_video 会把半条片子当成整条。
        "status": _status(path, video is not None),
    }


# ------------------------------------------------------------------ 质检与人工复核
#: 复核结论是"人看过了"的记录，**不改变已合成画面**。放在 output/review.json，
#: 与渲染产物同级但独立：重跑会覆盖 storyboard，不会抹掉复核意见。
_REVIEW_LOCK = threading.RLock()
_REVIEW_VERDICTS = {"approved", "rejected", "clear"}


class ReviewBody(BaseModel):
    verdict: str = Field(max_length=16)
    note: str = Field(default="", max_length=200)
    #: 界面看到的画面哈希。与服务端当前画面不一致时拒绝写入——否则会把
    #: "看过旧画面"的结论挂到新画面上，这种错误在数据里看不出来。
    image_sha256: str = Field(default="", max_length=64)


def _read_review_payload(path: Path) -> dict:
    payload = _json_file(path / "output" / "review.json")
    shots = payload.get("shots")
    if not isinstance(shots, dict):
        shots = {}
    return {
        "updated": str(payload.get("updated") or ""),
        "shots": {str(k): v for k, v in shots.items() if isinstance(v, dict)},
    }


def _write_review_payload(path: Path, shots: dict) -> None:
    """原子写：先写同目录临时文件再替换。

    直接覆盖时进程中途被杀会留下半截 JSON，下次读失败就表现为"所有复核意见
    都没了"。同目录是为了让 `os.replace` 保持原子性（不能跨盘）。
    """
    out = path / "output"
    out.mkdir(parents=True, exist_ok=True)
    target = out / "review.json"
    fd, name = tempfile.mkstemp(prefix=".review-", suffix=".tmp", dir=str(out))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(
                {"updated": time.strftime("%Y-%m-%d %H:%M:%S"), "shots": shots},
                fh, ensure_ascii=False, indent=2,
            )
        os.replace(name, target)
    except Exception:
        Path(name).unlink(missing_ok=True)
        raise


def _review_shot(shot: dict, manual: dict | None) -> dict:
    sha = str(shot.get("image_sha256") or "")
    tech_recorded = shot.get("tech_ok") is not None
    manual_view = None
    if manual:
        # 画面换过（重拍/重合成前重新出图）之后，旧结论只代表旧画面。
        stale = bool(manual.get("image_sha256") and sha
                     and str(manual["image_sha256"]) != sha)
        manual_view = {
            "verdict": str(manual.get("verdict") or ""),
            "note": str(manual.get("note") or ""),
            "reviewed_by": str(manual.get("reviewed_by") or ""),
            "reviewed_at": str(manual.get("reviewed_at") or ""),
            "stale": stale,
        }
    return {
        "id": shot.get("id"),
        "start": round(float(shot.get("start") or 0), 2),
        "duration": round(float(shot.get("duration") or 0), 2),
        "status": shot.get("status") or "",
        "quality": shot.get("quality"),
        "attempts": int(shot.get("attempts") or 0),
        "defect_type": shot.get("defect_type") or "",
        "review_mode": shot.get("review_mode") or "",
        "visual_check": shot.get("visual_check") or "",
        "review_rule": shot.get("review_rule") or "",
        "evidence_id": shot.get("evidence_id") or "",
        "frame_source": shot.get("frame_source") or "",
        "image_sha256": sha,
        "candidates": [
            {
                "evidence_id": str(item.get("evidence_id") or ""),
                "attempt": int(item.get("attempt") or 0),
                "sha256": str(item.get("sha256") or ""),
                "quality": int(item.get("quality") or 0),
                "verdict": str(item.get("verdict") or ""),
                "review_mode": str(item.get("review_mode") or ""),
                "provider": str(item.get("provider") or ""),
            }
            for item in (shot.get("candidate_evidence") or [])
            if isinstance(item, dict)
        ],
        "technical": {
            "recorded": tech_recorded,
            "ok": bool(shot.get("tech_ok")),
            "reason": str(shot.get("tech_reason") or ""),
            "width": int(shot.get("image_width") or 0),
            "height": int(shot.get("image_height") or 0),
            "mean": float(shot.get("image_mean") or 0.0),
            "contrast": float(shot.get("image_contrast") or 0.0),
        },
        "manual": manual_view,
    }


def _review_summary(shots: list[dict]) -> dict:
    approved = sum(
        1 for s in shots
        if s["manual"] and not s["manual"]["stale"] and s["manual"]["verdict"] == "approved"
    )
    rejected = sum(
        1 for s in shots
        if s["manual"] and not s["manual"]["stale"] and s["manual"]["verdict"] == "rejected"
    )
    stale = sum(1 for s in shots if s["manual"] and s["manual"]["stale"])
    modes: dict[str, int] = {}
    for shot in shots:
        key = shot["review_mode"] or "unrecorded"
        modes[key] = modes.get(key, 0) + 1
    return {
        "total": len(shots),
        "manual_approved": approved,
        "manual_rejected": rejected,
        "pending": max(0, len(shots) - approved - rejected),
        "stale": stale,
        "modes": modes,
        "technical_recorded": sum(1 for s in shots if s["technical"]["recorded"]),
        "technical_failed": sum(
            1 for s in shots if s["technical"]["recorded"] and not s["technical"]["ok"]
        ),
    }


@router.get("/history/{run_id}/review")
def run_review(run_id: str,
               principal: Principal | None = Depends(optional_principal)) -> dict:
    path = _safe(run_id, principal)
    board = _storyboard(path)
    if not board:
        raise HTTPException(status_code=404, detail="这次运行没有可复核的分镜（可能还没拍过）")
    payload = _read_review_payload(path)
    shots = [
        _review_shot(shot, payload["shots"].get(str(shot.get("id"))))
        for shot in board.get("shots") or []
    ]
    return {
        "run_id": run_id,
        "updated": payload["updated"],
        "shots": shots,
        "summary": _review_summary(shots),
    }


@router.post("/history/{run_id}/review/{shot_id}")
def review_shot(run_id: str, shot_id: int, body: ReviewBody,
                principal: Principal = Depends(require_principal)) -> dict:
    path = _safe(run_id, principal)
    board = _storyboard(path)
    shots = board.get("shots") or []
    target = next((s for s in shots if int(s.get("id") or 0) == shot_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="这次运行里没有这一镜")
    verdict = body.verdict.strip().lower()
    if verdict not in _REVIEW_VERDICTS:
        raise HTTPException(status_code=422, detail="结论只能是 approved / rejected / clear")

    sha = str(target.get("image_sha256") or "")
    if verdict != "clear" and body.image_sha256 and sha and body.image_sha256 != sha:
        raise HTTPException(
            status_code=409,
            detail="画面已经变化，旧判断不能沿用到新画面上；请刷新后重新复核",
        )

    with _REVIEW_LOCK:
        payload = _read_review_payload(path)
        manual = payload["shots"]
        if verdict == "clear":
            manual.pop(str(shot_id), None)
        else:
            manual[str(shot_id)] = {
                "verdict": verdict,
                "note": body.note.strip(),
                "image_sha256": sha,
                "reviewed_by": principal.kind,
                "reviewed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        _write_review_payload(path, manual)

    views = [_review_shot(s, manual.get(str(s.get("id")))) for s in shots]
    return {
        "shot": next(v for v in views if int(v.get("id") or 0) == shot_id),
        "summary": _review_summary(views),
        "note": (
            "已清除这一镜的人工结论"
            if verdict == "clear"
            else "已记录人工结论；它不改变已合成画面，需要换画面请重拍该镜"
        ),
    }


@router.get("/history/{run_id}/cover")
def run_cover(run_id: str,
              principal: Principal | None = Depends(optional_principal_media)):
    path = _safe(run_id, principal)
    shots = _storyboard(path).get("shots") or []
    if shots:
        still = _still(path, int(shots[0].get("id") or 1))
        if still:
            return FileResponse(still)
    raise HTTPException(status_code=404, detail="没有可用封面")


@router.get("/history/{run_id}/shots/{shot_id}/still")
def run_shot_still(run_id: str, shot_id: int,
                   principal: Principal | None = Depends(optional_principal_media)):
    """单镜留存画面。质检面板用它叠安全区、对照技术证据。

    只有真正出过图的运行才有这张图；旧记录或已清理帧目录的运行返回 404，
    前端据此显示"没有留存画面"，而不是挂一个破图标。
    """
    path = _safe(run_id, principal)
    still = _still(path, shot_id)
    if still is None:
        raise HTTPException(status_code=404, detail=f"镜 {shot_id} 没有留存画面")
    return FileResponse(still)


@router.get("/history/{run_id}/video")
def run_history_video(run_id: str, request: Request,
                      principal: Principal | None = Depends(optional_principal_media)):
    path = _safe(run_id, principal)
    video = artifacts.current_video(path)
    if video is None:
        raise HTTPException(status_code=404, detail="这次运行没有成片")
    return video_response(video, request)


@router.get("/history/{run_id}/subtitle")
def run_subtitle(run_id: str, format: str = "srt",
                 principal: Principal | None = Depends(optional_principal_media)) -> PlainTextResponse:
    path = _safe(run_id, principal)
    srt = path / "output" / "subtitles.srt"
    if not srt.is_file():
        raise HTTPException(status_code=404, detail="没有字幕文件")
    raw = srt.read_text(encoding="utf-8", errors="replace")
    if format.lower() == "lrc":
        # LRC 从实际成片字幕转出来：导出的东西必须和片子对得上，
        # 不能另走一套"大概齐"的时间轴。
        return PlainTextResponse(srt_to_lrc(raw))
    return PlainTextResponse(raw)


@router.delete("/history/{run_id}")
def delete_run(run_id: str,
               principal: Principal | None = Depends(optional_principal)) -> dict:
    """移到 runs/_trash，不做真删除——误删还能捞回来。"""
    path = _safe(run_id, principal)
    TRASH.mkdir(parents=True, exist_ok=True)
    dest = TRASH / f"{run_id}-{int(time.time())}"
    shutil.move(str(path), str(dest))
    return {"ok": True, "detail": f"已移到 runs/_trash/{dest.name}"}


@router.post("/history/{run_id}/reveal")
def reveal_run(run_id: str,
               principal: Principal | None = Depends(optional_principal)) -> dict:
    path = _safe(run_id, principal)
    try:
        os.startfile(str(path))  # noqa: S606 - Windows 才有，本项目只在 Windows 跑
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"打不开目录：{exc}") from exc
    return {"ok": True}


# ------------------------------------------------------------------ 环境
_ENV_LOCK = threading.Lock()
_ENV_WRITE_LOCK = threading.Lock()
_ENV_STATE: dict[str, Any] = {"data": None, "at": 0.0, "busy": False}
ENV_TTL = 45.0


def _configured_flags() -> dict[str, bool]:
    """.env 里哪些键有非空值。界面据此区分「保留」与「清除」，不回传明文。"""
    try:
        values = load_env_file(ENV_PATH)
    except OSError:
        values = {}
    return {key: bool(str(values.get(key, "")).strip()) for key in ENV_WRITABLE}


class EnvUpdate(BaseModel):
    """配置写入的显式三态：保留（不出现）、替换（values）、清除（delete）。

    以前收一个扁平字典，"值为空"既可能表示不改、也可能表示想删——界面清空
    输入框后旧密钥照旧生效。现在清除是独立动作，不能和"没改"混在一起。
    """

    values: dict[str, str] = Field(default_factory=dict)
    delete: list[str] = Field(default_factory=list)


@contextmanager
def _env_file_lock() -> Iterator[None]:
    """跨进程互斥。线程锁管同进程，锁文件管"另开了一个客户端/脚本"的情况。"""
    lock_path = ENV_PATH.with_name(".env.lock")
    deadline = time.time() + 5
    with _ENV_WRITE_LOCK:
        fd = -1
        while True:
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode("ascii"))
                os.close(fd)
                fd = -1
                break
            except FileExistsError:
                try:
                    if time.time() - lock_path.stat().st_mtime > 30:
                        lock_path.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                if time.time() > deadline:
                    raise HTTPException(
                        status_code=503, detail="配置正在被其他进程写入，请稍后重试"
                    ) from None
                time.sleep(0.05)
        try:
            yield
        finally:
            if fd >= 0:
                os.close(fd)
            lock_path.unlink(missing_ok=True)


def _atomic_write_env(lines: list[str]) -> None:
    """写临时文件再原子替换。写到一半断电不会留下半份 .env。"""
    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(ENV_PATH.parent), prefix=".env-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, ENV_PATH)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _refresh_worker() -> None:
    """体检要探 ComfyUI / Node / Edge，可能十几秒。放后台跑，接口不等它。"""
    payload: dict[str, Any]
    try:
        from app.engine import Engine

        data = Engine().check_env(fresh=True)
        payload = {
            "ready": bool(data.get("ready")),
            "checks": data.get("checks", []),
            "degraded": list(dict.fromkeys(data.get("degraded", []))),
            "live_ready": bool(data.get("live_ready")),
            "keys": data.get("keys", {}),
        }
    except Exception as exc:  # noqa: BLE001
        payload = {
            "ready": False,
            "checks": [{"name": "体检", "state": "bad", "detail": f"检查过程出错：{exc}"}],
            "degraded": [],
            "live_ready": False,
            "keys": {},
        }
    with _ENV_LOCK:
        _ENV_STATE["data"] = payload
        _ENV_STATE["at"] = time.time()
        _ENV_STATE["busy"] = False


@router.get("/env")
def env_status(fresh: bool = False) -> dict:
    with _ENV_LOCK:
        data = _ENV_STATE["data"]
        age = time.time() - _ENV_STATE["at"]
        busy = bool(_ENV_STATE["busy"])
        need = data is None or fresh or age > ENV_TTL
        if need and not busy:
            _ENV_STATE["busy"] = True
            threading.Thread(target=_refresh_worker, name="env-check", daemon=True).start()

    configured = _configured_flags()
    if data is None:
        return {
            "pending": True, "ready": False, "checks": [], "degraded": [],
            "live_ready": False, "keys": {}, "configured": configured,
        }
    return dict(data, pending=need, configured=configured)


@router.post("/env")
def save_env(body: EnvUpdate, user: dict[str, Any] = Depends(require_user)) -> dict:
    """替换或删除命中的行，注释与其他行原样保留。

    **必须登录**：能改 .env 就能改模型地址与密钥去向，这是本机里权限最高的一类
    操作。键名走白名单，值按类型校验——只查"大写字母开头"挡不住任意键。
    """
    pairs: dict[str, str] = {}
    for key, value in body.values.items():
        if key not in ENV_WRITABLE:
            raise HTTPException(status_code=422, detail=f"不支持写入的配置项：{key!r}")
        text = str(value)
        if "\n" in text or "\r" in text or "\x00" in text:
            raise HTTPException(status_code=422, detail=f"{key} 的值里不能有换行")
        pairs[key] = _validate_env_value(key, text)

    deletes = set(body.delete)
    unknown = sorted(k for k in deletes if k not in ENV_WRITABLE)
    if unknown:
        raise HTTPException(status_code=422, detail=f"不支持清除的配置项：{unknown}")
    if deletes & set(pairs):
        raise HTTPException(status_code=422, detail="同一个键不能同时替换和清除")

    if not pairs and not deletes:
        return {
            "updated": [], "deleted": [],
            "detail": "没有要写入的键",
            "configured": _configured_flags(),
        }

    with _env_file_lock():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.is_file() else []
        seen: set[str] = set()
        out: list[str] = []
        for line in lines:
            match = re.match(r"^\s*([A-Z][A-Z0-9_]{1,40})\s*=", line)
            key = match.group(1) if match else ""
            if key and key in deletes:
                # 整行移除，而不是写成 KEY=。后者在 Settings 里仍算"出现过"，
                # 会继续盖住系统环境变量，用户以为删掉了其实没有。
                continue
            if key and key in pairs:
                out.append(f"{key}={pairs[key]}")
                seen.add(key)
            else:
                out.append(line)
        for key, value in pairs.items():
            if key not in seen:
                out.append(f"{key}={value}")
        _atomic_write_env(out)

    # 写完让下一次体检重新跑
    with _ENV_LOCK:
        _ENV_STATE["at"] = 0.0

    return {
        "updated": sorted(pairs),
        "deleted": sorted(deletes),
        "detail": "已写入 .env，重启任务后生效",
        "configured": _configured_flags(),
    }
