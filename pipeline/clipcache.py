"""逐镜片段缓存指纹。

片段（`work/clips/shotNN.mp4`）是拍摄阶段最贵的产物：一次整片重拍要等几十秒
到几十分钟。只有"输入真的变了"才该重画，但过去复用的判据只是"文件存在"，
于是改了 A 镜文案、只重拍 B 镜，A 的旧片段会被静默拼进新成片——画面看着正常，
内容却是旧的。

这里把**影响画面的输入**算成一个稳定指纹，落在片段旁边的 `.key.json`：

- 镜头内容：画面描述、机位、上屏文字、验收标准、主体引用、提示词；
- 项目视觉：题材、风格、画幅、主体色板；
- 渲染规格：预览档位、画布倍率、安全区、视频 provider 链、目标分辨率。

**故意不包含镜长与时间轴起点**：改时长是"只重合成"支持的编辑，片段会被
`fit_clip` 裁尾/冻结末帧复用；把时长算进指纹会让每次改时长都变成整片重拍。
同样不包含状态、质检、片段路径等运行期字段。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

#: 指纹规则版本。改了参与哈希的字段就加一，让旧缓存自然失效。
#: v2：纳入选中图像摘要、实际锁定提示词、provider 身份、工作流内容与被消费素材。
VERSION = 2

_META_SUFFIX = ".key.json"


def _meta_path(clip: Path) -> Path:
    return clip.with_suffix(_META_SUFFIX)


def clip_key(
    shot,
    brief,
    board,
    *,
    genre: str,
    palette: str,
    preview: bool,
    work_scale: float,
    still_scale: float,
    guides: bool,
    providers: list[str],
    video_size: tuple[int, int],
    image_sha256: str = "",
    effective_prompt: str = "",
    workflow_fingerprint: str = "",
    reference_hashes: dict[str, str] | None = None,
) -> str:
    payload = {
        "version": VERSION,
        "shot": {
            "id": int(shot.id),
            "act": str(shot.act or ""),
            "camera": str(shot.camera or ""),
            "visual": str(shot.visual or ""),
            "narration": str(shot.narration or ""),
            "caption": str(shot.caption or ""),
            "prompt_image": str(shot.prompt_image or ""),
            "prompt_video": str(shot.prompt_video or ""),
            "subject_refs": [str(item) for item in (shot.subject_refs or [])],
            "workflow_id": str(shot.workflow_id or ""),
            "acceptance": str(shot.acceptance or ""),
        },
        "brief": {
            "theme": str(getattr(brief, "theme", "") or ""),
            "style": str(getattr(brief, "style", "") or ""),
            "aspect_ratio": str(getattr(brief, "aspect_ratio", "") or ""),
            "language": str(getattr(brief, "language", "") or ""),
            "must_include": list(getattr(brief, "must_include", []) or []),
            "must_avoid": list(getattr(brief, "must_avoid", []) or []),
            "reference": str(getattr(brief, "reference", "") or ""),
        },
        "total_shots": len(getattr(board, "shots", []) or []),
        "genre": str(genre or ""),
        "palette": str(palette or ""),
        "preview": bool(preview),
        "work_scale": round(float(work_scale), 3),
        "still_scale": round(float(still_scale), 3),
        "guides": bool(guides),
        "providers": [str(item) for item in providers],
        "video_size": [int(video_size[0]), int(video_size[1])],
        # 片段来自哪张图，必须进入 key：图换了、片段就不能继续冒充同一镜。
        "image_sha256": str(image_sha256 or ""),
        # 实际拼接后的提示词（含 visual_lock）。只放非敏感文本。
        "effective_prompt": str(effective_prompt or ""),
        # 工作流内容摘要：只改 workflow_id 名称但文件内容变了也应失效。
        "workflow_fingerprint": str(workflow_fingerprint or ""),
        # 只有真正被当前 provider 消费的参考图才进入依赖。
        "references": {str(k): str(v) for k, v in sorted((reference_hashes or {}).items())},
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def read_key(clip: Path) -> str:
    """读片段指纹。缺失/损坏/旧格式一律返回空串，调用方据此判断"未知"。"""
    try:
        data = json.loads(_meta_path(clip).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict) or int(data.get("version") or 0) != VERSION:
        return ""
    return str(data.get("key") or "")


def write_key(clip: Path, key: str, *, provider: str = "") -> None:
    """原子写指纹。片段成功落盘后才调用；写失败不影响已经产出的片段。"""
    path = _meta_path(clip)
    payload = {
        "version": VERSION,
        "key": key,
        "provider": str(provider or ""),
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".clipkey.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            Path(tmp).replace(path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except OSError:
        return


def state(clip: Path, expected: str) -> str:
    """片段与当前输入的关系：current / stale / unknown / missing。"""
    if not clip.is_file():
        return "missing"
    try:
        if clip.stat().st_size <= 0:
            return "missing"
    except OSError:
        return "missing"
    actual = read_key(clip)
    if not actual:
        return "unknown"
    return "current" if actual == expected else "stale"
