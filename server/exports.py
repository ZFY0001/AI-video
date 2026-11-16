"""成片多规格导出：从同一支横屏成片派生竖屏/方形版本。

为什么不在拍摄阶段直接按目标画幅重拍：分镜、提示词、片段缓存都是按
横屏一次成型的，为了一个平台的规格再等一遍逐镜出图，代价完全不成比例。
导出只做确定性的画面重排，几秒到几十秒；画面本身不会变好，也不会变差。

两种规则的差别必须说清楚：

- **适配（fit）**：整幅画面缩小放进目标画幅，两侧用同一画面的放大模糊版补边。
  不裁掉任何内容，烧进画面的字幕完整保留；代价是主体变小、两侧是虚化重复。
- **裁切（crop）**：放大铺满目标画幅后中心裁切。主体更大，但左右会被切掉，
  靠边的字幕可能被裁——界面必须提示，不能默认选它。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from pipeline import branding as pbrand
from pipeline.compose import probe_resolution
from pipeline.video import VideoError, probe_duration, run_ffmpeg, run_ffprobe

from . import artifacts
from .auth import Principal, optional_principal, optional_principal_media
from .history import _safe
from .media import video_response
from .schemas import BatchExportRequest, ExportRequest

router = APIRouter(prefix="/api")

#: 目标画幅 → (宽, 高)。16:9 通常就是原片，只在"原片不是 16:9"或
#: "要叠品牌层"时才会真正编码；源已是 16:9 又没有品牌时导出会被跳过。
TARGETS: dict[str, tuple[int, int]] = {
    "16:9": (1920, 1080),
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
}
MODES = ("fit", "crop")


@dataclass(frozen=True)
class Preset:
    """导出规格预设。

    平台给的码率/响度是**参考值**，界面必须这么标——规范会变，我们不能
    声称"官方认证"。
    """

    id: str
    label: str
    note: str
    fps: int                 # 0 = 沿用原片帧率
    crf: int
    maxrate: str             # 空 = 不设上限
    bufsize: str
    audio_kbps: int
    loudness: float | None   # LUFS；None = 不做响度标准化
    aspect: str | None       # 固定画幅；None = 跟随所选画幅


PRESETS: dict[str, Preset] = {
    "standard": Preset("standard", "标准", "沿用原帧率与画质，不做响度处理",
                       0, 20, "", "", 192, None, None),
    "douyin": Preset("douyin", "抖音竖屏", "9:16 · 30fps · 6Mbps · -14 LUFS（参考值）",
                     30, 20, "6M", "12M", 192, -14.0, "9:16"),
    "shipinhao": Preset("shipinhao", "视频号竖屏", "9:16 · 30fps · 4Mbps · -16 LUFS（参考值）",
                        30, 22, "4M", "8M", 128, -16.0, "9:16"),
    "bilibili": Preset("bilibili", "B站横屏", "16:9 · 30fps · 8Mbps · -14 LUFS（参考值）",
                       30, 20, "8M", "16M", 192, -14.0, "16:9"),
    "high": Preset("high", "高质量", "跟随画幅 · 30fps · 12Mbps 上限",
                   30, 18, "12M", "24M", 256, None, None),
    "small": Preset("small", "小体积", "跟随画幅 · 24fps · 3Mbps 上限",
                    24, 26, "3M", "6M", 96, None, None),
}


def preset_of(preset_id: str) -> Preset:
    preset = PRESETS.get(str(preset_id or "standard"))
    if preset is None:
        raise HTTPException(
            status_code=422,
            detail=f"不支持的导出预设 {preset_id}；可选 {'、'.join(PRESETS)}",
        )
    return preset


def preset_catalog() -> list[dict]:
    """给界面用的预设清单。不要在这里放进"官方保证"之类的话。"""
    return [
        {
            "id": item.id,
            "label": item.label,
            "note": item.note,
            "fps": item.fps,
            "loudness": item.loudness,
            "aspect": item.aspect or "",
        }
        for item in PRESETS.values()
    ]


@dataclass
class ExportTask:
    export_id: str
    run_id: str
    aspect: str
    mode: str
    status: str = "running"  # running / done / failed
    error: str = ""
    width: int = 0
    height: int = 0
    size_mb: float = 0.0
    created: float = field(default_factory=time.time)
    path: Path | None = None
    fingerprint: str = ""
    #: 起任务时的品牌快照。导出中途改模板不能影响正在跑的这条产物。
    brand: dict = field(default_factory=dict)
    brand_fp: str = ""
    brand_version: int = 0
    #: 导出规格预设的展开值。渲染只认这些具体参数，不再读全局常量。
    preset: str = "standard"
    fps: int = 0
    crf: int = 20
    maxrate: str = ""
    bufsize: str = ""
    audio_kbps: int = 192
    loudness: float | None = None


#: 键必须带 run_id：不同项目导出同规格时 export_id 相同，只按 export_id
#: 存任务会让 B 项目拿到 A 项目的进度。
_TASKS: dict[tuple[str, str], ExportTask] = {}
_LOCK = threading.Lock()


def export_id_of(aspect: str, mode: str, preset: str = "standard") -> str:
    base = f"{aspect.replace(':', '-')}-{mode}"
    # 标准规格沿用旧 id：升级后既有导出缓存不该全部作废。
    return base if preset in ("", "standard") else f"{base}-{preset}"


def _task_from_preset(preset: Preset, **kwargs) -> ExportTask:
    return ExportTask(
        preset=preset.id,
        fps=preset.fps,
        crf=preset.crf,
        maxrate=preset.maxrate,
        bufsize=preset.bufsize,
        audio_kbps=preset.audio_kbps,
        loudness=preset.loudness,
        **kwargs,
    )


def _exports_dir(run_path: Path) -> Path:
    return run_path / "output" / "exports"


def _video_path(run_path: Path, export_id: str) -> Path:
    return _exports_dir(run_path) / f"{export_id}.mp4"


def _meta_path(run_path: Path, export_id: str) -> Path:
    return _exports_dir(run_path) / f"{export_id}.json"


#: 实算过的母版哈希。键含 size+mtime，文件一换就自然失效。
_SHA_CACHE: dict[tuple[str, int, int], str] = {}
_SHA_LOCK = threading.Lock()
#: 缓存条目上限。成片动辄上百 MB，只留最近算过的那几支。
_SHA_MAX = 64


def _content_sha(source: Path) -> str:
    """母版内容哈希。manifest 之外要实算一次，之后按 size+mtime 命中缓存。"""
    stat = source.stat()
    key = (str(source.resolve()), stat.st_size, stat.st_mtime_ns)
    with _SHA_LOCK:
        hit = _SHA_CACHE.get(key)
    if hit:
        return hit
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    value = digest.hexdigest()
    with _SHA_LOCK:
        if len(_SHA_CACHE) >= _SHA_MAX:
            _SHA_CACHE.clear()
        _SHA_CACHE[key] = value
    return value


def _fingerprint(source: Path, brand_fp: str = "", run_path: Path | None = None) -> str:
    """导出缓存键。

    以前只用 size+mtime：同尺寸、mtime 又碰巧相同的两份不同内容会命中旧缓存，
    直接产出错片。现在以**内容哈希**为键，manifest 有哈希就先读它（回填后的
    旧项目也有），没有才实算一次并缓存。

    拿不到哈希（文件不可读）时回落到旧的 size-mtime 键：宁可偶发漏命中，
    也不让一条读盘异常把导出整条链打断。
    """
    sha = ""
    if run_path is not None:
        manifest = artifacts.current_manifest(run_path)
        if manifest and manifest.get("sha256"):
            target = (run_path / str(manifest.get("video") or "")).resolve()
            if target == source.resolve():
                sha = str(manifest["sha256"])
    if not sha:
        try:
            sha = _content_sha(source)
        except OSError:
            sha = ""
    if sha:
        base = f"sha:{sha[:40]}"
    else:
        stat = source.stat()
        base = f"{stat.st_size}-{stat.st_mtime_ns}"
    return f"{base}-{brand_fp}" if brand_fp else base


def _brand_context(run_path: Path) -> tuple[dict, str, int]:
    """读取项目当前品牌模板，返回 (配置, 指纹, 版本)。

    品牌只影响导出层，因此不参与项目 revision；导出缓存单独以品牌指纹失效。
    """
    plan_dir = run_path / "plan"
    try:
        meta = json.loads((plan_dir / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    brand = pbrand.normalize(meta.get("brand") if isinstance(meta, dict) else {})
    if not pbrand.is_active(brand):
        return {}, "", 0
    return brand, pbrand.fingerprint(brand, plan_dir), int(brand.get("version") or 0)


def _read_meta(run_path: Path, export_id: str) -> dict | None:
    try:
        data = json.loads(_meta_path(run_path, export_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _item(run_path: Path, task: ExportTask) -> dict:
    return {
        "export_id": task.export_id,
        "aspect": task.aspect,
        "mode": task.mode,
        "status": task.status,
        "error": task.error,
        "width": task.width,
        "height": task.height,
        "size_mb": task.size_mb,
        "branded": bool(task.brand_fp),
        "brand_version": task.brand_version,
        "preset": task.preset,
        "fps": task.fps,
        "loudness": task.loudness,
        "created": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(task.created)),
    }


def _cached(run_path: Path, aspect: str, mode: str, preset_id: str, fingerprint: str,
            brand_fp: str, brand_version: int) -> ExportTask | None:
    export_id = export_id_of(aspect, mode, preset_id)
    meta = _read_meta(run_path, export_id)
    video = _video_path(run_path, export_id)
    if not meta or not video.is_file() or meta.get("fingerprint") != fingerprint:
        return None
    return ExportTask(
        export_id=export_id,
        run_id=run_path.name,
        aspect=aspect,
        mode=mode,
        status="done",
        width=int(meta.get("width") or 0),
        height=int(meta.get("height") or 0),
        size_mb=float(meta.get("size_mb") or 0.0),
        created=float(meta.get("created") or video.stat().st_mtime),
        path=video,
        fingerprint=fingerprint,
        brand_fp=brand_fp,
        brand_version=brand_version,
        preset=str(meta.get("preset") or preset_id),
        fps=int(meta.get("fps") or 0),
        loudness=meta.get("loudness"),
    )


def _has_audio(source: Path) -> bool:
    try:
        info = run_ffprobe(["-show_entries", "stream=codec_type", str(source)])
    except Exception:  # noqa: BLE001 —— 探测失败按"没有音轨"处理，导出仍需完成
        return False
    return any((stream or {}).get("codec_type") == "audio"
               for stream in (info.get("streams") or []))


def _filter(aspect: str, mode: str) -> str:
    """把原片重排到目标画幅，输出统一叫 [b0]，方便再接品牌层。"""
    width, height = TARGETS[aspect]
    if mode == "crop":
        return (
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1[b0]"
        )
    return (
        "[0:v]split=2[bg][fg];"
        f"[bg]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},boxblur=24:2[b];"
        f"[fg]scale={width}:{height}:force_original_aspect_ratio=decrease[f];"
        "[b][f]overlay=(W-w)/2:(H-h)/2,setsar=1[b0]"
    )


def _layer_timing(brand: dict, duration: float) -> dict[str, tuple[float, float]]:
    """片头/片尾的实际出现区间。

    极短成片（比如 3 秒预览）按比例收窄，避免片头片尾在时间轴上叠在一起，
    也避免"片头 2 秒 + 片尾 2 秒"把中间内容全吃掉。
    """
    if duration <= 0:
        return {}
    cap = max(0.4, duration * 0.35)
    intro = min(float(brand.get("intro_seconds") or 0), cap)
    outro = min(float(brand.get("outro_seconds") or 0), cap)
    if intro + outro > duration * 0.9:
        outro = max(0.0, duration * 0.9 - intro)
    timing: dict[str, tuple[float, float]] = {}
    if intro > 0:
        timing["intro"] = (0.0, round(intro, 3))
    if outro > 0:
        timing["outro"] = (round(max(0.0, duration - outro), 3), round(duration, 3))
    return timing


def _compose_filter(task: ExportTask, layers: list[tuple[str, Path]],
                    duration: float) -> str:
    parts = [_filter(task.aspect, task.mode)]
    prev = "b0"
    timing = _layer_timing(task.brand, duration)
    for index, (kind, _path) in enumerate(layers, start=1):
        span = timing.get(kind)
        enable = ""
        if kind != "watermark" and span is not None:
            start, end = span
            enable = f":enable='between(t,{start},{end})'"
        label = f"v{index}"
        parts.append(f"[{prev}][{index}:v]overlay=0:0{enable}[{label}]")
        prev = label
    return ";".join(parts)


def _render(task: ExportTask, run_path: Path, source: Path) -> None:
    width, height = TARGETS[task.aspect]
    out_dir = _exports_dir(run_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    final = _video_path(run_path, task.export_id)
    # 先写 .part 再替换：ffmpeg 中途失败不能留下一个能被当成"已导出"的半截文件。
    part = final.with_suffix(".part.mp4")
    part.unlink(missing_ok=True)
    try:
        duration = probe_duration(source)
        layers: list[tuple[str, Path]] = []
        if task.brand_fp:
            layers = pbrand.build_layers(
                task.brand, run_path / "plan", width, height,
                out_dir / ".brand", task.brand_fp,
            )
        args = ["-i", str(source)]
        for _kind, path in layers:
            args += ["-i", str(path)]
        # 所有路径都走 filter_complex：品牌层就接在重排之后，不用区分有没有叠加。
        filters = _compose_filter(task, layers, duration)
        audio_label = ""
        if task.loudness is not None and _has_audio(source):
            # 响度标准化只作用于音轨，不碰画面。I 后面是平台参考值，不是"官方认证"。
            filters += f";[0:a]loudnorm=I={task.loudness}:TP=-1.5:LRA=11[aout]"
            audio_label = "[aout]"
        args += [
            "-filter_complex", filters,
            "-map", f"[v{len(layers)}]" if layers else "[b0]",
            "-map", audio_label or "0:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(task.crf),
            "-pix_fmt", "yuv420p",
        ]
        if task.fps:
            args += ["-r", str(task.fps)]
        if task.maxrate:
            args += ["-maxrate", task.maxrate, "-bufsize", task.bufsize or task.maxrate]
        args += [
            "-c:a", "aac", "-b:a", f"{task.audio_kbps}k",
            "-movflags", "+faststart",
            str(part),
        ]
        # 导出是独立于任务取消的派生操作：停止某个拍摄任务不该顺手打断导出。
        run_ffmpeg(args, timeout=1800, cancellable=False)
        part.replace(final)
        actual = probe_resolution(final)
        if actual != f"{width}x{height}":
            # 不看结果的编码最危险：曾经因为显式 -map 把视频流丢掉，
            # 产物只剩音轨、状态却是"成功"。这里必须当场失败。
            raise VideoError(f"导出分辨率是 {actual or '未知'}，不是 {width}x{height}")
        size_mb = round(final.stat().st_size / 1024 / 1024, 2)
        meta = {
            "aspect": task.aspect,
            "mode": task.mode,
            "width": width,
            "height": height,
            "size_mb": size_mb,
            "duration": round(probe_duration(final), 3),
            "created": time.time(),
            "fingerprint": task.fingerprint,
            "brand_fp": task.brand_fp,
            "brand_version": task.brand_version,
            "branded": bool(task.brand_fp),
            "preset": task.preset,
            "fps": task.fps,
            "loudness": task.loudness,
        }
        _meta_path(run_path, task.export_id).write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8"
        )
        task.width, task.height, task.size_mb = width, height, size_mb
        task.status = "done"
    except (VideoError, OSError, ValueError) as exc:
        task.status = "failed"
        task.error = f"导出失败：{str(exc)[:300]}"
        part.unlink(missing_ok=True)
    finally:
        task.path = final if task.status == "done" else None


def _source(run_path: Path) -> Path:
    source = artifacts.current_video(run_path)
    if source is None:
        raise HTTPException(status_code=404, detail="这次运行没有成片，无法导出")
    return source


def _source_dims(source: Path) -> tuple[int, int]:
    try:
        width_text, height_text = probe_resolution(source).split("x", 1)
        return int(width_text), int(height_text)
    except Exception:  # noqa: BLE001 —— 探测失败按"未知画幅"处理，交给导出阶段报错
        return 0, 0


def start_export(run_id: str, req: ExportRequest,
                 principal: Principal | None = None) -> dict:
    run_path = _safe(run_id, principal)
    aspect = req.aspect
    mode = req.mode
    if aspect not in TARGETS:
        raise HTTPException(
            status_code=422, detail=f"不支持的画幅 {aspect}；可导出 {'、'.join(TARGETS)}"
        )
    if mode not in MODES:
        raise HTTPException(status_code=422, detail=f"不支持的导出规则 {mode}")
    preset = preset_of(req.preset)
    if preset.aspect and preset.aspect != aspect:
        raise HTTPException(
            status_code=422,
            detail=f"{preset.label}固定为 {preset.aspect}，当前选择的是 {aspect}；换个画幅或换个预设",
        )

    source = _source(run_path)
    src_w, src_h = _source_dims(source)
    target_w, target_h = TARGETS[aspect]
    brand, brand_fp, brand_version = _brand_context(run_path)
    if (src_w and src_h and not brand_fp
            and abs(src_w / src_h - target_w / target_h) < 0.01):
        # 只有“画幅相同且没有任何输出变换”才不需要重编码。原生竖屏加品牌
        # 是有效的输出变换，不能因为画幅没变就拒绝。
        raise HTTPException(status_code=422, detail=f"成片已经是 {aspect}，不需要再导出")
    fingerprint = _fingerprint(source, brand_fp, run_path)
    export_id = export_id_of(aspect, mode, preset.id)

    key = (run_id, export_id)
    with _LOCK:
        cached = _cached(run_path, aspect, mode, preset.id, fingerprint, brand_fp, brand_version)
        if cached is not None:
            _TASKS[key] = cached
            return _item(run_path, cached)
        running = _TASKS.get(key)
        if running and running.status == "running":
            if running.fingerprint == fingerprint:
                return _item(run_path, running)
            # 同一输出文件不能被两条线程同时写：旧任务基于旧成片，先让它跑完。
            raise HTTPException(
                status_code=409,
                detail="上一次导出还在进行，等它完成后再导；避免两个版本互相覆盖",
            )
        task = _task_from_preset(
            preset, export_id=export_id, run_id=run_id, aspect=aspect, mode=mode,
            fingerprint=fingerprint, brand=brand, brand_fp=brand_fp,
            brand_version=brand_version,
        )
        _TASKS[key] = task

    threading.Thread(
        target=_render, args=(task, run_path, source), daemon=True,
        name=f"export-{export_id}",
    ).start()
    return _item(run_path, task)


def start_batch(run_id: str, req: BatchExportRequest,
                principal: Principal | None = None) -> dict:
    """批量导出：一次选中多个规格，按顺序渲染。

    顺序而不是并发，是因为每条导出都要占满 CPU 编码；四个 1080p 同时跑只会
    互相拖慢，还更容易把机器拖到风扇狂转。顺序跑总时长相近，进度也更容易看懂。
    """
    run_path = _safe(run_id, principal)
    source = _source(run_path)
    src_w, src_h = _source_dims(source)
    brand, brand_fp, brand_version = _brand_context(run_path)
    fingerprint = _fingerprint(source, brand_fp, run_path)

    unique: list[tuple[str, str, str]] = []
    for target in req.targets:
        triple = (target.aspect, target.mode, str(target.preset or "standard"))
        if triple not in unique:
            unique.append(triple)

    skipped: list[dict] = []
    planned: list[tuple[str, str, str, str]] = []
    for aspect, mode, preset_id in unique:
        if aspect not in TARGETS:
            raise HTTPException(status_code=422, detail=f"不支持的画幅 {aspect}")
        if mode not in MODES:
            raise HTTPException(status_code=422, detail=f"不支持的导出规则 {mode}")
        preset = preset_of(preset_id)
        if preset.aspect and preset.aspect != aspect:
            raise HTTPException(
                status_code=422,
                detail=f"{preset.label}固定为 {preset.aspect}，批量里出现了 {aspect}",
            )
        target_w, target_h = TARGETS[aspect]
        if (src_w and src_h and not brand_fp
                and abs(src_w / src_h - target_w / target_h) < 0.01):
            skipped.append({
                "aspect": aspect, "mode": mode, "preset": preset.id,
                "reason": f"原片已经是 {aspect} 且没有品牌变换，不需要再导出",
            })
            continue
        planned.append((aspect, mode, preset.id, export_id_of(aspect, mode, preset.id)))

    if not planned:
        raise HTTPException(
            status_code=422,
            detail="选中的规格都已经是原片画幅，没有需要导出的版本",
        )

    fresh: list[ExportTask] = []
    items: list[dict] = []
    with _LOCK:
        # 先全部校验再起任务：批量里有一条在跑，就整批拒绝，避免"部分成功了
        # 但前端以为整批成功"。
        for aspect, mode, preset_id, export_id in planned:
            key = (run_id, export_id)
            cached = _cached(run_path, aspect, mode, preset_id, fingerprint, brand_fp, brand_version)
            if cached is not None:
                _TASKS[key] = cached
                continue
            running = _TASKS.get(key)
            if running and running.status == "running":
                if running.fingerprint != fingerprint:
                    raise HTTPException(
                        status_code=409,
                        detail=f"{aspect} {mode} 的上一次导出还在进行，等它完成后再批量导出",
                    )
        for aspect, mode, preset_id, export_id in planned:
            key = (run_id, export_id)
            existing = _TASKS.get(key)
            if existing is not None:
                reused = existing.status == "running" or (
                    existing.status == "done"
                    and existing.fingerprint == fingerprint
                    and existing.path is not None
                    and existing.path.is_file()
                )
                if reused:
                    items.append(_item(run_path, existing))
                    continue
            task = _task_from_preset(
                preset_of(preset_id),
                export_id=export_id, run_id=run_id, aspect=aspect, mode=mode,
                fingerprint=fingerprint, brand=brand, brand_fp=brand_fp,
                brand_version=brand_version,
            )
            _TASKS[key] = task
            fresh.append(task)
            items.append(_item(run_path, task))

    if fresh:
        threading.Thread(
            target=_render_batch, args=(fresh, run_path, source), daemon=True,
            name=f"export-batch-{run_id}",
        ).start()
    return {"items": items, "skipped": skipped, "branded": bool(brand_fp)}


def _render_batch(tasks: list[ExportTask], run_path: Path, source: Path) -> None:
    for task in tasks:
        _render(task, run_path, source)


def export_status(run_id: str, export_id: str,
                  principal: Principal | None = None) -> dict:
    run_path = _safe(run_id, principal)
    source = artifacts.current_video(run_path)
    brand, brand_fp, brand_version = _brand_context(run_path)
    fingerprint = _fingerprint(source, brand_fp, run_path) if source is not None else ""
    with _LOCK:
        task = _TASKS.get((run_id, export_id))
    # 派生的导出只对当次成片有效：成片重拍后，旧的 done 任务不能继续报告完成。
    if task is not None and (
        task.status == "running" or task.fingerprint == fingerprint
    ):
        return _item(run_path, task)
    if source is None:
        raise HTTPException(status_code=404, detail="没有成片")
    meta = _read_meta(run_path, export_id)
    video = _video_path(run_path, export_id)
    if not meta or not video.is_file() or meta.get("fingerprint") != fingerprint:
        raise HTTPException(status_code=404, detail="没有这次导出（或成片已重拍，缓存已失效）")
    return {
        "export_id": export_id,
        "aspect": str(meta.get("aspect") or ""),
        "mode": str(meta.get("mode") or ""),
        "status": "done",
        "error": "",
        "width": int(meta.get("width") or 0),
        "height": int(meta.get("height") or 0),
        "size_mb": float(meta.get("size_mb") or 0.0),
        "branded": bool(meta.get("branded")),
        "brand_version": int(meta.get("brand_version") or 0),
        "preset": str(meta.get("preset") or "standard"),
        "fps": int(meta.get("fps") or 0),
        "loudness": meta.get("loudness"),
        "created": time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(float(meta.get("created") or 0))
        ),
    }


def list_exports(run_id: str, principal: Principal | None = None) -> dict:
    run_path = _safe(run_id, principal)
    source = artifacts.current_video(run_path)
    if source is None:
        return {"items": [], "source": "", "presets": preset_catalog()}
    brand, brand_fp, brand_version = _brand_context(run_path)
    fingerprint = _fingerprint(source, brand_fp, run_path)
    items: dict[str, dict] = {}
    for preset in PRESETS.values():
        for aspect in TARGETS:
            if preset.aspect and preset.aspect != aspect:
                continue
            for mode in MODES:
                cached = _cached(run_path, aspect, mode, preset.id,
                                 fingerprint, brand_fp, brand_version)
                if cached is not None:
                    items[cached.export_id] = _item(run_path, cached)
    with _LOCK:
        running = [t for t in _TASKS.values()
                   if t.run_id == run_id and t.status == "running"
                   and t.fingerprint == fingerprint]
    for task in running:
        items[task.export_id] = _item(run_path, task)
    order = list(TARGETS) * 1
    result = sorted(
        items.values(),
        key=lambda item: (order.index(item["aspect"]) if item["aspect"] in order else 99,
                          MODES.index(item["mode"]) if item["mode"] in MODES else 99),
    )
    try:
        source_rel = source.resolve().relative_to(run_path.resolve()).as_posix()
    except ValueError:
        source_rel = source.name
    return {
        "items": result,
        "source": source_rel,
        "branded": bool(brand_fp),
        "brand_version": brand_version,
        "presets": preset_catalog(),
    }


# ------------------------------------------------------------------ 接口
@router.get("/history/{run_id}/exports")
def list_run_exports(run_id: str,
                     principal: Principal | None = Depends(optional_principal)) -> dict:
    return list_exports(run_id, principal)


@router.post("/history/{run_id}/exports", status_code=202)
def create_run_export(run_id: str, req: ExportRequest,
                      principal: Principal | None = Depends(optional_principal)) -> dict:
    return start_export(run_id, req, principal)


@router.post("/history/{run_id}/exports/batch", status_code=202)
def create_run_exports_batch(run_id: str, req: BatchExportRequest,
                             principal: Principal | None = Depends(optional_principal)) -> dict:
    return start_batch(run_id, req, principal)


@router.get("/history/{run_id}/exports/{export_id}")
def read_run_export(run_id: str, export_id: str,
                    principal: Principal | None = Depends(optional_principal)) -> dict:
    return export_status(run_id, export_id, principal)


@router.get("/history/{run_id}/exports/{export_id}/file")
def download_run_export(run_id: str, export_id: str, request: Request,
                        principal: Principal | None = Depends(optional_principal_media)) -> Response:
    run_path = _safe(run_id, principal)
    video = _video_path(run_path, export_id)
    meta = _read_meta(run_path, export_id)
    source = artifacts.current_video(run_path)
    if not video.is_file():
        raise HTTPException(status_code=404, detail="导出文件还没准备好（或不存在）")
    _brand, brand_fp, _version = _brand_context(run_path)
    if (not meta or source is None
            or meta.get("fingerprint") != _fingerprint(source, brand_fp, run_path)):
        # 不把旧成片派生出的文件冒充新版本：宁可 404 让用户重导。
        raise HTTPException(status_code=404, detail="成片已重拍或品牌已变更，这次导出已过期，请重新导出")
    return video_response(video, request)
