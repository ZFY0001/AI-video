"""出图与评审缓存（缓存 v2 的生成阶段）。

片段缓存只能省掉编码；真正昂贵的是出图与语义评审。此前每次正式拍摄都会
先重新出图、重新评审，再到 `_make_clip()` 才发现旧片段能复用——既浪费时间，
也可能出现“新评审证据配旧视频”的错位。

这里把**最终选中的候选**作为一条完整记录缓存：图像、哈希、技术证据、评审
模式、来源 provider 一起存一起取。命中时调用方直接恢复这套证据，不再调用
任何出图或评审函数。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

VERSION = 3


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".gencache.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def signature(
    *,
    shot_id: int,
    prompt: str,
    visual_locks: list[str],
    brief_key: str,
    genre: str,
    palette: str,
    preview: bool,
    size: tuple[int, int],
    seed: int,
    candidates: int,
    provider_identity: list[str],
    workflow_fingerprint: str,
    reference_hashes: dict[str, str],
    frame_inputs: dict[str, Any],
) -> str:
    payload = {
        "version": VERSION,
        "shot_id": int(shot_id),
        # 实际拼接后的提示词，而不是原始 prompt：角色 visual_lock 会改变出图。
        "prompt": str(prompt),
        "visual_locks": list(visual_locks),
        "brief": str(brief_key),
        "genre": str(genre),
        "palette": str(palette),
        "preview": bool(preview),
        "size": [int(size[0]), int(size[1])],
        # 种子只影响“第一次尝试”；命中记录代表最终选择，重放时不需要重跑重试链。
        "seed": int(seed),
        "candidates": int(candidates),
        # provider 身份只放规范化后的非敏感配置，绝不写入 API Key。
        "providers": list(provider_identity),
        "workflow": str(workflow_fingerprint),
        # 只有真正被当前 provider 消费的参考图才进入签名。
        "references": {str(k): str(v) for k, v in sorted(reference_hashes.items())},
        # 离线占位帧把镜头文案/辅助线/倍率直接画进图里，这些也是出图输入。
        "frame": dict(frame_inputs),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def load(cache_dir: Path, shot_id: int, key: str) -> dict[str, Any] | None:
    """读一条生成缓存。图像缺失或摘要不符一律视为未命中。"""
    record_path = Path(cache_dir) / f"shot{shot_id:02d}-{key}.json"
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or int(record.get("version") or 0) != VERSION:
        return None
    if str(record.get("key") or "") != key:
        return None
    image = Path(str(record.get("image") or ""))
    if not image.is_file() or image.stat().st_size <= 0:
        return None
    sha = str(record.get("sha256") or "")
    if sha and _sha256(image) != sha:
        return None
    record["image"] = str(image)
    return record


def store(
    cache_dir: Path,
    shot_id: int,
    key: str,
    image: Path,
    *,
    provider: str,
    quality: int,
    status: str,
    evidence: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """保存最终选中的图像与完整证据。失败不阻断已完成的拍摄。"""
    directory = Path(cache_dir)
    directory.mkdir(parents=True, exist_ok=True)
    suffix = image.suffix or ".png"
    target = directory / f"shot{shot_id:02d}-{key}{suffix}"
    part = target.with_suffix(target.suffix + ".part")
    try:
        part.write_bytes(image.read_bytes())
        part.replace(target)
    except OSError:
        part.unlink(missing_ok=True)
        return {}
    record = {
        "version": VERSION,
        "key": key,
        "shot_id": int(shot_id),
        "image": str(target),
        "sha256": _sha256(target),
        "provider": str(provider or ""),
        "quality": int(quality),
        "status": str(status or ""),
        "evidence": evidence,
        "candidates": candidates,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        _write(directory / f"shot{shot_id:02d}-{key}.json", record)
    except OSError:
        return {}
    return record
