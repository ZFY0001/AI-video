"""音视频的进出。

进来的是**音轨上传**（`POST /api/media/audio`），出去的是**成片流**。

成片不能用 `FileResponse` 一把梭：浏览器拖动进度条时发的是 `Range` 请求，
服务端必须回 206 + `Content-Range`，否则播放器只能从头播、不能拖。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse

from pipeline.config import UPLOADS_DIR
from pipeline.video import run_ffprobe

from .schemas import AUDIO_EXTS, MAX_AUDIO_BYTES, AudioUpload

CHUNK = 512 * 1024
_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")

router = APIRouter(prefix="/api")

#: 合法 token：16 位十六进制 + 扩展名。**这道正则就是路径穿越的第一道墙**——
#: `../x.mp3`、`C:\x.mp3`、`a/b.mp3` 连格式都不对，走不到文件系统调用。
TOKEN_RE = re.compile(r"^[0-9a-f]{16}\.[a-z0-9]{2,5}$")

#: 上传目录里多久没被碰过的文件可以删（秒）。
#:
#: 敢删的前提是 `Pipeline._adopt_music_source()` 会把音轨复制进运行目录，
#: 一次运行自带副本。删掉的只影响"拿很久以前的拆解重拍"。
UPLOAD_TTL_SECONDS = 7 * 24 * 3600


def resolve_audio(token: str) -> Path:
    """token → uploads 目录里的绝对路径。

    两道检查缺一不可：正则挡格式，`resolve()` 后的父目录比对挡符号链接
    （正则看不出 `link.mp3` 指向 `C:\\Windows\\...`）。
    """
    raw = (token or "").strip()
    if not TOKEN_RE.match(raw):
        raise HTTPException(
            status_code=422,
            detail="音轨 token 格式不对（应形如 3f2a9c1d8e4b7a06.mp3）",
        )
    root = UPLOADS_DIR.resolve()
    path = (root / raw).resolve()
    if path.parent != root:
        raise HTTPException(status_code=422, detail="音轨 token 越界")
    if not path.is_file():
        # 422 而不是 404：对调用方来说这是"参数里的音轨没了"，
        # 不是"这个接口不存在"。前端据此提示"请重新上传"。
        raise HTTPException(status_code=422, detail="音轨不存在或已被清理，请重新上传")
    return path


def _probe_audio(path: Path) -> tuple[float, str]:
    """返回 (时长秒, format_name)。不是可解码音频就抛 422。

    同时查 `format` 与 `stream`：只看时长的话，一段 30 秒的 mp4 视频也能通过，
    然后 `mux_audio` 会拿它当音轨去混——错得很晚，也错得很难查。
    """
    try:
        info = run_ffprobe([
            "-show_entries", "format=duration,format_name:stream=codec_type",
            str(path),
        ])
    except Exception as exc:  # noqa: BLE001 —— ffprobe 缺失、超时、文件损坏都在这一层
        raise HTTPException(
            status_code=422,
            detail=f"这个文件不是可解码的音频（{str(exc)[:120]}）",
        ) from exc

    fmt = str((info.get("format") or {}).get("format_name") or "")
    if not any((s or {}).get("codec_type") == "audio" for s in (info.get("streams") or [])):
        raise HTTPException(status_code=422, detail="这个文件里没有音频轨")
    try:
        duration = float((info.get("format") or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        raise HTTPException(status_code=422, detail="读不出音频时长，这个文件可能已损坏")
    return duration, fmt


def _pick_ext(name: str, fmt: str) -> str:
    """扩展名：客户端的合法扩展名优先，否则从 format_name 推，再不行用 bin。

    扩展名只影响可读性——ffprobe 认的是文件头，不认后缀。但 token 里带个
    `.mp3` 比 `.bin` 好排查得多。
    """
    suffix = Path(name).suffix.lower().lstrip(".")
    if suffix in AUDIO_EXTS:
        return suffix
    for part in fmt.split(","):
        part = part.strip().lower()
        if part in AUDIO_EXTS:
            return part
        if part in ("mp4", "mov", "m4a"):
            return "m4a"
        if part in ("matroska", "webm"):
            return "ogg"
    return "bin"


def _prune_uploads() -> None:
    """删掉上传目录里超过 TTL 的文件。失败不报——清理是顺手的事。"""
    cutoff = time.time() - UPLOAD_TTL_SECONDS
    try:
        for entry in UPLOADS_DIR.iterdir():
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink(missing_ok=True)
    except OSError:
        pass


def read_audio_meta(token: str) -> dict:
    """读上传时记下的旁注（原始文件名 / 大小 / 容器格式）。

    为什么用旁注而不是让客户端回传文件名：文件名要进 `plan/meta.json` 给拆解页
    显示，从客户端拿就是一条不受控的字符串。旁注是服务端自己写的，改不了。
    读不到就返回空字典——旁注被清理掉不影响出片，只影响徽章上的名字。
    """
    raw = (token or "").strip()
    if not TOKEN_RE.match(raw):
        return {}
    try:
        payload = json.loads((UPLOADS_DIR / f"{raw}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


@router.post("/media/audio", response_model=AudioUpload)
async def upload_audio(file: UploadFile = File(...)) -> dict:
    """上传音轨，返回 token。

    边读边算哈希、边数字节：超过上限立刻 413，不把 70 MB 先收完再判断。
    落盘用 `.part` 再改名，进程被杀也不会在上传目录里留下半截文件——
    那种文件会让下一次上传的哈希去重命中一个残缺文件。
    """
    name = Path(file.filename or "audio").name
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

    digest = hashlib.sha256()
    size = 0
    tmp = UPLOADS_DIR / f".uploading-{time.time_ns()}.part"
    try:
        with tmp.open("wb") as out:
            while True:
                chunk = await file.read(CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_AUDIO_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"音频超过 {MAX_AUDIO_BYTES // (1024 * 1024)} MB 上限",
                    )
                digest.update(chunk)
                out.write(chunk)

        if size == 0:
            raise HTTPException(status_code=422, detail="上传的是空文件")

        duration, fmt = _probe_audio(tmp)
        ext = _pick_ext(name, fmt)
        token = f"{digest.hexdigest()[:16]}.{ext}"
        target = UPLOADS_DIR / token
        # 同内容不重复落盘：用户改一句歌词反复传同一首歌是常态。
        tmp.replace(target)
        # 旁注：原始文件名与容器格式。拆解页的徽章要显示"海阔天空.mp3"，
        # 而磁盘上只有一串哈希，这份旁注是唯一还记得名字的地方。
        (UPLOADS_DIR / f"{token}.json").write_text(
            json.dumps({"name": name, "size": size, "format": fmt}, ensure_ascii=False),
            encoding="utf-8",
        )
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        await file.close()

    _prune_uploads()
    return {
        "token": token,
        "name": name,
        "size": size,
        "duration": round(duration, 3),
        "format": fmt,
    }


def video_response(path: Path, request: Request) -> Response:
    if not path.is_file():
        raise HTTPException(status_code=404, detail="成片文件不存在")

    size = path.stat().st_size
    if size <= 0:
        raise HTTPException(status_code=404, detail="成片文件为空")

    header = request.headers.get("range")
    if not header:
        return FileResponse(
            path,
            media_type="video/mp4",
            headers={"Accept-Ranges": "bytes", "Cache-Control": "no-store"},
        )

    match = _RANGE_RE.match(header.strip())
    if not match:
        raise HTTPException(
            status_code=416,
            detail="不支持的 Range 格式",
            headers={"Content-Range": f"bytes */{size}"},
        )

    start_s, end_s = match.group(1), match.group(2)
    if not start_s and not end_s:
        raise HTTPException(
            status_code=416,
            detail="Range 为空",
            headers={"Content-Range": f"bytes */{size}"},
        )

    if not start_s:
        # `bytes=-500` 表示最后 500 字节
        length = int(end_s)
        start = max(0, size - length)
        end = size - 1
    else:
        start = int(start_s)
        end = int(end_s) if end_s else size - 1

    if start >= size or start > end:
        raise HTTPException(
            status_code=416,
            detail="Range 越界",
            headers={"Content-Range": f"bytes */{size}"},
        )
    end = min(end, size - 1)
    length = end - start + 1

    def iter_file():
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        iter_file(),
        status_code=206,
        media_type="video/mp4",
        headers={
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
            "Cache-Control": "no-store",
        },
    )
