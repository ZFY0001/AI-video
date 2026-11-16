"""当前发布产物（master）的统一解析。

历史播放、下载、导出、封面和任务结果必须指向**同一个**成片。此前代码绘制
把任务结果改到 `painted/painted.mp4`，而历史/导出仍固定读 `output/final.mp4`：
用户在创作页看到水彩版，进成片墙却拿到基础版——两个入口都没有报错。

这里用一份 `output/master.json` 记录当前发布产物，所有入口只认这一个指针。
旧项目没有 manifest 时回落到 `output/final.mp4`，保持兼容；有 manifest 但目标
文件缺失时**不**回落到旧路径，避免把"母版丢了"伪装成"还能播"。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

MASTER_FILE = "master.json"

#: 回填按目标文件加锁：同一旧项目被并发访问时只应写出一份 manifest。
_BACKFILL_LOCKS: dict[str, threading.RLock] = {}
_BACKFILL_GUARD = threading.Lock()


def _backfill_lock(target: Path) -> threading.RLock:
    key = str(target.resolve())
    with _BACKFILL_GUARD:
        return _BACKFILL_LOCKS.setdefault(key, threading.RLock())


def _master_path(run_path: Path) -> Path:
    return run_path / "output" / MASTER_FILE


def _within(root: Path, path: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def publish_master(
    run_path: Path,
    video: str | Path,
    *,
    backend: str = "",
    kind: str = "master",
    job_id: str = "",
    snapshot_id: str = "",
    notes: list[str] | None = None,
) -> dict[str, Any]:
    """校验产物后原子切换当前母版。

    只有位于项目目录内、非空且可读的成片才能成为 master。发布失败抛错，
    调用方必须保留上一版母版，而不是写入一个指向半成品或外部路径的指针。
    """
    root = Path(run_path)
    path = Path(video)
    if not path.is_absolute():
        path = root / path
    if not path.is_file() or path.stat().st_size <= 0:
        raise ValueError(f"母版文件不存在或为空：{path}")
    if not _within(root, path):
        raise ValueError(f"母版必须位于项目目录内：{path}")

    manifest = {
        "version": 1,
        "artifact_id": uuid.uuid4().hex,
        "kind": str(kind or "master"),
        "video": path.resolve().relative_to(root.resolve()).as_posix(),
        "sha256": _sha256(path),
        "size": path.stat().st_size,
        "backend": str(backend or ""),
        "job_id": str(job_id or ""),
        "snapshot_id": str(snapshot_id or ""),
        "created": round(time.time(), 3),
        "notes": list(notes or []),
    }
    target = _master_path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".master.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        Path(tmp).replace(target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return {**manifest, "path": str(path)}


def current_manifest(run_path: Path) -> dict[str, Any] | None:
    """读当前母版 manifest。没有或损坏返回 None（调用方再走兼容回落）。"""
    target = _master_path(run_path)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or not payload.get("video"):
        return None
    return payload


def backfill_master(run_path: Path, video: str | Path = "output/final.mp4") -> dict[str, Any] | None:
    """给没有 manifest 的旧项目补一份 legacy 母版指针。

    旧项目拍在母版层之前，只有 `output/final.mp4`。没有 manifest 就拿不到
    内容哈希与 artifact_id——导出缓存键、审计追溯对这批项目全部失效，
    而且每次读都要现算。

    幂等且只补不改：已有 manifest（含用户后来发布的真实版本）一律不动；
    manifest 存在但损坏时也不覆盖，那是"文件坏了"，不该被悄悄替换成另一份。
    """
    root = Path(run_path)
    target = _master_path(root)
    path = Path(video)
    if not path.is_absolute():
        path = root / path
    with _backfill_lock(target):
        if target.exists():
            return current_manifest(root)
        if not path.is_file() or path.stat().st_size <= 0 or not _within(root, path):
            return None
        stat = path.stat()
        manifest = {
            "version": 1,
            "artifact_id": f"legacy-{root.name}",
            "kind": "legacy",
            "video": path.resolve().relative_to(root.resolve()).as_posix(),
            "sha256": _sha256(path),
            "size": stat.st_size,
            "backend": "",
            "job_id": "",
            "snapshot_id": "",
            "created": round(stat.st_mtime, 3),
            "notes": ["旧项目回填：拍摄时还没有母版清单"],
            "legacy": True,
            "backfilled": round(time.time(), 3),
        }
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".master.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(manifest, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            Path(tmp).replace(target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return {**manifest, "path": str(path)}


def current_video(run_path: Path) -> Path | None:
    """当前可播放/可导出的成片。

    有 manifest 时只认 manifest 指向的文件；没有 manifest 的旧项目才回落到
    `output/final.mp4`。manifest 目标缺失返回 None，不猜别的版本。
    """
    root = Path(run_path)
    manifest = current_manifest(root)
    if manifest is not None:
        path = (root / str(manifest.get("video") or "")).resolve()
        if not _within(root, path) or not path.is_file():
            return None
        size = int(manifest.get("size") or 0)
        if size and path.stat().st_size != size:
            return None
        return path
    legacy = root / "output" / "final.mp4"
    return legacy if legacy.is_file() else None


def current_master(run_path: Path) -> dict[str, Any] | None:
    """当前母版 + 实际路径，供接口返回来源信息。

    **只读**：旧项目没有 manifest 时给一份读视图，不往用户项目里写文件。
    补清单是显式迁移（`tools/backfill_masters.py --apply`）——读一次历史就
    改一次别人的项目目录，不是可接受的行为。
    """
    root = Path(run_path)
    path = current_video(root)
    if path is None:
        return None
    manifest = current_manifest(root)
    if manifest is None:
        return {
            "version": 0,
            "artifact_id": "",
            "kind": "legacy",
            "video": path.relative_to(root).as_posix(),
            "backend": "",
            "created": path.stat().st_mtime,
            "legacy": True,
            "path": str(path),
        }
    return {**manifest, "path": str(path)}
