"""品牌模板接口：保存、撤销、清除 logo 与配置。

品牌不是分镜内容，所以它**不参与项目版本号**：给导出换一个水印不该让正在
编辑分镜的人收到 409。它有自己的 `version`，前端保存时带上期望版本即可避免
两个人同时改品牌时互相覆盖。

历史只保留最近若干份完整配置，撤销就是弹回上一份——这也让"模板应用可撤销"
不是一句口号：每次保存/上传 logo/清除都会先留档。
"""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
import time
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from pipeline import branding as pbrand

from .auth import Principal, optional_principal, optional_principal_media
from .plan import _plan_dir, _project_lock, _read, _safe, _write
from .schemas import BrandRequest

router = APIRouter(prefix="/api")

#: 撤销栈上限。模板配置很小，留 10 份足够回到"换错模板之前"。
HISTORY_KEEP = 10
#: logo 只做包装，不需要原图分辨率的巨无霸文件。
MAX_LOGO_BYTES = 8 * 1024 * 1024
LOGO_EXTS = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _meta(plan_dir: Path) -> dict:
    return _read(plan_dir / "meta.json")


def _save_meta(plan_dir: Path, meta: dict) -> None:
    _write(plan_dir / "meta.json", meta)


def _state(run_id: str, meta: dict) -> dict:
    brand = pbrand.normalize(meta.get("brand"))
    history = meta.get("brand_history") if isinstance(meta.get("brand_history"), list) else []
    logo = (brand.get("logo") or {}).get("asset") or ""
    return {
        "run_id": run_id,
        "brand": brand,
        "applied": pbrand.is_active(brand),
        "can_undo": bool(history),
        "history": len(history),
        "logo_url": f"/api/history/{run_id}/brand/logo" if logo else "",
    }


def _check_version(brand: dict, expected: int | None) -> None:
    if expected is None:
        return
    current = int(brand.get("version") or 0)
    if expected != current:
        raise HTTPException(
            status_code=409,
            detail=(
                f"品牌模板已被修改（当前版本 {current}，你基于版本 {expected} 保存）。"
                "刷新后再改，避免覆盖别人的修改。"
            ),
        )


def _push_history(meta: dict, current: dict) -> list[dict]:
    history = meta.get("brand_history")
    history = list(history) if isinstance(history, list) else []
    # 空配置 + 从未保存过时不必留档；否则"第一次保存"会多一次无意义的撤销。
    if pbrand.is_active(current) or int(current.get("version") or 0) > 0:
        history.append(current)
    return history[-HISTORY_KEEP:]


def _next(current: dict, **fields) -> dict:
    payload = {
        key: fields.get(key, current.get(key))
        for key in ("name", "intro_text", "outro_text", "position", "opacity",
                    "intro_seconds", "outro_seconds")
    }
    payload["logo"] = fields.get("logo", current.get("logo") or {})
    payload["version"] = int(current.get("version") or 0) + 1
    payload["updated"] = _now()
    return pbrand.normalize(payload)


def _logo_dir(plan_dir: Path) -> Path:
    return plan_dir / "assets" / "brand"


def _inspect_logo(data: bytes) -> tuple[str, int, int, str]:
    from PIL import Image

    if not data:
        raise HTTPException(status_code=422, detail="logo 文件是空的")
    if len(data) > MAX_LOGO_BYTES:
        raise HTTPException(status_code=422, detail="logo 不能超过 8 MB")
    try:
        with Image.open(io.BytesIO(data)) as image:
            fmt = str(image.format or "").upper()
            image.load()
            width, height = image.size
    except Exception as exc:  # noqa: BLE001 —— 损坏/未知格式都在这一层
        raise HTTPException(status_code=422, detail=f"logo 无法解码：{str(exc)[:120]}") from exc
    ext = LOGO_EXTS.get(fmt)
    if not ext:
        raise HTTPException(status_code=422, detail="logo 只支持 PNG / JPEG / WebP")
    if width < 32 or height < 32:
        raise HTTPException(status_code=422, detail=f"logo 太小（{width}x{height}），至少 32x32")
    return hashlib.sha256(data).hexdigest(), width, height, ext


def _write_asset(plan_dir: Path, sha: str, ext: str, data: bytes) -> str:
    root = _logo_dir(plan_dir)
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{sha[:16]}{ext}"
    if not target.is_file():
        fd, tmp = tempfile.mkstemp(dir=str(root), prefix=".logo.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
            Path(tmp).replace(target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    return f"assets/brand/{target.name}"


def _prune_assets(plan_dir: Path, meta: dict) -> None:
    """删掉当前配置与撤销栈都不再引用的 logo。

    内容寻址文件不会误伤：同一张图重复上传仍指向同一个文件。
    """
    root = _logo_dir(plan_dir)
    if not root.is_dir():
        return
    keep = {Path(str(asset)).name for asset in _referenced_assets(meta)}
    try:
        for entry in root.iterdir():
            if entry.is_file() and entry.name not in keep:
                entry.unlink(missing_ok=True)
    except OSError:
        pass


def _referenced_assets(meta: dict) -> set[str]:
    brands = [meta.get("brand")]
    history = meta.get("brand_history")
    if isinstance(history, list):
        brands.extend(history)
    assets: set[str] = set()
    for item in brands:
        rel = str(((item or {}).get("logo") or {}).get("asset") or "")
        if rel:
            assets.add(rel)
    return assets


def _require_plan(run_id: str, principal: Principal | None) -> Path:
    return _plan_dir(run_id, principal)


@router.get("/history/{run_id}/brand")
def read_brand(run_id: str,
               principal: Principal | None = Depends(optional_principal)) -> dict:
    # 旧项目没有 plan/ 目录（拆解层之前拍的）。读品牌不该因此 404：
    # 抽屉一打开就在控制台报错，而"没有品牌"本身是个合法状态。
    # 先过 _safe 确认运行存在且可见，再容忍缺 plan/——否则不存在的运行
    # 也会拿到一份默认品牌。写入仍要求 plan/ 存在：没有 meta.json 无处落版本号。
    run_path = _safe(run_id, principal)
    plan_dir = run_path / "plan"
    return _state(run_id, _meta(plan_dir) if plan_dir.is_dir() else {})


@router.put("/history/{run_id}/brand")
def save_brand(run_id: str, req: BrandRequest,
               principal: Principal | None = Depends(optional_principal)) -> dict:
    with _project_lock(run_id):
        plan_dir = _require_plan(run_id, principal)
        meta = _meta(plan_dir)
        current = pbrand.normalize(meta.get("brand"))
        _check_version(current, req.expected_version)
        updated = _next(
            current,
            name=req.name,
            intro_text=req.intro_text,
            outro_text=req.outro_text,
            position=req.position,
            opacity=req.opacity,
            intro_seconds=req.intro_seconds,
            outro_seconds=req.outro_seconds,
        )
        meta["brand"] = updated
        meta["brand_history"] = _push_history(meta, current)
        _save_meta(plan_dir, meta)
        _prune_assets(plan_dir, meta)
    return _state(run_id, meta)


@router.post("/history/{run_id}/brand/undo")
def undo_brand(run_id: str,
               principal: Principal | None = Depends(optional_principal)) -> dict:
    with _project_lock(run_id):
        plan_dir = _require_plan(run_id, principal)
        meta = _meta(plan_dir)
        history = meta.get("brand_history")
        if not isinstance(history, list) or not history:
            raise HTTPException(status_code=409, detail="没有可撤销的品牌模板")
        current = pbrand.normalize(meta.get("brand"))
        previous = pbrand.normalize(history[-1])
        # 以当前版本号为基准 +1，而不是沿用旧记录的版本号：撤销之后版本仍然
        # 单调递增，否则下一次带期望版本的保存会与历史版本"撞号"。
        restored = _next(
            current,
            name=previous["name"],
            intro_text=previous["intro_text"],
            outro_text=previous["outro_text"],
            position=previous["position"],
            opacity=previous["opacity"],
            intro_seconds=previous["intro_seconds"],
            outro_seconds=previous["outro_seconds"],
            logo=previous.get("logo") or {},
        )
        meta["brand"] = restored
        meta["brand_history"] = history[:-1]
        _save_meta(plan_dir, meta)
        _prune_assets(plan_dir, meta)
    return {**_state(run_id, meta), "note": "已撤销到上一版品牌模板"}


@router.delete("/history/{run_id}/brand")
def clear_brand(run_id: str,
                principal: Principal | None = Depends(optional_principal)) -> dict:
    with _project_lock(run_id):
        plan_dir = _require_plan(run_id, principal)
        meta = _meta(plan_dir)
        current = pbrand.normalize(meta.get("brand"))
        meta["brand"] = pbrand.normalize({"version": int(current.get("version") or 0) + 1,
                                          "updated": _now()})
        meta["brand_history"] = _push_history(meta, current)
        _save_meta(plan_dir, meta)
        _prune_assets(plan_dir, meta)
    return {**_state(run_id, meta), "note": "品牌模板已清除，重新导出即可得到无品牌版本"}


@router.post("/history/{run_id}/brand/logo")
async def upload_brand_logo(run_id: str,
                            file: UploadFile = File(...),
                            expected_version: int | None = Query(default=None, ge=0),
                            principal: Principal | None = Depends(optional_principal)) -> dict:
    data = await file.read()
    sha, width, height, ext = _inspect_logo(data)
    with _project_lock(run_id):
        plan_dir = _require_plan(run_id, principal)
        meta = _meta(plan_dir)
        current = pbrand.normalize(meta.get("brand"))
        _check_version(current, expected_version)
        asset = _write_asset(plan_dir, sha, ext, data)
        updated = _next(current, logo={
            "asset": asset,
            "sha": sha,
            "width": width,
            "height": height,
            "name": Path(file.filename or "logo").name[:80],
        })
        meta["brand"] = updated
        meta["brand_history"] = _push_history(meta, current)
        _save_meta(plan_dir, meta)
        _prune_assets(plan_dir, meta)
    return _state(run_id, meta)


@router.delete("/history/{run_id}/brand/logo")
def delete_brand_logo(run_id: str,
                      expected_version: int | None = Query(default=None, ge=0),
                      principal: Principal | None = Depends(optional_principal)) -> dict:
    with _project_lock(run_id):
        plan_dir = _require_plan(run_id, principal)
        meta = _meta(plan_dir)
        current = pbrand.normalize(meta.get("brand"))
        _check_version(current, expected_version)
        updated = _next(current, logo={})
        meta["brand"] = updated
        meta["brand_history"] = _push_history(meta, current)
        _save_meta(plan_dir, meta)
        _prune_assets(plan_dir, meta)
    return _state(run_id, meta)


@router.get("/history/{run_id}/brand/logo")
def read_brand_logo(run_id: str,
                    principal: Principal | None = Depends(optional_principal_media)):
    plan_dir = _require_plan(run_id, principal)
    brand = pbrand.normalize(_meta(plan_dir).get("brand"))
    path = pbrand.logo_path(plan_dir, brand)
    if path is None:
        raise HTTPException(status_code=404, detail="品牌模板还没有 logo")
    return FileResponse(path)
