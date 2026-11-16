"""一言片场 · 出图（执行层第一部分）。

三种 provider，按可用性自动选择：
- comfy   ：本地 ComfyUI（POST /prompt + /history + /view），需要服务在跑
- offline ：离线占位帧合成（pipeline.image_render），永远可用
- cloud   ：云端出图 API（需配置 VIDEO_API_BASE_URL，未配置则拒绝执行，避免意外花钱）

无论用哪种 provider，都会额外产出一张「分镜示意帧」，方便人工对照质检。
"""

from __future__ import annotations

import hashlib
import io
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from . import net
from .image_render import RESOLUTIONS, render_still
from .models import ProjectBrief, Shot

# 出图分辨率上限（长边像素）。出图尺寸与成片尺寸是两件事：
# 成片必须 1920x1080，但让 SD1.5 直接画 1920x1080 要 8 倍于 512x320 的显存与算力，
# 8GB 笔记本上表现为「单张跑 3-10 分钟甚至 VAE 解码卡死」。
# 真实出图按长边钳到 GEN_MAX_EDGE，成片由 FFmpeg 放大（见 video.KenBurnsProvider）。
GEN_MAX_EDGE = 640
# 潜变量尺寸必须是 8 的倍数（VAE 下采样 8 倍）
_LATENT_STEP = 8


def inspect_image(path: Path) -> dict[str, Any]:
    """真实读取图片并做基础技术检查。

    这是"质检到底看没看画面"的最低答案：文件存在、能解码、尺寸合理、
    不是纯黑/纯白/纯色。返回的哈希用于复查"这个分数对应哪张图"。

    检查失败不代表画面一定差（可能是生成器写坏了文件），但**必须**让流程
    停下来，而不是继续按镜头文字给分。
    """
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        return {"ok": False, "reason": f"读不到图片：{exc}", "sha256": "", "bytes": 0}

    digest = hashlib.sha256(data).hexdigest()
    try:
        from PIL import Image, ImageStat

        with Image.open(io.BytesIO(data)) as im:
            im.load()
            width, height = im.size
            stat = ImageStat.Stat(im.convert("L"))
            mean = float(stat.mean[0])
            contrast = float(stat.stddev[0])
    except Exception as exc:  # noqa: BLE001 —— 损坏文件、未知格式都算技术检查失败
        return {
            "ok": False,
            "reason": f"无法解码图片：{exc}",
            "sha256": digest,
            "bytes": len(data),
        }

    base = {
        "sha256": digest,
        "bytes": len(data),
        "width": width,
        "height": height,
        "mean": round(mean, 1),
        "contrast": round(contrast, 2),
    }
    if width < 64 or height < 64:
        return {**base, "ok": False, "reason": f"尺寸过小（{width}x{height}）"}
    if mean < 2 or mean > 253:
        return {**base, "ok": False, "reason": "画面接近纯黑或纯白"}
    if contrast < 1.5:
        return {**base, "ok": False, "reason": "画面接近纯色（对比度过低）"}
    return {**base, "ok": True, "reason": ""}


def generation_size(aspect_ratio: str, max_edge: int = GEN_MAX_EDGE) -> tuple[int, int]:
    """按画幅算出**出图**尺寸：保持比例，长边不超过 max_edge，且为 8 的倍数。"""
    w, h = RESOLUTIONS.get(aspect_ratio, RESOLUTIONS["16:9"])
    scale = min(1.0, float(max_edge) / max(w, h))
    out_w = max(_LATENT_STEP * 8, int(w * scale) // _LATENT_STEP * _LATENT_STEP)
    out_h = max(_LATENT_STEP * 8, int(h * scale) // _LATENT_STEP * _LATENT_STEP)
    return out_w, out_h


class ImageError(RuntimeError):
    pass


class ImageProvider(Protocol):
    name: str

    def available(self) -> tuple[bool, str]: ...

    def reference_capability(self) -> tuple[bool, str]: ...

    def generate(self, shot: Shot, brief: ProjectBrief, out_path: Path, seed: int) -> Path: ...


# ------------------------------------------------------------------ ComfyUI
@dataclass
class ComfyUIProvider:
    """本地 ComfyUI。工作流模板放在 workflows/ 目录，由 REA 解析层产出。

    可用性判定要**分维度报告**而不是遇错即返回：
    之前先查服务再查模板，服务没起时只报"未响应"，用户完全看不出
    "模板其实也还没准备好"，两个问题混成一个。改成 `_service_ok` /
    `_template_ok` 分别判断，缺哪项说哪项。
    """

    base_url: str
    workflow_dir: Path
    template: str = "txt2img.json"
    # 真实出图耗时高度依赖可用显存：显存充足约 30-60 秒/张；
    # 8GB 机器上 Windows 占掉 3GB+ 时，VAE 解码阶段可能卡住 10 分钟以上。
    # 宁可早点降级成占位帧，也不要让整条管线死等——所以给一个偏保守的上限。
    timeout: int = 240
    #显存紧张时用更短的超时，快速降级
    timeout_tight: int = 120
    # 出图长边上限：真实出图不按成片分辨率画，画完再交给 FFmpeg 放大
    max_edge: int = GEN_MAX_EDGE
    name: str = "comfy"

    # ------------------------------------------------------------ 可用性
    def _service_ok(self) -> tuple[bool, str]:
        """服务端是否在跑且响应。

        顺带读一次显存占用：8GB 机器上 Windows 自己就吃掉 3GB+，
        出图慢或VAE 解码卡死基本都是显存不够。先看清现状再决定要不要等。
        """
        if not self.base_url:
            return False, "未配置 COMFYUI_BASE_URL"
        url = self.base_url.rstrip("/") + "/system_stats"
        try:
            # 必须用 net.open_url：本机地址要绕过系统代理，
            # 否则开着代理时会拿到 502、却报成"ComfyUI 未响应"（见 pipeline/net.py）
            # 超时 0.6 秒：这里只回答"服务在不在"，不是在等它算图。
            # 实测本机 8188 未监听时拿到的是 TimeoutError 而非 ConnectionRefused
            # （Windows 防火墙对回环端口静默丢包），每次都要走满整个超时——
            # 8 秒超时 = 每次点「开始生成」白等 8 秒。0.6 秒足够覆盖"刚启动"抖动。
            with net.open_url(url, timeout=0.6) as resp:
                if resp.status != 200:
                    return False, f"HTTP {resp.status}"
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            return False, net.describe_net_error(url, exc)
        version = (data.get("system") or {}).get("comfyui_version")
        devs = data.get("devices") or [{}]
        dev = devs[0]
        total = dev.get("vram_total") or 0
        free = dev.get("vram_free") or 0
        detail = f"ComfyUI {version}" if version else "已响应"
        if total:
            gb = total / 1024**3
            used_gb = (total - free) / 1024**3
            detail += f" ｜ 显存 {gb:.1f}GB（已用 {used_gb:.1f}GB）"
            # 显存空闲不足 1GB 时给出预警——这是出图慢 / 卡死的根因
            if free < 1024**3:
                detail += " ⚠ 显存紧张"
        return True, detail

    def _template_ok(self) -> tuple[bool, str]:
        """工作流模板是否存在且为合法 JSON。"""
        path = self.workflow_dir / self.template
        if not path.exists():
            return False, f"缺少工作流模板 {self.template}"
        try:
            graph = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return False, f"模板 JSON 非法：{exc}"
        if not isinstance(graph, dict) or not graph:
            return False, "模板内容为空或格式不对"
        return True, f"{self.template}（{len(graph)} 节点）"

    def available(self) -> tuple[bool, str]:
        # 两个维度都要检查、都要说。任一项缺失就不可用，但**两者状态始终都报出来**：
        # 只报服务不通，用户永远不知道模板也没准备好，排查得来回试。
        svc_ok, svc_msg = self._service_ok()
        tpl_ok, tpl_msg = self._template_ok()
        detail = f"{svc_msg} ｜ {tpl_msg}" if svc_ok and tpl_ok else f"{svc_msg}；{tpl_msg}"
        return (svc_ok and tpl_ok), detail

    def reference_capability(self) -> tuple[bool, str]:
        # 模板是 txt2img：只换提示词/尺寸/种子，没有 IPAdapter/ControlNet 之类的
        # 参考图输入。把"不支持"明确报出来，别让用户以为传了参考图就会生效。
        return False, "不支持参考图（txt2img 模板只吃提示词）"

    def _load_graph(self, shot: Shot, brief: ProjectBrief, seed: int) -> dict[str, Any]:
        """把工作流模板改造成本次请求的 API 格式图。

        只改参数、不动拓扑：正向提示词、负向提示词、分辨率、种子、批大小。
        """
        path = self.workflow_dir / self.template
        raw = json.loads(path.read_text(encoding="utf-8"))
        if "nodes" in raw:  # UI 导出格式（不是我们支持的格式）
            raise ImageError(
                f"{self.template} 是 ComfyUI 的 UI 导出格式（含 nodes/links），"
                "管线只支持 API 格式。请在 ComfyUI 里开启 Dev Mode → "
                "Save (API Format) 重新导出。"
            )
        # 剥离说明性字段：模板里可以用 "_meta" / "_note" 写注释给人看，
        # 但 ComfyUI 会把每个顶层键当成节点，未知 key 会报 missing_node_type。
        graph = {
            nid: node for nid, node in raw.items()
            if not str(nid).startswith("_") and isinstance(node, dict)
        }
        # 出图尺寸按长边钳制，不是成片分辨率（成片由 FFmpeg 放大到 1080p）
        w, h = generation_size(brief.aspect_ratio, self.max_edge)
        # 第一步：统一剥离说明字段（节点顶层与 inputs 内都可能挂 "_note"）。
        # 必须先全部清完再改参数——否则负向提示词那种「保持原样」的分支会被跳过，
        # 留下 _note 残留，ComfyUI 会报 missing_node_type 拒绝整个请求。
        for node in graph.values():
            for k in [k for k in node if str(k).startswith("_")]:
                node.pop(k)
            if isinstance(node.get("inputs"), dict):
                node["inputs"] = {
                    k: v for k, v in node["inputs"].items() if not str(k).startswith("_")
                }

        # 第二步：改参数（只改参数、不动拓扑）
        for node in graph.values():
            ct = str(node.get("class_type", ""))
            inputs = node.setdefault("inputs", {})
            if ct in ("CLIPTextEncode", "BNK_CLIPTextEncodeAdvanced"):
                text = str(inputs.get("text", ""))
                # 负向提示词保持原样（靠关键词识别，避免被镜头提示词覆盖）
                if any(k in text.lower() for k in ("negative", "worst quality", "lowres")):
                    continue
                inputs["text"] = shot.prompt_image
            elif ct == "EmptyLatentImage":
                inputs["width"], inputs["height"] = w, h
                inputs["batch_size"] = 1
            elif ct in ("KSampler", "KSamplerAdvanced"):
                inputs["seed"] = seed
        return graph

    def generate(self, shot: Shot, brief: ProjectBrief, out_path: Path, seed: int) -> Path:
        graph = self._load_graph(shot, brief, seed)
        client_id = uuid.uuid4().hex
        payload = {"prompt": graph, "client_id": client_id}
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base_url.rstrip("/") + "/prompt",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with net.open_url(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise ImageError(f"ComfyUI 提交失败 HTTP {exc.code}: {exc.read().decode('utf-8', 'ignore')[:200]}") from exc
        prompt_id = body.get("prompt_id")
        if not prompt_id:
            raise ImageError(f"ComfyUI 未返回 prompt_id：{body}")
        images = self._wait_images(prompt_id)
        if not images:
            raise ImageError("ComfyUI 执行完成但没有输出图片")
        return self._download(images[0], out_path)

    def _wait_images(self, prompt_id: str) -> list[dict[str, Any]]:
        """轮询 history 直到出图。

        超时按**实时显存占用**动态收紧：显存越紧，等越短。
        理由是显存不足时 ComfyUI 会卡在 VAE 解码（既不报错也不完成），
        这时候继续等只是白等——不如早点让管线降级到占位帧，成片仍能出。
        """
        deadline = time.time() + self._current_timeout()
        while time.time() < deadline:
            try:
                with net.open_url(
                    f"{self.base_url.rstrip('/')}/history/{prompt_id}", timeout=10
                ) as resp:
                    hist = json.loads(resp.read().decode("utf-8"))
            except Exception:  # noqa: BLE001
                time.sleep(1.0)
                continue
            entry = hist.get(prompt_id) or {}
            outputs = entry.get("outputs") or {}
            found: list[dict[str, Any]] = []
            for node_out in outputs.values():
                for img in node_out.get("images", []) or []:
                    if img.get("type") != "temp":
                        found.append(img)
            if found:
                return found
            status = (entry.get("status") or {}).get("status_str")
            if status in ("error", "failed"):
                raise ImageError(f"ComfyUI 执行失败：{json.dumps(entry.get('status'), ensure_ascii=False)[:300]}")
            time.sleep(1.0)
        raise ImageError(f"ComfyUI 等待超时（{self._current_timeout()}s）")

    def _current_timeout(self) -> int:
        """按当前显存占用选超时：显存紧就用短超时，尽快降级。"""
        if self._is_vram_tight():
            return self.timeout_tight
        return self.timeout

    def _is_vram_tight(self) -> bool:
        """显存空闲是否低于 1GB。

        8GB 机器上 Windows 桌面本身占 3GB+，ComfyUI 只剩 5GB 左右，
        一旦其他程序再抢一点，VAE 解码就会因为放不下而卡死。
        """
        ok, msg = self._service_ok()
        return ok and "⚠ 显存紧张" in msg

    def _download(self, image: dict[str, Any], out_path: Path) -> Path:
        query = urllib.parse.urlencode(
            {"filename": image.get("filename", ""), "subfolder": image.get("subfolder", ""), "type": image.get("type", "output")}
        )
        url = f"{self.base_url.rstrip('/')}/view?{query}"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with net.open_url(url, timeout=60) as resp:
            out_path.write_bytes(resp.read())
        return out_path


# ------------------------------------------------------------------ 离线占位帧
@dataclass
class OfflineFrameProvider:
    """离线占位帧合成，永远可用。用于打通管线与人工审看。"""

    name: str = "offline"

    def available(self) -> tuple[bool, str]:
        # 只探测包是否存在即可：真正用到是在 render_still 里
        import importlib.util

        if importlib.util.find_spec("PIL") is None:
            return False, "缺少 Pillow"
        return True, "ok"

    def reference_capability(self) -> tuple[bool, str]:
        # 占位帧按镜头规格确定性合成，没有"参考图"这个输入。
        return False, "不支持参考图（占位帧按镜头规格合成）"

    def generate(self, shot: Shot, brief: ProjectBrief, out_path: Path, seed: int) -> Path:
        genre = getattr(shot, "_genre", None)
        if genre is None:
            from .genre import pick_genre

            genre = pick_genre(brief.theme + brief.style)
        return render_still(shot, brief, genre, out_path, total_shots=getattr(shot, "_total", 1))


# ------------------------------------------------------------------ 云端出图
@dataclass
class CloudImageProvider:
    """云端出图占位实现：**未配置端点时直接拒绝**，避免意外产生费用。"""

    base_url: str
    api_key: str
    model: str = ""
    name: str = "cloud"

    def available(self) -> tuple[bool, str]:
        if not self.base_url or not self.api_key:
            # 报真实存在的配置项（VIDEO_API_*），别提根本不存在的 CLOUD_IMAGE_*
            return False, "未配置云端出图端点（VIDEO_API_BASE_URL / VIDEO_API_KEY）"
        return False, "云端出图适配器尚未接入，本项目以本地出图为主"

    def reference_capability(self) -> tuple[bool, str]:
        return False, "不支持参考图（适配器未接入）"

    def generate(self, shot: Shot, brief: ProjectBrief, out_path: Path, seed: int) -> Path:
        raise ImageError("云端出图未启用")


# ------------------------------------------------------------------ 选择
def build_provider_chain(settings, workflow_dir: Path) -> list[ImageProvider]:
    """按优先级给出 provider 链：能跑真图就用真图，否则占位帧。

    复用 provider_status 的缓存结果，不再单独探测一次——否则这条出片主路径
    上会白等一次 ComfyUI 连接超时。
    """
    chain: list[ImageProvider] = []
    comfy_status = next(
        (i for i in provider_status(settings, workflow_dir)
         if i["provider"] == "comfy"), None)
    if comfy_status and comfy_status["available"] == "是":
        chain.append(ComfyUIProvider(
            base_url=settings.get("COMFYUI_BASE_URL"), workflow_dir=workflow_dir))
    chain.append(OfflineFrameProvider())
    return chain


#: provider_status 的短 TTL 缓存：(缓存键 → (取样时刻, 结果))。
#: 为什么要缓存：ComfyUI 未启动时探测要走到连接超时（实测 0.6s，改前 2.0s），
#: 而它被放在「点开始生成」的关键路径上（engine.check_env）——连点几次就白等几次。
_STATUS_CACHE: dict[Any, tuple[float, list[dict[str, str]]]] = {}
_STATUS_TTL = 30.0


def provider_status(settings, workflow_dir: Path, fresh: bool = False) -> list[dict[str, str]]:
    """探测各出图后端可用性（带 30 秒 TTL 缓存）。

    fresh=True 绕过缓存——用户主动点「环境体检」时用，因为那通常意味着
    刚启动了 ComfyUI 或刚改了配置，必须看到最新状态。
    """
    now = time.monotonic()
    key = (settings.get("COMFYUI_BASE_URL"), str(workflow_dir),
           settings.get("VIDEO_API_BASE_URL"))
    cached = _STATUS_CACHE.get(key)
    if cached and not fresh and now - cached[0] < _STATUS_TTL:
        # 返回副本：调用方改了不该污染缓存，否则下次读到的就是被改过的状态
        return [dict(item) for item in cached[1]]

    comfy = ComfyUIProvider(base_url=settings.get("COMFYUI_BASE_URL"), workflow_dir=workflow_dir)
    offline = OfflineFrameProvider()
    cloud = CloudImageProvider(base_url=settings.get("VIDEO_API_BASE_URL"), api_key=settings.get("VIDEO_API_KEY"))
    out = []
    for prov in (comfy, offline, cloud):
        ok, why = prov.available()
        ref_ok, ref_why = prov.reference_capability()
        out.append({
            "provider": prov.name,
            "available": "是" if ok else "否",
            "detail": why,
            # 参考图能力单独报：能出图 ≠ 能用参考图。两者混在一起，
            # 用户会以为"ComfyUI 在跑"就等于"角色一致性有保障"。
            "reference_images": "是" if ref_ok else "否",
            "reference_detail": ref_why,
        })
    _STATUS_CACHE[key] = (now, [dict(item) for item in out])
    return out
