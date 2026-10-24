"""设备能力检测：决定"开不开放本地模型"。

**为什么要做这个**：本地模型（ComfyUI + SD）对硬件有硬门槛，而这个门槛
用户自己判断不了——他不知道自己 8 GB 显存能跑什么、跑不动什么。
让他去查型号、对参数表，就是又一次"上手成本"。

所以这里做三件事：
  1. 真实探测（不是问用户填）
  2. 给出**档位结论**（能跑哪个级别的本地模型）
  3. 顺着结论给**下一步动作**（开本地 / 配云端）

检测原则：**只报真实探测到的**。探测不到就说"未检测到"，绝不猜一个数值糊弄。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

#: ComfyUI 的常见安装位置（含项目自己的探测脚本用的几个）
COMFY_CANDIDATES = (
    r"D:\ComfyUI",
    r"C:\ComfyUI",
    r"D:\ComfyUI_windows_portable\ComfyUI",
    r"C:\ComfyUI_windows_portable\ComfyUI",
)

#: 显存 → 能跑什么。阈值取的是"能真正出一张像样的图"，不是"勉强不 OOM"。
TIERS = (
    (16.0, "sdxl", "16 GB 以上：可跑 SDXL / Flux，本地画质接近云端"),
    (10.0, "sdxl_low", "10-15 GB：可跑 SDXL（需降分辨率），速度偏慢"),
    (6.0, "sd15", "6-9 GB：只能跑 SD 1.5 低配（512×320 / 8 步），画质有限"),
    (0.0, "none", "显存不足 6 GB 或无独显：本地跑不动，建议用云端"),
)


def _run(cmd: list[str], timeout: int = 8) -> str:
    """跑一条命令取 stdout，失败返回空串。

    必须隐藏窗口：这个函数会在 windowed 的 EXE 里被调用，不隐藏就是
    任务管理器黑框一闪（和 pipeline/proc.py 同一个理由）。
    """
    try:
        from .proc import hidden_kwargs

        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, **hidden_kwargs())
        return (proc.stdout or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _detect_gpu() -> dict[str, Any]:
    """显卡与显存。先说 NVIDIA（能拿到准确显存），退回到 wmic 只看型号。"""
    out = _run(["nvidia-smi", "--query-gpu=name,memory.total",
                "--format=csv,noheader,nounits"])
    if out:
        first = out.splitlines()[0]
        parts = [p.strip() for p in first.split(",")]
        if len(parts) >= 2:
            try:
                vram = round(int(parts[1]) / 1024, 1)
            except ValueError:
                vram = 0.0
            return {"vendor": "nvidia", "name": parts[0], "vram_gb": vram,
                    "source": "nvidia-smi", "accurate": True}

    # 非 NVIDIA：拿得到型号，拿不到准确显存（显存常是共享内存，参考价值低）
    out = _run(["wmic", "path", "win32_VideoController", "get", "name"])
    names = [ln.strip() for ln in out.splitlines()[1:] if ln.strip()]
    if names:
        # 集显优先丢弃：Intel/AMD 的核显跑不了扩散模型
        discrete = [n for n in names if not any(
            k in n.lower() for k in ("intel", "radeon graphics", "vega", "uhd"))]
        name = (discrete or names)[0]
        return {"vendor": "other", "name": name, "vram_gb": 0.0,
                "source": "wmic", "accurate": False}

    return {"vendor": "none", "name": "", "vram_gb": 0.0, "source": "none", "accurate": False}


def _detect_ram() -> float:
    """物理内存 GB。"""
    try:
        import ctypes

        class _MemStatus(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        stat = _MemStatus()
        stat.dwLength = ctypes.sizeof(_MemStatus)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))  # type: ignore[attr-defined]
        return round(stat.ullTotalPhys / 1024**3, 1)
    except Exception:  # noqa: BLE001
        return 0.0


def _detect_disk() -> dict[str, Any]:
    """磁盘剩余空间。模型 4 GB、成片一条几十 MB，空间不足会在中途失败。"""
    import shutil as _shutil

    out = []
    for label, root in (("系统盘", "C:////"), ("程序所在盘", str(Path(sys.executable).anchor or "C:"))):
        try:
            usage = _shutil.disk_usage(root)
            out.append({
                "label": label,
                "root": root,
                "free_gb": round(usage.free / 1024**3, 1),
                "total_gb": round(usage.total / 1024**3, 1),
            })
        except Exception:  # noqa: BLE001
            continue
    free = out[0]["free_gb"] if out else 0.0
    return {"volumes": out, "free_gb": free,
            "enough_for_model": free >= 8.0}


def _detect_os() -> dict[str, Any]:
    """操作系统版本。WebView2 / ffmpeg 的行为在 Win10 与 Win11 上有差别。"""
    try:
        import platform as _pf
        ver = _pf.version()
        rel = _pf.release()
        # Windows 11 的 build 号 >= 22000
        friendly = ""
        try:
            parts = [int(x) for x in ver.split(".")[:3]]
            build = parts[2] if len(parts) > 2 else 0
            friendly = "Windows 11" if build >= 22000 else ("Windows 10" if build >= 10200 else "Windows")
        except Exception:  # noqa: BLE001
            friendly = _pf.system()
        return {"system": _pf.system(), "release": rel, "version": ver, "friendly": friendly}
    except Exception:  # noqa: BLE001
        return {"system": sys.platform, "release": "", "version": "", "friendly": ""}


def _detect_runtimes() -> dict[str, Any]:
    """运行时：Python（源码模式要）、Node（代码绘制要）。"""
    py = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    node_out = _run(["node", "--version"])
    node = node_out.strip().lstrip("v") if node_out else ""
    return {
        "python": py,
        "python_ok": sys.version_info >= (3, 10),
        "node": node,
        "node_ok": bool(node),
    }


def _detect_webview2() -> dict[str, Any]:
    """WebView2 运行时。界面就是这个跑的，没有它窗口一片空白。

    优先读注册表（最可靠）；读不到就退回猜路径。
    """
    try:
        import winreg

        keys = (
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
            (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
        )
        for root, path in keys:
            try:
                with winreg.OpenKey(root, path) as key:
                    ver, _ = winreg.QueryValueEx(key, "pv")
                    if ver:
                        return {"installed": True, "version": str(ver), "source": "registry"}
            except OSError:
                continue
    except Exception:  # noqa: BLE001
        pass
    # 猜路径（Win10/11 一般都有）
    for base in (r"C://Program Files (x86)//Microsoft//EdgeWebView//Application",
                 r"C://Program Files//Microsoft//EdgeWebView//Application"):
        p = Path(base)
        if p.is_dir():
            return {"installed": True, "version": "", "source": "path"}
    return {"installed": False, "version": "", "source": "none"}


def _probe_endpoint(url: str, api_key: str = "", timeout: int = 5) -> dict[str, Any]:
    """探一个云端端点是否可达。只发 HEAD/轻量 GET，不产生费用。"""
    if not url:
        return {"configured": False, "reachable": False, "detail": "未配置"}
    import urllib.error
    import urllib.request

    try:
        from .net import describe_net_error, open_url

        req = urllib.request.Request(url, method="GET")
        if api_key:
            req.add_header("Authorization", "Bearer " + api_key)
        with open_url(req, timeout=timeout) as resp:
            return {"configured": True, "reachable": True,
                    "detail": f"HTTP {getattr(resp, 'status', 200)}"}
    except urllib.error.HTTPError as exc:
        # 4xx 也算"能连通"：说明网络通、只是这个路径要鉴权
        return {"configured": True, "reachable": True, "detail": f"HTTP {exc.code}"}
    except Exception as exc:  # noqa: BLE001
        try:
            from .net import describe_net_error

            detail = describe_net_error(url, exc)
        except Exception:  # noqa: BLE001
            detail = str(exc)[:120]
        return {"configured": True, "reachable": False, "detail": detail}


def _detect_comfyui() -> dict[str, Any]:
    """本机是否已装 ComfyUI、有没有可用模型。"""
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in COMFY_CANDIDATES:
        p = Path(raw)
        if not p.is_dir() or str(p).lower() in seen:
            continue
        seen.add(str(p).lower())
        main_py = p / "main.py"
        ckpt_dir = p / "models" / "checkpoints"
        models = []
        if ckpt_dir.is_dir():
            models = sorted(f.name for f in ckpt_dir.glob("*.safetensors"))
        found.append({
            "path": str(p),
            "has_main": main_py.is_file(),
            "models": models,
            "model_count": len(models),
        })

    # 环境变量里可能指了别的路径
    env_path = os.environ.get("COMFYUI_DIR", "").strip()
    if env_path and Path(env_path).is_dir() and env_path.lower() not in seen:
        ckpt_dir = Path(env_path) / "models" / "checkpoints"
        models = sorted(f.name for f in ckpt_dir.glob("*.safetensors")) if ckpt_dir.is_dir() else []
        found.append({"path": env_path, "has_main": (Path(env_path) / "main.py").is_file(),
                      "models": models, "model_count": len(models)})

    usable = [f for f in found if f["has_main"] and f["model_count"] > 0]
    return {
        "installed": bool(found),
        "usable": bool(usable),
        "locations": found,
        "pick": usable[0]["path"] if usable else "",
    }


def _verdict(gpu: dict[str, Any], ram: float, comfy: dict[str, Any]) -> dict[str, Any]:
    """给档位结论和下一步动作。"""
    vram = float(gpu.get("vram_gb") or 0.0)
    label, reason = "none", TIERS[-1][2]
    for threshold, t_label, t_reason in TIERS:
        if vram >= threshold:
            label, reason = t_label, t_reason
            break

    # 内存不足也会拖死管线（模型加载、ffmpeg 并发都吃内存）
    ram_note = ""
    if 0 < ram < 16:
        ram_note = f"；内存 {ram:.0f} GB 偏小，出片时可能变慢"

    return {
        "tier": label,          # sdxl / sdxl_low / sd15 / none
        "reason": reason + ram_note,
        "local_allowed": label != "none",
        "needs_cloud": label == "none",
        # 有 ComfyUI 且档位够，直接就能用，不用下载任何东西
        "ready_now": bool(comfy.get("usable") and label != "none"),
    }


def detect(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """完整设备画像 + 档位建议。给设置页的"设备检测"卡片 / 体检报告用。

    `settings` 传进来时会额外探云端端点连通性（不传就跳过，避免离线场景白等）。
    """
    gpu = _detect_gpu()
    ram = _detect_ram()
    comfy = _detect_comfyui()
    verdict = _verdict(gpu, ram, comfy)
    disk = _detect_disk()
    osinfo = _detect_os()
    runtimes = _detect_runtimes()
    webview = _detect_webview2()

    cloud: dict[str, Any] = {}
    if settings:
        video_base = str(settings.get("VIDEO_API_BASE_URL") or "")
        llm_base = str(settings.get("LLM_BASE_URL") or "")
        cloud = {
            "image_and_video": _probe_endpoint(video_base.rstrip("/") + "/models",
                                               str(settings.get("VIDEO_API_KEY") or "")),
            "llm": _probe_endpoint(llm_base.rstrip("/") + "/models",
                                    str(settings.get("LLM_API_KEY") or "")),
        }

    advice: list[str] = []
    if verdict["ready_now"]:
        advice.append("本机已装 ComfyUI 且有模型，可以直接勾「本地优先」使用")
    elif verdict["local_allowed"]:
        advice.append("硬件够跑本地模型，但本机还没装 ComfyUI："
                      "需要下载 ComfyUI 本体 + 约 4 GB 的 SD 1.5 模型")
    else:
        advice.append("本机硬件跑不动本地大模型，建议配置云端 API（智谱 / 阿里云）")

    return {
        "checked_at": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "gpu": gpu,
        "cpu": {"cores": os.cpu_count() or 0},
        "ram_gb": ram,
        "disk": disk,
        "os": osinfo,
        "runtimes": runtimes,
        "webview2": webview,
        "comfyui": comfy,
        "cloud": cloud,
        "verdict": verdict,
        "advice": advice,
        "platform": sys.platform,
    }


# ==================================================================== 体检报告
#: 报告落盘位置（相对数据根目录）。json 给程序读，md 给人看。
REPORT_JSON = "device-report.json"
REPORT_MD = "device-report.md"


def _ok(cond: bool) -> str:
    return "✅ 正常" if cond else "⚠️ 需要处理"


def report_markdown(data: dict[str, Any], version: str = "") -> str:
    """把 detect() 的结果渲染成一份可读的体检报告。

    为什么单独出一份 markdown 而不只是 json：用户要能直接发给别人看
    （"我这台机器跑不跑得动"），json 他看不懂。
    """
    gpu = data.get("gpu") or {}
    disk = data.get("disk") or {}
    osinfo = data.get("os") or {}
    rt = data.get("runtimes") or {}
    wv = data.get("webview2") or {}
    comfy = data.get("comfyui") or {}
    cloud = data.get("cloud") or {}
    v = data.get("verdict") or {}

    lines: list[str] = []
    lines.append("# 一言片场 · 设备体检报告")
    lines.append("")
    lines.append(f"- 检测时间：**{data.get('checked_at') or '—'}**")
    lines.append(f"- 程序版本：{version or '—'}")
    lines.append(f"- 操作系统：{osinfo.get('friendly') or osinfo.get('system') or '—'}"
                 f"（{osinfo.get('version') or '—'}）")
    lines.append("")
    lines.append("## 一、结论")
    lines.append("")
    lines.append(f"**档位：`{v.get('tier')}`** — {v.get('reason')}")
    lines.append("")
    lines.append(f"- 允许使用本地模型：{'是' if v.get('local_allowed') else '否'}")
    lines.append(f"- 现在就能用本地模型：{'是' if v.get('ready_now') else '否'}")
    lines.append(f"- 需要配置云端 API：{'是' if v.get('needs_cloud') else '否'}")
    lines.append("")

    lines.append("## 二、硬件")
    lines.append("")
    lines.append("| 项目 | 实测值 | 判断 |")
    lines.append("|---|---|---|")
    gpu_name = gpu.get("name") or "未检测到独显"
    gpu_val = gpu_name
    if gpu.get("vram_gb"):
        gpu_val += f" · {gpu.get('vram_gb')} GB 显存"
    lines.append(f"| 显卡 | {gpu_val} | {_ok(gpu.get('vendor') == 'nvidia')} |")
    lines.append(f"| CPU | {data.get('cpu', {}).get('cores', 0)} 线程 | {_ok(True)} |")
    lines.append(f"| 内存 | {data.get('ram_gb') or '未知'} GB | {_ok(not (0 < (data.get('ram_gb') or 0) < 16))} |")
    lines.append(f"| 磁盘剩余 | {disk.get('free_gb') or '未知'} GB | {_ok(bool(disk.get('enough_for_model')))} |")
    lines.append("")

    lines.append("## 三、系统与运行时")
    lines.append("")
    lines.append("| 项目 | 状态 | 说明 |")
    lines.append("|---|---|---|")
    lines.append(f"| WebView2 运行时 | {_ok(bool(wv.get('installed')))} | 界面依赖它，缺失会白屏 |")
    lines.append(f"| Python | {_ok(bool(rt.get('python_ok')))} | {rt.get('python') or '—'}（源码模式需要） |")
    lines.append(f"| Node | {_ok(bool(rt.get('node_ok')))} | {rt.get('node') or '未安装'}（代码绘制需要） |")
    lines.append("")

    lines.append("## 四、本地模型（ComfyUI）")
    lines.append("")
    if comfy.get("installed"):
        for loc in comfy.get("locations") or []:
            mark = "✅ 可用" if (loc.get("has_main") and loc.get("model_count")) else "⚠️ 不完整"
            models = "、".join(loc.get("models") or []) or "无"
            lines.append(f"- {mark}　`{loc.get('path')}`")
            lines.append(f"  - 模型（{loc.get('model_count', 0)} 个）：{models}")
    else:
        lines.append("- ⚠️ 未检测到 ComfyUI")
    lines.append("")

    if cloud:
        lines.append("## 五、云端 API")
        lines.append("")
        lines.append("| 用途 | 状态 | 详情 |")
        lines.append("|---|---|---|")
        for label, key in (("出图 / 出视频", "image_and_video"), ("文案（大模型）", "llm")):
            item = cloud.get(key) or {}
            if not item.get("configured"):
                lines.append(f"| {label} | ⚠️ 未配置 | — |")
            else:
                lines.append(f"| {label} | {_ok(bool(item.get('reachable')))} | {item.get('detail') or ''} |")
        lines.append("")

    lines.append("## 六、建议")
    lines.append("")
    for line in data.get("advice") or []:
        lines.append(f"- {line}")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("> 这份报告由程序自动探测生成，**换硬件 / 装完模型 / 改完配置后点「重新检测」即可更新**。")
    return "\n".join(lines)


def save_report(root: Path, data: dict[str, Any], version: str = "") -> dict[str, str]:
    """落盘：json（程序读）+ md（人看）。返回两个路径。"""
    import json as _json

    root.mkdir(parents=True, exist_ok=True)
    json_path = root / REPORT_JSON
    md_path = root / REPORT_MD
    json_path.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(report_markdown(data, version), encoding="utf-8")
    return {"json": str(json_path), "markdown": str(md_path)}


def load_report(root: Path) -> dict[str, Any] | None:
    """读上次保存的报告。没有就返回 None。"""
    import json as _json

    p = root / REPORT_JSON
    if not p.is_file():
        return None
    try:
        return _json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
