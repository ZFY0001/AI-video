"""成片墙与环境设置的数据接口。

只做「读磁盘 / 写 .env」两件事，业务逻辑仍在 `app.engine` 与 `pipeline/`。
所有路径都先过 `_safe()`，只允许 `runs/<一层目录>`，防止被构造成任意读。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse

from pipeline.config import ROOT

from .auth import db, optional_user, optional_user_media
from .media import video_response

router = APIRouter(prefix="/api")

RUNS = ROOT / "runs"
TRASH = RUNS / "_trash"
ENV_PATH = ROOT / ".env"

#: 允许写回的键：大写字母开头、下划线分隔。刻意不接受任意键，避免把 .env 写成垃圾场
_ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]{1,40}$")


# ------------------------------------------------------------------ 工具
def _owner_of(run_id: str) -> int | None:
    """这个成片归谁。None = 没有归属记录（账号体系上线前的旧数据，视为公共）。"""
    try:
        with db() as conn:
            row = conn.execute("SELECT user_id FROM run_owners WHERE run_id = ?",
                               (run_id,)).fetchone()
        return int(row["user_id"]) if row else None
    except Exception:  # noqa: BLE001
        return None


def _can_view(run_id: str, user_id: int | None) -> bool:
    """可见性规则：公共旧数据谁都能看；有主的数据只有主人能看。

    游客（user_id=None）只能看公共数据——否则登录就失去意义了。
    """
    owner = _owner_of(run_id)
    if owner is None:
        return True
    return user_id is not None and owner == user_id


def _safe(run_id: str, user_id: int | None = None) -> Path:
    if not run_id or run_id in (".", "..") or "/" in run_id or "\\" in run_id:
        raise HTTPException(status_code=400, detail="非法的运行 ID")
    root = RUNS.resolve()
    path = (root / run_id).resolve()
    if path.parent != root or not path.is_dir():
        raise HTTPException(status_code=404, detail="运行不存在")
    # 不存在与无权限都回 404：不泄露"这个 id 是真实存在的别人的作品"
    if not _can_view(run_id, user_id):
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
    """镜头的过检图优先，退回第一张尝试图。"""
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
    if (entry / "output" / "final.mp4").is_file():
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
                 user: dict[str, Any] | None = Depends(optional_user)) -> dict:
    if not RUNS.is_dir():
        return {"items": []}

    me = int(user["id"]) if user else None
    items: list[dict[str, Any]] = []
    for entry in sorted(RUNS.iterdir(), reverse=True):
        if not entry.is_dir() or entry.name.startswith("_"):
            continue
        if not _can_view(entry.name, me):
            continue
        # mine=true 用于「个人中心 · 我的作品」：只保留确实归属本人的。
        # 不加这个开关的话，个人中心会把公共旧数据也列进来，和上面的统计
        # （只算自己的）对不上——同一屏两个数互相矛盾。
        if mine and (_owner_of(entry.name) != me):
            continue
        # 只拆解、没拍摄的记录不进成片墙：它一个镜头都没出，
        # 列在这里只会被三态判成「失败」——那是错的，它根本没失败，只是还没拍。
        if _plan_only(entry):
            continue
        board = _storyboard(entry)
        project = board.get("project") or {}
        shots = board.get("shots") or []
        video = entry / "output" / "final.mp4"
        items.append(
            {
                "run_id": entry.name,
                "title": project.get("title") or entry.name.split("-", 2)[-1],
                "style": project.get("style", ""),
                "aspect": project.get("aspect_ratio", ""),
                "duration": round(float(project.get("total_duration") or 0), 1),
                "shots": len(shots),
                "size_mb": round(video.stat().st_size / 1048576, 2) if video.is_file() else 0,
                "has_video": video.is_file(),
                "status": _status(entry, video.is_file()),
                "created": _created(entry.name, entry),
                "tags": _tags(project.get("style", "")),
            }
        )
        if len(items) >= max(1, min(limit, 200)):
            break
    return {"items": items}


@router.get("/history/{run_id}")
def run_detail(run_id: str,
               user: dict[str, Any] | None = Depends(optional_user)) -> dict:
    path = _safe(run_id, int(user["id"]) if user else None)
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
            }
        )

    output = path / "output"
    return {
        "run_id": run_id,
        "title": project.get("title") or run_id.split("-", 2)[-1],
        "style": project.get("style", ""),
        "aspect": project.get("aspect_ratio", ""),
        "duration": round(float(project.get("total_duration") or 0), 1),
        "created": _created(run_id, path),
        "shots": shots,
        "has_video": (output / "final.mp4").is_file(),
        "has_subtitle": (output / "subtitles.srt").is_file(),
        "size_mb": (
            round((output / "final.mp4").stat().st_size / 1048576, 2)
            if (output / "final.mp4").is_file()
            else 0
        ),
        "tags": _tags(project.get("style", "")),
    }


@router.get("/history/{run_id}/cover")
def run_cover(run_id: str,
              user: dict[str, Any] | None = Depends(optional_user_media)):
    path = _safe(run_id, int(user["id"]) if user else None)
    shots = _storyboard(path).get("shots") or []
    if shots:
        still = _still(path, int(shots[0].get("id") or 1))
        if still:
            return FileResponse(still)
    raise HTTPException(status_code=404, detail="没有可用封面")


@router.get("/history/{run_id}/video")
def run_history_video(run_id: str, request: Request,
                      user: dict[str, Any] | None = Depends(optional_user_media)):
    path = _safe(run_id, int(user["id"]) if user else None)
    video = path / "output" / "final.mp4"
    if not video.is_file():
        raise HTTPException(status_code=404, detail="这次运行没有成片")
    return video_response(video, request)


@router.get("/history/{run_id}/subtitle")
def run_subtitle(run_id: str,
                 user: dict[str, Any] | None = Depends(optional_user_media)) -> PlainTextResponse:
    path = _safe(run_id, int(user["id"]) if user else None)
    srt = path / "output" / "subtitles.srt"
    if not srt.is_file():
        raise HTTPException(status_code=404, detail="没有字幕文件")
    return PlainTextResponse(srt.read_text(encoding="utf-8", errors="replace"))


@router.delete("/history/{run_id}")
def delete_run(run_id: str,
               user: dict[str, Any] | None = Depends(optional_user)) -> dict:
    """移到 runs/_trash，不做真删除——误删还能捞回来。"""
    path = _safe(run_id, int(user["id"]) if user else None)
    TRASH.mkdir(parents=True, exist_ok=True)
    dest = TRASH / f"{run_id}-{int(time.time())}"
    shutil.move(str(path), str(dest))
    return {"ok": True, "detail": f"已移到 runs/_trash/{dest.name}"}


@router.post("/history/{run_id}/reveal")
def reveal_run(run_id: str,
               user: dict[str, Any] | None = Depends(optional_user)) -> dict:
    path = _safe(run_id, int(user["id"]) if user else None)
    try:
        os.startfile(str(path))  # noqa: S606 - Windows 才有，本项目只在 Windows 跑
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"打不开目录：{exc}") from exc
    return {"ok": True}


# ------------------------------------------------------------------ 环境
_ENV_LOCK = threading.Lock()
_ENV_STATE: dict[str, Any] = {"data": None, "at": 0.0, "busy": False}
ENV_TTL = 45.0


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

    if data is None:
        return {"pending": True, "ready": False, "checks": [], "degraded": [], "live_ready": False, "keys": {}}
    return dict(data, pending=need)


@router.post("/env")
def save_env(payload: dict) -> dict:
    """只替换命中的行，注释与其他行原样保留。"""
    pairs: dict[str, str] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not _ENV_KEY.match(key):
            raise HTTPException(status_code=422, detail=f"键名不合法：{key!r}")
        text = str(value)
        if "\n" in text or "\r" in text:
            raise HTTPException(status_code=422, detail=f"{key} 的值里不能有换行")
        pairs[key] = text.strip()

    if not pairs:
        return {"updated": [], "detail": "没有要写入的键"}

    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.is_file() else []
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        match = re.match(r"^\s*([A-Z][A-Z0-9_]{1,40})\s*=", line)
        if match and match.group(1) in pairs:
            key = match.group(1)
            out.append(f"{key}={pairs[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, value in pairs.items():
        if key not in seen:
            out.append(f"{key}={value}")

    ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")

    # 写完让下一次体检重新跑
    with _ENV_LOCK:
        _ENV_STATE["at"] = 0.0

    return {"updated": sorted(pairs), "detail": "已写入 .env，重启任务后生效"}
