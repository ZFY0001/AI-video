"""一言片场 · 出图（执行层第一部分）。

三种 provider，按可用性自动选择：
- comfy   ：本地 ComfyUI（POST /prompt + /history + /view），需要服务在跑
- offline ：离线占位帧合成（pipeline.image_render），永远可用
- cloud   ：云端出图 API（需配置 VIDEO_API_BASE_URL，未配置则拒绝执行，避免意外花钱）

无论用哪种 provider，都会额外产出一张「分镜示意帧」，方便人工对照质检。
"""

from __future__ import annotations

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
from .net import describe_net_error, open_url
from .models import ProjectBrief, Shot

# 出图分辨率上限（长边像素）。出图尺寸与成片尺寸是两件事：
# 成片必须 1920x1080，但让 SD1.5 直接画 1920x1080 要 8 倍于 512x320 的显存与算力，
# 8GB 笔记本上表现为「单张跑 3-10 分钟甚至 VAE 解码卡死」。
# 真实出图按长边钳到 GEN_MAX_EDGE，成片由 FFmpeg 放大（见 video.KenBurnsProvider）。
GEN_MAX_EDGE = 640
# 潜变量尺寸必须是 8 的倍数（VAE 下采样 8 倍）
_LATENT_STEP = 8


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

    def generate(self, shot: Shot, brief: ProjectBrief, out_path: Path, seed: int) -> Path:
        genre = getattr(shot, "_genre", None)
        if genre is None:
            from .genre import pick_genre

            genre = pick_genre(brief.theme + brief.style)
        return render_still(shot, brief, genre, out_path, total_shots=getattr(shot, "_total", 1))


# ------------------------------------------------------------------ 云端出图
@dataclass
class CloudImageProvider:
    """智谱 CogView 云端出图。

    **为什么必须有**：本地 SD 1.5 有两条硬伤——文本编码器只认英文（分镜描述里的
    中文等于没写），以及"速度优先"档只有 8 步 / 512×320。CogView 是中文语料的，
    且直接出 1440×720，画质与遵循度都不是一个量级。

    同步接口（不像视频要轮询）：
        POST {base}/images/generations  {model, prompt, size}
        → data[0].url
    """

    #: 画幅 → CogView 支持的尺寸（16:9 用 1440x720，最接近且原生支持）
    SIZE_BY_ASPECT = {
        "16:9": "1440x720",
        "9:16": "720x1440",
        "1:1": "1024x1024",
        "4:3": "1152x864",
        "3:4": "864x1152",
    }

    base_url: str = ""
    api_key: str = ""
    model: str = "cogview-4"
    name: str = "cloud"

    def available(self) -> tuple[bool, str]:
        if not self.base_url or not self.api_key:
            # 报真实存在的配置项（VIDEO_API_*），别提根本不存在的 CLOUD_IMAGE_*
            return False, "未配置云端出图端点（VIDEO_API_BASE_URL / VIDEO_API_KEY）"
        return True, f"智谱 {self.model}（云端出图，中文提示词可直接用）"

    def generate(self, shot: Shot, brief: ProjectBrief, out_path: Path, seed: int) -> Path:
        # 允许同一镜重试时换种子：CogView 不接受 seed，所以在 prompt 尾部
        # 加一个无意义的变化词没有意义（会污染画面描述），这里保持 prompt 原样。
        prompt = (shot.prompt_image or shot.visual or "").strip()
        if not prompt:
            raise ImageError("镜头没有可用的画面描述")
        aspect = str(getattr(brief, "aspect_ratio", "") or "16:9")
        size = self.SIZE_BY_ASPECT.get(aspect, "1440x720")

        body = {"model": self.model, "prompt": prompt, "size": size}
        url = self.base_url.rstrip("/") + "/images/generations"
        data: dict = {}
        # 429 是**账户级限流**，不是"这次请求有问题"：一条片 3 镜要连续调 6 次
        # （3 次出图 + 3 次出片），必然撞上。退避重试 4 次，总等待约 60 秒。
        for attempt in range(4):
            req = urllib.request.Request(
                url, data=json.dumps(body).encode(), method="POST",
                headers={"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"},
            )
            try:
                with open_url(req, timeout=120) as r:
                    data = json.loads(r.read() or b"{}")
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < 3:
                    time.sleep(8 * (attempt + 1))
                    continue
                raise ImageError("云端出图失败：" + describe_net_error(url, exc)) from exc
            except Exception as exc:  # noqa: BLE001
                raise ImageError("云端出图失败：" + describe_net_error(url, exc)) from exc
        # 调用之间留一点间隔，降低撞限流的概率
        time.sleep(2)

        url = ((data.get("data") or [{}])[0] or {}).get("url")
        if not url:
            raise ImageError("云端出图未返回图片地址：" + json.dumps(data, ensure_ascii=False)[:200])

        try:
            with open_url(urllib.request.Request(url), timeout=120) as r:
                out_path.write_bytes(r.read())
        except Exception as exc:  # noqa: BLE001
            raise ImageError("云端图片下载失败：" + describe_net_error(url, exc)) from exc
        return out_path


# ------------------------------------------------------------------ 选择
def build_provider_chain(settings, workflow_dir: Path,
                         require_real: bool = False) -> list[ImageProvider]:
    """按优先级给出 provider 链：能跑真图就用真图，否则占位帧。

    复用 provider_status 的缓存结果，不再单独探测一次——否则这条出片主路径
    上会白等一次 ComfyUI 连接超时。

    `require_real=True` 时**剔除占位帧**，ComfyUI 不可用就抛错。为什么要有这个：

    占位帧本来是"链路验证信号"（像彩条），不是给用户看的画面。但它默认兜底，
    于是 ComfyUI 没开时用户会拿到一条色块片，界面还显示"成功"——**静默降级
    比失败更糟**，因为人会误以为是自己写错了提示词。这个开关让"要么给真图，
    要么明确告诉我为什么给不了"成为可选行为。
    """
    chain: list[ImageProvider] = []
    # require_real 时绕过 30 秒缓存：用户可能刚启动 ComfyUI，缓存会误判成不可用
    comfy_status = next(
        (i for i in provider_status(settings, workflow_dir, fresh=require_real)
         if i["provider"] == "comfy"), None)
    comfy_ok = bool(comfy_status and comfy_status["available"] == "是")
    cloud = CloudImageProvider(base_url=settings.get("VIDEO_API_BASE_URL"),
                               api_key=settings.get("VIDEO_API_KEY"))
    cloud_ok = cloud.available()[0]

    if require_real:
        # 真图优先时**云端在前**：SD 1.5 读不懂中文提示词、只有 8 步 512×320，
        # 画质与遵循度都撑不起"真图"。ComfyUI 留在链尾当断网/额度用完的备胎。
        if cloud_ok:
            chain.append(cloud)
        if comfy_ok:
            chain.append(ComfyUIProvider(
                base_url=settings.get("COMFYUI_BASE_URL"), workflow_dir=workflow_dir))
        if not chain:
            detail = (comfy_status or {}).get("detail", "未探测到 ComfyUI")
            raise ImageError(
                "已勾选「必须真图」，但没有可用出图后端：云端未配置（VIDEO_API_KEY），"
                "本地 ComfyUI 也不可用（" + str(detail)[:120] + "）。"
                "｜配置云端 Key，或先启动 ComfyUI，或取消勾选「必须真图」。")
        return chain

    # 不要求真图：本地优先（免费），ComfyUI 不通就占位帧
    if comfy_ok:
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
        out.append({"provider": prov.name, "available": "是" if ok else "否", "detail": why})
    _STATUS_CACHE[key] = (now, [dict(item) for item in out])
    return out
