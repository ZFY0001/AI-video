"""一言片场 · 出片（执行层第二部分）。

- KenBurnsProvider：本地 FFmpeg 把静帧变成有运动的片段（永远可用，也是离线 demo 的成片来源）
- CloudVideoProvider：云端图生视频 API（未配置直接拒绝，避免意外花钱）

FFmpeg 是本机唯一"真正在产出成片"的执行者，所以这里对编码参数、兼容性做了保守设置：
H.264 yuv420p + faststart，保证 Windows 播放器、微信、剪映都能直接打开。
"""

from __future__ import annotations

import json
import shutil
import time
import urllib.error
import urllib.request
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .models import Shot
from .net import describe_net_error, open_url
from .proc import run as run_hidden

_PROTECTED_DIRS = ("desktop", "documents", "pictures", "videos", "music", "favorites")


def _in_protected_dir(p: Path) -> bool:
    """路径是否落在「受控文件夹访问」默认保护的目录里。

    这些目录下的可执行文件，写文件会被 Windows 拦（错误码 -13 EACCES）。
    """
    try:
        return any(part.lower() in _PROTECTED_DIRS for part in p.parts)
    except Exception:  # noqa: BLE001
        return False


def _resolve_tool(name: str) -> str:
    """定位 ffmpeg/ffprobe。

    打包成单文件 EXE 后，PyInstaller 把随包二进制解到 `sys._MEIPASS/bin`，
    该目录**不在 PATH 里**，`shutil.which` 会返回 None，最终回退成裸命令名并
    在运行时抛 FileNotFoundError。这里按「随包 → PATH → 裸命令」三级解析，
    保证 EXE 与源码两种形态都能跑。
    """
    # 1. PyInstaller 随包目录（源码运行时不存在）
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        for candidate in (Path(meipass) / "bin" / f"{name}.exe", Path(meipass) / f"{name}.exe"):
            if candidate.exists():
                return str(candidate)
    # 2. 项目根的 bin/（用户自备 ffmpeg 的约定位置）
    try:
        local = Path(__file__).resolve().parents[1] / "bin" / f"{name}.exe"
        if local.exists():
            # 桌面/文档等受保护目录里的 exe，写盘会被系统拦（实测 2026-10-17：
            # 同一文件同一 SHA256，放桌面项目里报 EACCES，复制到 C 盘根下立刻正常，
            # 连输出到别的盘也写不进去 —— 限制作用于 exe 所在位置本身）。
            # 这种情况优先用 PATH 上的同版本。
            alt = shutil.which(name)
            if alt and _in_protected_dir(local) and not _in_protected_dir(Path(alt)):
                return alt
            return str(local)
    except Exception:  # noqa: BLE001
        pass
    # 3. 系统 PATH
    return shutil.which(name) or name


FFMPEG = _resolve_tool("ffmpeg")
FFPROBE = _resolve_tool("ffprobe")
FPS = 30


class VideoError(RuntimeError):
    pass


def run_ffmpeg(args: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    proc = run_hidden(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()[:500]
        hint = ""
        # 区分「文件真的不在」与「ffmpeg 根本无权访问磁盘」——后者是环境问题，
        # 报成 No such file 会把人引向错误方向（实测踩过：受限终端里两者都像缺文件）。
        if "Permission denied" in err:
            hint = "｜提示：FFmpeg 被拒绝访问文件系统，通常是受限终端/沙箱/杀软拦截，请在普通 PowerShell 或 CMD 中运行"
        elif "No such file or directory" in err:
            hint = (
                "｜提示：输入文件缺失或当前进程/子进程无法读取该路径。"
                "部分受限终端（含沙箱）会把「无权读取」也报成 No such file，"
                "请在普通 PowerShell 或 CMD 中重试"
            )
        raise VideoError(f"FFmpeg 失败（{proc.returncode}）：{err}{hint}")
    return proc


def run_ffprobe(args: list[str], timeout: int = 60) -> dict:
    proc = run_hidden(
        [FFPROBE, "-hide_banner", "-loglevel", "error", "-print_format", "json", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise VideoError(f"FFprobe 失败：{(proc.stderr or '').strip()[:300]}")
    return json.loads(proc.stdout or "{}")


def probe_duration(path: Path) -> float:
    info = run_ffprobe(["-show_entries", "format=duration", str(path)])
    try:
        return float(info["format"]["duration"])
    except (KeyError, ValueError):
        return 0.0


def ffmpeg_available() -> tuple[bool, str]:
    """判断 FFmpeg 是否可用。

    不能用 `shutil.which` 判断：打包后 ffmpeg 可能在随包目录里而不在 PATH 上，
    which 会返回 None 从而误报不可用。这里直接尝试执行已解析出的真实路径。
    """
    try:
        proc = run_hidden(
            [FFMPEG, "-version"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=20,
        )
        if proc.returncode != 0:
            return False, (proc.stderr or "").strip()[:120] or "无法执行 ffmpeg"
        first = (proc.stdout or "").splitlines()[0] if proc.stdout else "ffmpeg"
        return True, first
    except FileNotFoundError:
        return False, "PATH 中找不到 ffmpeg（可把 ffmpeg.exe 放到项目 bin/ 目录）"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _quiet_unlink(path: Path) -> None:
    """尽力删除，失败就算了。

    为什么不能直接 `path.unlink()`：探针 png 刚被 ffmpeg 写完时，杀软
    （Defender 实时防护）常会短暂持有句柄，此时 unlink 抛
    `PermissionError: [WinError 5] 拒绝访问`。

    这个文件只是**探测用的临时物**，删不掉完全不影响"能不能写盘"的结论；
    但如果异常逃出去，整条出片流程会因为一句清理动作直接中断——
    实测就发生过：探测已成功，流程却死在 unlink 上。
    """
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def ffmpeg_filesystem_accessible() -> tuple[bool, str]:
    """真实写一张测试图，验证 FFmpeg 子进程能否读写磁盘。

    只跑 `ffmpeg -version` 是不够的：某些受限终端（沙箱、受控环境、个别杀软）
    允许执行 ffmpeg，却禁止它做文件 I/O，此时版本探测通过但出片全失败，
    且报错是误导性的 "No such file or directory"（实测踩过）。这里做一次
    2×2 像素的写盘+读回，把环境问题提前暴露在「环境体检」里。
    """
    # 多路径：原来只在系统 Temp 探一次。但 Temp 恰恰最容易被限制
    # （受控文件夹访问 / 杀软 / 组策略），而项目 work/ 通常可写。
    # 一个位置写不进去就整条流程失败，是设计缺陷。
    candidates = [Path(__file__).resolve().parents[1] / "work" / "ffprobe",
                  Path(tempfile.gettempdir()) / "yiyan_ffprobe"]
    tried: list[str] = []
    for tmp_dir in candidates:
        probe = tmp_dir / "_probe.png"
        try:
            tmp_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            tried.append(f"{tmp_dir}（建目录失败：{type(exc).__name__}）")
            continue
        _quiet_unlink(probe)
        try:
            run_ffmpeg(
                ["-f", "lavfi", "-i", "color=c=black:s=16x16:d=1",
                 "-frames:v", "1", str(probe)],
                # 15 秒足够写一张 16x16 的图。原来给 60 秒，两个候选位置叠加
                # 最多把主线程堵 2 分钟，用户看到的就是界面未响应。
                timeout=15,
            )
        except VideoError as exc:
            _quiet_unlink(probe)
            first = str(exc).split("｜")[0].splitlines()[0][:110]
            tried.append(f"{tmp_dir}（{first}）")
            continue
        except Exception as exc:  # noqa: BLE001
            _quiet_unlink(probe)
            tried.append(f"{tmp_dir}（{type(exc).__name__}: {str(exc)[:70]}）")
            continue
        ok = probe.exists() and probe.stat().st_size > 0
        _quiet_unlink(probe)
        if ok:
            return True, f"FFmpeg 可正常读写磁盘（探测位置：{tmp_dir}）"
        tried.append(f"{tmp_dir}（命令成功但文件未生成，多半被杀软拦截）")

    return False, "所有候选位置都无法写入：" + "；".join(tried)


class VideoProvider(Protocol):
    name: str

    def available(self) -> tuple[bool, str]: ...

    def make_clip(self, shot: Shot, still: Path, out_path: Path, duration: float) -> Path: ...


MOTION_KINDS = ("zoom_in", "zoom_out", "pan_right", "pan_left")


@dataclass
class KenBurnsProvider:
    """静帧 → 运动片段。

    关键设计：**运动只作用于背景图**，文字层（镜号/字幕/验收标准）在运动之后叠加，
    否则推镜会把标题和字幕一起放大裁掉（这是实测踩过的坑）。
    """

    name: str = "kenburns"
    width: int = 1920
    height: int = 1080

    def available(self) -> tuple[bool, str]:
        return ffmpeg_available()

    def make_clip(self, shot: Shot, still: Path, out_path: Path, duration: float,
                  overlay: Path | None = None, work: int = 2) -> Path:
        """背景做运动 → 叠加文字层 → 缩到成片尺寸。

        overlay 必须与背景的工作分辨率一致（调用方按 work 倍率渲染），
        这样文字既不参与缩放（不会被裁切），也不会被二次重采样（保持锐利）。
        """
        out_path.parent.mkdir(parents=True, exist_ok=True)
        duration = max(1.0, float(duration))
        frames = max(2, int(round(duration * FPS)))
        kind = _motion_kind(shot)
        bg = _kenburns_filter(kind, frames, self.width, self.height, work=work)
        if overlay is not None and overlay.exists():
            fc = (
                f"[0:v]{bg}[bgm];"
                f"[1:v]scale={self.width * work}:{self.height * work},setsar=1[txt];"
                f"[bgm][txt]overlay=0:0:format=auto,"
                f"scale={self.width}:{self.height},fps={FPS},format=yuv420p[out]"
            )
            args = [
                "-loop", "1", "-framerate", str(FPS), "-i", str(still),
                "-loop", "1", "-framerate", str(FPS), "-i", str(overlay),
                "-t", f"{duration:.3f}",
                "-filter_complex", fc, "-map", "[out]",
            ]
        else:
            args = [
                "-loop", "1", "-framerate", str(FPS), "-i", str(still),
                "-t", f"{duration:.3f}",
                "-vf", f"{bg},scale={self.width}:{self.height},fps={FPS},format=yuv420p",
            ]
        args += [
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "20",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "-an",
            str(out_path),
        ]
        run_ffmpeg(args, timeout=300)
        return out_path


def _motion_kind(shot: Shot) -> str:
    cam = (shot.camera or "").lower()
    if "拉" in shot.camera or "pull-back" in cam:
        return "zoom_out"
    if "摇" in shot.camera or "pan" in cam:
        return "pan_left" if shot.id % 2 == 0 else "pan_right"
    if "推" in shot.camera or "push" in cam:
        return "zoom_in"
    return MOTION_KINDS[shot.id % len(MOTION_KINDS)]


def _kenburns_filter(kind: str, frames: int, w: int, h: int, work: int = 2,
                     zoom_end: float = 1.10) -> str:
    """生成 zoompan 滤镜链（缓慢推拉/横移），**只作用于背景图，不含文字层**。

    两个实测踩过的坑：
    1. 用 z='min(zoom+步长,上限)' 这种依赖上一帧状态的表达式，ffmpeg 从第 2 帧起行为
       不可靠；因此一律用帧号 on 直接算缩放值，逐帧确定。
    2. 文字层若一起进 zoompan，推镜会把标题字幕放大裁掉；所以文字层由调用方在
       zoompan **之后**以原始工作分辨率 overlay 上去（见 make_clip）。

    输出保持工作分辨率（w*work），由后续 overlay + 最终 scale 决定成片尺寸。
    """
    zw, zh = w * work, h * work
    scale = f"scale={zw}:{zh}:force_original_aspect_ratio=increase,crop={zw}:{zh},setsar=1"
    last = max(1, frames - 1)
    inc = (zoom_end - 1.0) / last

    if kind == "zoom_in":
        z = f"'min(1+{inc:.6f}*on,{zoom_end})'"
    elif kind == "zoom_out":
        z = f"'max({zoom_end}-{inc:.6f}*on,1.0)'"
    else:
        z = f"'{zoom_end:.3f}'"  # 横移用固定放大倍数，保证有可平移的余量

    x_center = "iw/2-(iw/zoom/2)"
    y_center = "ih/2-(ih/zoom/2)"
    if kind in ("zoom_in", "zoom_out"):
        x_expr, y_expr = x_center, y_center
    else:
        # 平移幅度严格限制在放大后的余量内，边缘不会露白
        travel = "(iw-iw/zoom)*"
        x_expr = f"{travel}(on/{last})" if kind == "pan_right" else f"{travel}(1-on/{last})"
        y_expr = y_center

    zoompan = (
        f"zoompan=z={z}:d={frames}:x='{x_expr}':y='{y_expr}'"
        f":s={zw}x{zh}:fps={FPS},scale={zw}:{zh},setsar=1"
    )
    return f"{scale},{zoompan}"


# ------------------------------------------------------------------ 云端
@dataclass
class CloudVideoProvider:
    """智谱 CogVideoX 图生视频（云端）。

    **为什么必须要有它**：本地 SD 1.5 的文本编码器只认英文，而分镜描述是中英混杂
    （"一碗热汤面 (烟火美食), 掀帘进门, hand-pulled noodles mid-air"）——中文那半
    完全不起作用，实测"掀帘进门"根本没进画面。CogVideoX 是中文语料训练的，
    且输出 1080p。

    **水印**：智谱所有输出都带右下角"AI生成"（文件名 `_watermark.mp4`），
    所以下载后裁掉底部 `crop_bottom`（默认 7%），再缩放回目标尺寸。

    **接口**（实测 2026-10-24）：
        提交 POST {base}/videos/generations   {model, prompt, image_url}
        查询 GET  {base}/async-result/{id}    ← 不是 /videos/generations/{id}
        结果 video_result[0].url
    """

    #: 用哪家：zhipu / minimax。两家的提交、轮询、取文件接口完全不同
    provider: str = "zhipu"
    base_url: str = ""
    api_key: str = ""
    model: str = "cogvideox-2"
    poll_timeout: int = 300
    #: 底部裁掉比例（去"AI生成"水印）。0 表示不裁。
    crop_bottom: float = 0.07
    provider_id: str = ""
    name: str = "cloud"

    def _url(self, path: str) -> str:
        return (self.base_url or "").rstrip("/") + path

    def available(self) -> tuple[bool, str]:
        if not self.base_url or not self.api_key:
            return False, f"未配置 {self.provider} 的端点与 Key"
        label = {"minimax": "MiniMax", "zhipu": "智谱"}.get(self.provider, self.provider)
        return True, f"{label} {self.model}（图生视频，自动裁水印）"

    def _image_b64(self, still: Path) -> str:
        import base64
        import io

        from PIL import Image

        raw = still.read_bytes()
        # base64 会膨胀 33%，控制在 ~1MB 以内，避免请求体过大
        if len(raw) > 900_000:
            buf = io.BytesIO()
            with Image.open(still) as im:
                im.convert("RGB").thumbnail((960, 960), Image.LANCZOS)
                im.save(buf, "JPEG", quality=88)
            raw = buf.getvalue()
        return "data:image/jpeg;base64," + base64.b64encode(raw).decode()

    def _submit(self, prompt: str, still: Path) -> str:
        body = {"model": self.model, "prompt": prompt, "image_url": self._image_b64(still)}
        req = urllib.request.Request(
            self._url("/videos/generations"), data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"},
        )
        data: dict = {}
        # 同 images.py：429 是账户级限流，连续多镜必然遇到，退避重试
        for attempt in range(4):
            try:
                with open_url(req, timeout=60) as r:
                    data = json.loads(r.read() or b"{}")
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < 3:
                    time.sleep(10 * (attempt + 1))
                    # 请求体要重新构造：Request 对象被读过一次后不能复用
                    req = urllib.request.Request(
                        self._url("/videos/generations"), data=json.dumps(body).encode(), method="POST",
                        headers={"Authorization": "Bearer " + self.api_key,
                                 "Content-Type": "application/json"},
                    )
                    continue
                raise VideoError("云端提交失败：" + describe_net_error(
                    self._url("/videos/generations"), exc)) from exc
            except Exception as exc:  # noqa: BLE001
                raise VideoError("云端提交失败：" + describe_net_error(
                    self._url("/videos/generations"), exc)) from exc
        task_id = data.get("id")
        if not task_id:
            raise VideoError("云端未返回任务 id：" + json.dumps(data, ensure_ascii=False)[:200])
        return str(task_id)

    def _wait(self, task_id: str) -> str:
        t0 = time.time()
        last = ""
        while time.time() - t0 < self.poll_timeout:
            req = urllib.request.Request(
                self._url(f"/async-result/{task_id}"),
                headers={"Authorization": "Bearer " + self.api_key},
            )
            try:
                with open_url(req, timeout=40) as r:
                    data = json.loads(r.read() or b"{}")
            except Exception:  # noqa: BLE001
                time.sleep(5)
                continue
            status = (data.get("task_status") or "").upper()
            last = status or last
            if status == "SUCCESS":
                url = ((data.get("video_result") or [{}])[0] or {}).get("url")
                if not url:
                    raise VideoError("云端任务成功但没有返回视频 URL")
                return str(url)
            if status in ("FAIL", "FAILED"):
                raise VideoError("云端渲染失败：" + json.dumps(data, ensure_ascii=False)[:200])
            time.sleep(5)
        raise VideoError(f"云端渲染超时（{self.poll_timeout} 秒，最后状态 {last or '未知'}）")

    def make_clip(self, shot: Shot, still: Path, out_path: Path, duration: float) -> Path:
        prompt = (shot.prompt_image or shot.visual or "").strip() or "cinematic shot"
        if self.provider == "minimax":
            task_id = self._mm_submit(prompt, still, duration)
            url = self._mm_wait(task_id)
        else:
            task_id = self._submit(prompt, still)
            url = self._wait(task_id)

        raw_out = out_path.parent / (out_path.stem + "_cloud_raw.mp4")
        try:
            with open_url(urllib.request.Request(url), timeout=240) as r:
                raw_out.write_bytes(r.read())
        except Exception as exc:  # noqa: BLE001
            raise VideoError("云端视频下载失败：" + describe_net_error(url, exc)) from exc

        # 裁水印 → 缩放 → 截到本镜时长。云端是 1720x1080/5.1s，本地成片要 1920x1080
        w, h = 1920, 1080
        keep = max(0.5, 1.0 - float(self.crop_bottom))
        vf = (f"crop=iw:ih*{keep:.4f}:0:0,"
              f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}")
        try:
            run_ffmpeg([
                "-i", str(raw_out), "-vf", vf, "-t", f"{duration:.2f}",
                "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                str(out_path),
            ], timeout=180)
        finally:
            raw_out.unlink(missing_ok=True)
        return out_path

    # ---------------------------------------------------------- MiniMax
    # 三步入库，和智谱完全不同：
    #   ① POST /v1/video_generation        → task_id
    #   ② GET  /v1/query/video_generation  → status + file_id
    #   ③ GET  /v1/files/retrieve          → file.download_url

    def _mm_submit(self, prompt: str, still: Path, duration: float) -> str:
        body = {
            "model": self.model,
            "prompt": prompt,
            "first_frame_image": self._image_b64(still),
            # MiniMax 只认 6 或 10 秒；镜长不足 6 秒也按 6 秒生成，后面再裁
            "duration": 6 if duration <= 6 else 10,
            "resolution": "768P" if self.model.startswith("MiniMax-Hailuo") else "720P",
        }
        url = self._url("/v1/video_generation")
        data: dict = {}
        for attempt in range(4):
            req = urllib.request.Request(
                url, data=json.dumps(body).encode(), method="POST",
                headers={"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"},
            )
            try:
                with open_url(req, timeout=90) as r:
                    data = json.loads(r.read() or b"{}")
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < 3:
                    time.sleep(10 * (attempt + 1))
                    continue
                raise VideoError("MiniMax 提交失败：" + describe_net_error(url, exc)) from exc
            except Exception as exc:  # noqa: BLE001
                raise VideoError("MiniMax 提交失败：" + describe_net_error(url, exc)) from exc

        base_resp = data.get("base_resp") or {}
        if base_resp.get("status_code") not in (0, "0", None):
            raise VideoError(f"MiniMax 拒绝任务：{base_resp.get('status_msg') or data}")
        task_id = data.get("task_id")
        if not task_id:
            raise VideoError("MiniMax 未返回 task_id：" + json.dumps(data, ensure_ascii=False)[:200])
        return str(task_id)

    def _mm_wait(self, task_id: str) -> str:
        """轮询到 Success，再换下载链接。未完成时业务码是 0 且 status 为
        Queueing/Processing/Preparing，所以不能只看 status_code。"""
        t0 = time.time()
        last = ""
        while time.time() - t0 < self.poll_timeout:
            q = self._url("/v1/query/video_generation?task_id=" + task_id)
            try:
                req = urllib.request.Request(q, headers={"Authorization": "Bearer " + self.api_key})
                with open_url(req, timeout=40) as r:
                    data = json.loads(r.read() or b"{}")
            except Exception:  # noqa: BLE001
                time.sleep(5)
                continue
            status = str(data.get("status") or "").lower()
            last = status or last
            if status in ("success", "succeeded"):
                file_id = str(data.get("file_id") or "")
                if not file_id:
                    raise VideoError("MiniMax 成功但没给 file_id：" + json.dumps(data, ensure_ascii=False)[:200])
                return self._mm_file_url(file_id)
            if status in ("fail", "failed"):
                raise VideoError("MiniMax 渲染失败：" + json.dumps(data, ensure_ascii=False)[:200])
            time.sleep(5)
        raise VideoError(f"MiniMax 渲染超时（{self.poll_timeout} 秒，最后状态 {last or '未知'}）")

    def _mm_file_url(self, file_id: str) -> str:
        q = self._url("/v1/files/retrieve?file_id=" + file_id)
        req = urllib.request.Request(q, headers={"Authorization": "Bearer " + self.api_key})
        try:
            with open_url(req, timeout=40) as r:
                data = json.loads(r.read() or b"{}")
        except Exception as exc:  # noqa: BLE001
            raise VideoError("MiniMax 取文件链接失败：" + describe_net_error(q, exc)) from exc
        url = ((data.get("file") or {}).get("download_url")) or ""
        if not url:
            raise VideoError("MiniMax 没返回 download_url：" + json.dumps(data, ensure_ascii=False)[:200])
        return str(url)


def build_video_chain(settings, width: int, height: int) -> list[VideoProvider]:
    chain: list[VideoProvider] = []
    from . import model_registry

    vid_provider, vid_model, _vid_meta = model_registry.resolve(settings, "video")
    prov_meta = model_registry.get_provider(vid_provider) or {}
    key_name = str(prov_meta.get("key_env") or "VIDEO_API_KEY")
    vid_key = str(settings.get(key_name) or settings.get("VIDEO_API_KEY") or "")
    vid_base = str(settings.get(vid_provider.upper() + "_BASE_URL")
                   or prov_meta.get("base_url") or settings.get("VIDEO_API_BASE_URL") or "")
    cloud = CloudVideoProvider(provider=vid_provider, base_url=vid_base,
                               api_key=vid_key, model=vid_model)
    ok, _why = cloud.available()
    if ok:
        chain.append(cloud)
    chain.append(KenBurnsProvider(width=width, height=height))
    return chain
