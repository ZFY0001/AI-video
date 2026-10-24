# -*- coding: utf-8 -*-
"""代码绘制后端：把项目分镜翻译成p5.js 水彩场景，逐帧渲染成片。

设计要点（三条，都是踩过坑才定下来的）：

1. **每一帧必须是时间t 的纯函数**。引擎会并行、乱序地渲染帧（见 render.mjs 的
   --frames/--clip），任何跨帧残留状态都会导致画面闪烁。所以生成的场景代码里
   禁止出现跨帧的可变量，用 `boilSeed(key)` 让线条"沸腾"但每帧可复现。

2. **不手写场景，改为数据驱动**。原 skill 要求 AI 为每个题材手写一个 JS 场景函数，
   那没法批量出片。这里把「题材 + 分镜」编译成场景：镜头数、时长、主体、情绪、
   机位全部来自项目已有的 Shot 对象，AI 不参与也能出画。

3. **离线优先，逐级降级**。Node / 浏览器 / 水彩引擎任一缺失，都退回项目原有的
   Pillow 出图，而不是抛异常——与项目其他后端保持一致。

引擎来自 tuzhechen2005/opus-video-skills（MIT，John Heibel 的 ClaudeAnimationBase
方法论）。它本来只给 Agent 当"说明书"，这里把它当真正的渲染引擎用。
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time

from .proc import hidden_kwargs, run as run_hidden
from .models import caption_of
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

def template_dir() -> Path:
    """定位水彩渲染模板（运行时求值，不缓存成常量）。

    打包成 EXE 后，随包的 vendor/ 在 PyInstaller 临时解包目录 sys._MEIPASS 下，
    而不是 EXE 同级——这与 prompts/workflows 走 resource_dir() 是同一个道理。
    曾经只写 ROOT / "vendor"，源码运行没问题、打包后必然找不到模板，
    表现是"代码绘制不可用：缺少渲染模板"。
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        cand = Path(meipass) / "vendor" / "painted-template"
        if cand.exists():
            return cand
    return ROOT / "vendor" / "painted-template"


#: 兼容旧引用（测试与外部脚本）。注意这是**导入时**快照，运行期请用 template_dir()。
TEMPLATE_DIR = template_dir()


def _in_bundle() -> bool:
    """当前是否运行在 PyInstaller 的临时解包目录里（模板只读）。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return False
    return template_dir().is_relative_to(Path(meipass))


def modules_prefix() -> str:
    """p5 / p5.brush 的加载前缀（HTML 里用）。

    源码模式下就是工作目录下的相对路径 node_modules；打包后工作副本里没有
    node_modules，只能指回模板目录的绝对路径（反斜杠要转 /，file:// 才好使）。
    """
    if not _in_bundle():
        return "node_modules"
    mods = template_dir() / "node_modules"
    if not mods.exists():
        return "node_modules"
    return str(mods).replace(os.sep, "/")


def work_dir() -> Path:
    """可写的工作副本目录。

    渲染要往模板里写场景文件，而打包后的模板在 sys._MEIPASS 里——那是只读的
    （甚至进程退出即消失）。所以源码模式下直接用模板目录，打包模式复制一份到
    用户可写目录再渲染。node_modules 有 63 MB，不复制（复制会让每次启动多花
    几十秒），而是让 studio.html 用绝对路径去模板目录加载 p5。
    """
    tpl = template_dir()
    if not _in_bundle():
        return tpl
    dst = ROOT / "cache" / "painted"
    if (dst / "render.mjs").exists():
        _ensure_writable(dst)
        return dst
    dst.mkdir(parents=True, exist_ok=True)
    for item in ("render.mjs", "studio.html", "package.json", "package-lock.json"):
        src = tpl / item
        if src.exists():
            shutil.copy2(src, dst / item)
    (dst / "src" / "scenes").mkdir(parents=True, exist_ok=True)
    # config.js 也要复制：引擎在 core.js 顶层读PROJECT，缺了直接白屏。
    for item in ("config.js", "core.js", "clawd.js", "timeline.js",
                 "karaoke.js", "sheets.js"):
        src = tpl / "src" / item
        if src.exists():
            shutil.copy2(src, dst / "src" / item)
    # 关键：copy2 会**连权限位一起复制**。模板在 PyInstaller 解包目录里可能是
    # 只读的，复制出来的工作副本也就只读，之后写 config.js / 场景文件直接
    # Permission denied（模拟打包形态实测踩到）。所以复制完统一补上写权限。
    _ensure_writable(dst)
    _link_modules(tpl / "node_modules", dst / "node_modules")
    return dst


def _ensure_writable(root: Path) -> None:
    """给工作副本补上写权限（对已存在的目录也执行）。

    每次都跑一遍而不是只在首次复制后：用户可能把 cache/ 拷到只读位置，
    或者程序被强杀留下半成品 —— 这两种情况下重新打开也是写不进去。
    """
    for p in [root, *root.rglob("*")]:
        try:
            if p.is_dir():
                os.chmod(p, os.stat(p).st_mode | 0o200)
            elif p.is_file():
                os.chmod(p, os.stat(p).st_mode | 0o200)
        except OSError:
            # 加不了权限不是致命问题：真写失败时 render_shots 会如实报错
            pass


def _link_modules(src_mods: Path, dst_mods: Path) -> None:
    """让工作副本里的 render.mjs 能解析到 puppeteer-core。

    这一步是打包形态的**硬需求**，不是优化：render.mjs 用的是 ESM
    `import puppeteer from 'puppeteer-core'`，而 Node 的 ESM 解析只会从**入口
    文件所在目录逐级向上**找 node_modules，既不看 NODE_PATH（对 ESM 无效），
    也不看 CWD。所以工作副本里没有 node_modules 就直接 ERR_MODULE_NOT_FOUND，
    连 ffmpeg 都到不了。

    目录联接（junction / symlink）不需要管理员权限，Windows 上 mklink /J
    是最省事的做法；失败则退回复制（63 MB，慢但正确）。
    """
    if not src_mods.exists() or dst_mods.exists():
        return
    try:
        if os.name == "nt":
            # mklink /J 建目录联接：不需要 SeCreateSymbolicLinkPrivilege。
            rc = subprocess.call(["cmd", "/c", "mklink", "/J", str(dst_mods), str(src_mods)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **hidden_kwargs())
            if rc == 0 and dst_mods.exists():
                return
        else:
            dst_mods.symlink_to(src_mods, target_is_directory=True)
            return
    except (OSError, NotImplementedError):
        pass
    print(f"[painted] 建联接失败，改为复制 node_modules（{_dir_size_mb(src_mods):.0f} MB，会慢几秒）")
    shutil.copytree(src_mods, dst_mods, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(".cache"))


def _dir_size_mb(path: Path) -> float:
    total = 0
    for p in Path(path).rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total / 1024 / 1024

#:浏览器候选。puppeteer-core 不绑定 Chrome 内核，Edge 也是 Chromium，可直接顶替。
BROWSER_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
)

#:题材 → 引擎调色板。
#: 三层远景 + 前景 + 主体色各自独立，**不要**都往天空色上混——混多了会糊成一片，
#: 大气透视过头反而丢掉层次（实测踩过：三层山全糊成同一个橙色）。这里的 far/near
#: 已经预混好空气透视，绘制时不要再mixCol。
GENRE_PALETTES: dict[str, dict[str, Any]] = {
    # 天空 / 远山 / 中山 / 近山 / 前景 / 主体 / 高光
    "烟火美食": {"sky": "#F6D9B0", "far": "#E8A87C", "mid": "#C97B52", "near": "#8C4A32",
                 "fore": "#4A2418", "prop": "#E2703A", "glow": "#FFD36E"},
    "城市宣传": {"sky": "#BFE0F2", "far": "#7FB3CE", "mid": "#4A87A8", "near": "#2C5470",
                 "fore": "#17303F", "prop": "#3A9C98", "glow": "#FFF5E2"},
    "人物故事": {"sky": "#FBE0C8", "far": "#E9A98F", "mid": "#C4708A", "near": "#7A4A78",
                 "fore": "#3D2440", "prop": "#E27A92", "glow": "#FFD9A0"},
    "产品商业": {"sky": "#E4EEF8", "far": "#A8C4E0", "mid": "#5C82B4", "near": "#2E4A72",
                 "fore": "#182438", "prop": "#378ADD", "glow": "#FFF5E2"},
    "国风古建": {"sky": "#F2EBC8", "far": "#CFD9A0", "mid": "#93AE6A", "near": "#55713F",
                 "fore": "#2C3A20", "prop": "#6E9F58", "glow": "#E8C33A"},
}
DEFAULT_PALETTE = GENRE_PALETTES["城市宣传"]


class PaintedError(RuntimeError):
    """代码绘制后端不可用或渲染失败。"""


@dataclass
class PaintedStatus:
    """体检结果，供 GUI 三态展示（就绪 / 环境受限 / 需修复）。"""

    ok: bool
    detail: str
    node: str = ""
    browser: str = ""
    template: bool = False
    deps: bool = False


def find_browser(explicit: str = "") -> str:
    """找一个可用的 Chromium 内核浏览器。先看显式传入/环境变量，再看常见安装位置。"""
    for cand in (explicit, os.environ.get("CHROME_PATH", ""), *BROWSER_CANDIDATES):
        if cand and Path(cand).exists():
            return cand
    return ""


def _node_exe() -> str:
    """定位node。优先项目 bin/ 下随包携带的，其次 PATH。"""
    bundled = ROOT / "bin" / "node.exe"
    if bundled.exists():
        return str(bundled)
    found = shutil.which("node")
    if found:
        return found
    raise PaintedError(
        "未找到 Node.js。代码绘制需要 Node ≥ 18（用于跑p5.js 渲染引擎）。\n"
        "  解决：安装 Node.js 后重试，或在设置页不勾选「代码绘制」，"
        "改用 Pillow / ComfyUI 后端。"
    )


def check_env() -> PaintedStatus:
    """体检：Node / 浏览器 / 模板 / 依赖是否齐备。任一缺失都不抛异常，只如实报告。"""
    st = PaintedStatus(ok=False, detail="")

    tpl = template_dir()
    st.template = tpl.exists() and (tpl / "studio.html").exists()
    if not st.template:
        st.detail = f"缺少渲染模板（应在 {tpl}）。请重新分发或运行 build_web.py 重新打包。"
        return st

    st.node = _node_exe()
    if not st.browser:
        st.browser = find_browser()
    if not st.browser:
        st.detail = (
            "未找到 Chrome / Edge。代码绘制需要 Chromium 内核浏览器来渲染 p5.js 画面。\n"
            "  解决：安装 Chrome 或 Edge（Windows 自带 Edge 即可），"
            "或设置环境变量 CHROME_PATH 指向浏览器可执行文件。"
        )
        return st

    st.deps = (tpl / "node_modules").exists()
    if not st.deps:
        st.detail = (
            "渲染依赖未安装（p5.js / puppeteer-core）。\n"
            f"  解决：在 {tpl} 下执行 npm install"
        )
        return st

    st.ok = True
    st.detail = f"就绪 · Node {Path(st.node).name} · {Path(st.browser).name}"
    return st


def _js_str(value: Any) -> str:
    """把 Python 值安全地转成 JS 字面量（中文与引号都要正确转义）。"""
    return json.dumps(str(value), ensure_ascii=False)


def _js_num(value: Any, default: float = 0.0) -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        f = default
    if f != f or f in (float("inf"), float("-inf")):  # NaN / inf
        f = default
    return repr(round(f, 4))


def build_scene_js(
    shots: list[dict[str, Any]],
    *,
    genre_label: str = "",
    fps: int = 24,
    captions: bool = True,
) -> str:
    """把分镜数据编译成引擎场景 JS。

    这是整个后端的核心：项目里 ``Shot`` 已经有 narration / visual / camera /
    duration / act这些字段，这里把它们映射成引擎能画的镜头表。生成物是纯函数式的
    ``shot_N(t, lt, dur)``，符合引擎"每帧是t 的纯函数"的硬约束。

    每个镜头画出：天空渐变 → 远山/桌面 → 主体剪影 → 前景装饰 → 可选字幕板。
    主体用引擎的 ``clawd()``（它保证角色跨镜头造型一致，正是这个引擎的核心卖点）。
    """
    pal = GENRE_PALETTES.get(genre_label, DEFAULT_PALETTE)
    total = sum(float(s.get("duration") or 0.0) for s in shots) or 15.0
    # 时长对齐到 0.5 秒，避免最后一帧时长为 0
    total = max(3.0, round(total * 2) / 2)

    parts: list[str] = []
    parts.append("// 由 pipeline/painted.py 自动生成 —— 请勿手工编辑。")
    parts.append(f"// 题材: {genre_label or '未分类'} / 镜头数: {len(shots)} / 总时长: {total}s")
    # 注意：PROJECT 定义在生成的 config.js 里（引擎在 core.js 顶层读取它，
    # 这里再定义一次会和config.js 冲突，且时序上无法保证谁先执行）。
    parts.append("const SKY = %s, FAR = %s, MID = %s, NEAR = %s, FORE = %s, PROP = %s, HOT = %s;" % (
        _js_str(pal["sky"]), _js_str(pal["far"]), _js_str(pal["mid"]), _js_str(pal["near"]),
        _js_str(pal["fore"]), _js_str(pal["prop"]), _js_str(pal["glow"])))
    # 单位换算：场景里有一批"按 1080 标定"的**绝对像素尺寸**（角色身量、草叶大小、
    # 字幕字号、阴影偏移）。它们不能乘 W 或 H——那些是**相对比例**，而这些是绝对尺寸。
    #
    # 基准取**短边**而不是高：三个受支持的画幅（1920×1080 / 1080×1920 / 1080×1080）
    # 短边都是 1080，所以 U 恒为 1、输出与改造前完全一致；换到 720p/1440p 时才会
    # 按比例缩放。曾按 H 归一（竖屏 U=1.78），实测把角色放大到撑满画面宽度并被右缘裁切。
    parts.append("const U = Math.min(W, H) / 1080;   // 短边 1080 基准的绝对像素换算")

    parts.append(SCENE_HELPERS_JS)

    table: list[str] = []
    cursor = 0.0
    for idx, shot in enumerate(shots):
        dur = float(shot.get("duration") or 0.0)
        if dur <= 0:
            dur = total / max(1, len(shots))
        # 机位：把中文机位名映射成缩放/位移，让每个镜头真的有区别
        zoom, cx, cy = _camera_for(shot.get("camera") or "", idx, len(shots))
        mood = _mood_for(shot.get("act") or "", idx)
        narr = caption_of(shot) if captions else ""
        parts.append(_build_shot_fn(idx, dur, zoom, cx, cy, mood, narr, pal))
        table.append("  [%s, shot%d]" % (_js_num(cursor), idx))
        cursor += dur

    parts.append("shots([%s]);" % ", ".join(table))
    parts.append("// 结尾轻微暗角收束，避免最后一帧戛然而止")
    # 结尾收束。半径上限必须夹住：iris() 会把半径外的区域填成墨色，
    # 增长过快时最后一帧整个画面被吞成一条窄带（实测踩过：* 2600 时
    # 末帧黑边吃掉两侧三成画面）。这里上限取 H*0.62，且只在最后 0.25 秒收。
    parts.append(
        "const _lastEnd = %s;" % _js_num(cursor)
    )
    return "\n\n".join(parts)


#: 估计不出节拍时给引擎的默认速度。引擎用 BEAT = 60/BPM 驱动角色呼吸与舞步，
#: bpm 传 0 会让 BEAT 变成 Infinity，所以必须兜一个正值。
DEFAULT_BPM = 96.0

#: 画幅 → 画布尺寸。**必须与 image_render.RESOLUTIONS 一致**（有测试盯着）——
#: 两处不一致会让成片画幅与占位帧画幅对不上。
ASPECT_SIZES: dict[str, tuple[int, int]] = {
    "16:9": (1920, 1080),
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
}
DEFAULT_SIZE = ASPECT_SIZES["16:9"]


def size_for_aspect(aspect: str) -> tuple[int, int]:
    """把画幅字符串翻译成画布尺寸（未知画幅回落 16:9，与简报层的兜底一致）。"""
    return ASPECT_SIZES.get(str(aspect or "").strip(), DEFAULT_SIZE)

#: 渲染并发页面上限。**不要按 CPU 核数放大**：实测（22 逻辑核机器）
#: workers 1→8 吞吐只从 0.75 涨到 0.90 fps（1.20×，瓶颈在集显 WebGL 光栅化），
#: 但单次并发 ≥2 个 page 时 25 次调用里崩 3 次（12%，Navigation timeout）。
#: 所以并发越多越容易崩、还不更快——焊死在 4。
MAX_WORKERS = 4
#: 渲染失败后的重试次数。render.mjs 会跳过已渲好的帧，所以重试是**续渲**，
#: 不是从头来；一次导航超时（30 秒）也远小于一次完整渲染。
RENDER_RETRIES = 3
#: 单帧渲染耗时的**预算上限**（实测 1.0-1.8 s/帧，留 2 倍余量）。
#: 总超时按帧数估：写死 1800s 时，3 分钟片（约 4320 帧）必然被杀在半路。
SECONDS_PER_FRAME_BUDGET = 4
#: 总超时的下限（短片不该因为帧数少而给一个很紧的超时）
MIN_RENDER_TIMEOUT = 1800
#: **空闲超时**：多久没有新输出就判定卡死。render.mjs 每 24 帧打印一次
#: ms/frame，按 1.8 s/帧算是 43 秒一次，600 秒有 14 倍余量。
RENDER_IDLE_TIMEOUT = 600.0

#: 启动宽限：子进程起来后到吐出第一行之前，不按"空闲"计时（秒）。
#:
#: 渲染进程的冷启动不是瞬间的——加载解释器、跑 Node、拉起无头浏览器，
#: 这台机器实测 4.7 秒才有第一行输出。空闲超时若从 Popen 就开始计，
#: 慢机器上会误杀一个正在正常启动的进程，而且报的是"超时"，
#: 排查方向会整个跑偏。宽限只作用于第一行之前，之后立刻回到正常判定。
STARTUP_GRACE = 8.0

#: 水彩画质档位 → 引擎系数（写进 quality.js，core.js 顶层读取）。
#:
#: **为什么需要档位**：逐帧渲染的成本大头在水彩纸纹（p5.brush 的 fillTexture
#: 是逐像素模拟）。实测（1920×1080、无头 Edge、RTX 4060 Laptop，同进程内切档，
#: 排除页面加载与机器抖动）：
#:     high   754 ms/帧
#:     medium 695 ms/帧  （快约 8%）
#:     fast   544 ms/帧  （快约 28%）
#: 即**降档最多快 1.4 倍**，别指望它解决"太慢"，但它是目前唯一有效的提速手段
#: （加线程几乎无效：1→8 页吞吐只从 0.81 涨到 1.16 fps）。
#:
#: 注：队友机器（Intel Arc 集显）测得 1.0-1.8 s/帧，绝对值因硬件而异，
#: 但"纸纹占大头、并发收益小"这个结构性结论两边一致。
QUALITY_PRESETS: dict[str, dict[str, float]] = {
    "high":   {"texScale": 1.0, "bleedScale": 1.0},
    "medium": {"texScale": 0.5, "bleedScale": 0.7},
    "fast":   {"texScale": 0.0, "bleedScale": 0.0},
}
DEFAULT_QUALITY = "medium"


def quality_js(quality: str) -> str:
    """生成 quality.js（引擎读 window.QUALITY 决定纸纹/渗色强度）。"""
    q = QUALITY_PRESETS.get(str(quality or ""), QUALITY_PRESETS[DEFAULT_QUALITY])
    return (
        "// 由 pipeline/painted.py 自动生成，不要手改\n"
        f"window.QUALITY = {{ texScale: {q['texScale']}, bleedScale: {q['bleedScale']} }};\n"
    )


def render_timeout_for(frames: int) -> int:
    """按帧数估算渲染总超时。"""
    return max(MIN_RENDER_TIMEOUT, int(max(0, frames)) * SECONDS_PER_FRAME_BUDGET + 120)


def default_workers() -> int:
    """默认并发页面数：够用就好，不随核数放大（原因见 MAX_WORKERS）。"""
    return max(2, min(MAX_WORKERS, (os.cpu_count() or 4) // 2))


def render_with_retry(run_once: Any, attempts: int = RENDER_RETRIES, say: Any = None) -> tuple[int, str]:
    """跑一次渲染，失败就重试。返回最后一次的 (退出码, 输出)。

    为什么必须重试：并发开页面会随机 `Navigation timeout`（实测 12%），
    而引擎的 ``--frames`` 本身支持断点续渲（跳过已存在的帧），
    所以重试的代价只是重开浏览器、不会重渲已完成的帧。
    不做重试的话，一次随机超时就白白废掉整段渲染。
    """
    code, output = 1, ""
    for i in range(1, max(1, int(attempts)) + 1):
        code, output = run_once()
        if code == 0:
            return code, output
        if i < attempts and callable(say):
            say(f"渲染第 {i} 次失败（退出码 {code}），重试——已渲的帧会保留，不从头来…")
    return code, output


def build_config_js(
    duration: float,
    fps: int = 24,
    beat_bpm: float = 0.0,
    beat_offset: float = 0.0,
    size: tuple[int, int] = (1920, 1080),
) -> str:
    """生成 config.js。PROJECT 必须单独成文件——引擎在 core.js 顶层读它。

    ``PROJECT.bpm`` / ``offset`` **真的会被引擎消费**（core.js:4 → BEAT/bpOf/pulse，
    驱动角色呼吸、31 种情绪的 body 运动、13 种舞步）。此前这里是死参数：
    管线从不传，bpm 恒 96.0、offset 恒 0，于是"引擎有节拍、数据是假的"。

    ``PROJECT.width`` / ``height``：画幅。core.js 原来是写死的
    ``const W = 1920, H = 1080``，所以竖屏出不来；现在由这里下发。
    """
    bpm = float(beat_bpm) if float(beat_bpm or 0.0) > 0 else DEFAULT_BPM
    w, h = int(size[0]), int(size[1])
    return (
        "// 由 pipeline/painted.py 自动生成\n"
        f"const PROJECT = {{ duration: {_js_num(duration)}, bpm: {_js_num(bpm)}, "
        f"offset: {_js_num(max(0.0, float(beat_offset or 0.0)))}, "
        f"width: {w}, height: {h} }};\n"
        f"const FPS = {int(fps)};\n"
    )


def build_lyrics_js(lines: list[dict[str, Any]] | None) -> str:
    """生成 src/lyrics.js：把歌词按引擎要的格式注入 ``window.KARAOKE_LY``。

    引擎的格式是 ``[[start, end, "词", opts?]]``，时间是**片子时间**。
    MV 模式下片子时间 == 歌曲时间（时间轴按音频时长对齐），所以歌词时间可直接用。

    必须**先于** karaoke.js 加载：karaoke.js 的 ``window.EXTRA_FONTS`` 在脚本加载时
    就按歌词字符算好了字体预载集，事后注入既改不了它的 ``const LY``、也来不及预载字体。
    """
    rows: list[str] = []
    for ln in lines or []:
        text = str(ln.get("text") or "").strip()
        try:
            a = float(ln.get("start") or 0.0)
            b = float(ln.get("end") or 0.0)
        except (TypeError, ValueError):
            continue
        if not text or b <= a:
            continue
        rows.append(f"  [{_js_num(a)}, {_js_num(b)}, {_js_str(text)}],")
    body = "\n".join(rows)
    return (
        "// 由 pipeline/painted.py 自动生成 —— 请勿手工编辑\n"
        "// 引擎按 [[start, end, \"词\", opts?]] 消费；时间单位是秒（片子时间）。\n"
        f"window.KARAOKE_LY = [\n{body}\n];\n"
    )


def build_studio_html(
    scene_file: str,
    modules_prefix: str = "node_modules",
    karaoke: bool = False,
    size: tuple[int, int] = (1920, 1080),
) -> str:
    """生成一个不依赖 demo 场景的 studio 页面。

    与上游 studio.html 的两处必要差异：
    1. 去掉 Google Fonts 外链——那会让离线环境卡在字体加载上；同时补一个本地中文字体栈，
       否则中文标题会掉成方块。原引擎 fonts.load 只load 了西文字体，中文本来就靠回退。
    2. 只加载我们生成的那个场景文件，不加载 demo.js。

    ``karaoke=True`` 时额外加载 ``src/lyrics.js`` → ``src/karaoke.js``（逐字上屏）。
    顺序不能反：lyrics.js 先定义 ``window.KARAOKE_LY``，karaoke.js 才读得到，
    并且它的字体预载集是在加载时按歌词算的。

    modules_prefix：p5 / p5.brush 的加载前缀。打包成 EXE 后工作副本里没有
    node_modules（63 MB，不复制），只能指回模板目录的绝对路径——所以这个参数
    必须是绝对路径（已把反斜杠转成 /）。曾在这里踩坑：work_dir() 把 studio.html
    改成绝对路径，但渲染时又用本函数整体覆盖 studio.html，把改写冲掉了，
    于是 EXE 下p5 加载失败、控制台一片空白。
    """
    karaoke_tags = ""
    if karaoke:
        karaoke_tags = (
            '<script src="src/lyrics.js"></script>\n'
            '<script src="src/karaoke.js"></script>\n'
        )
    # 输出画布尺寸必须与 core.js 的 W/H 一致：composite() 把 p5 画布
    # drawImage 到这张 out 画布上，尺寸不一致会拉伸或裁切。
    w, h = int(size[0]), int(size[1])
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>YiYan painted studio</title>
<style>
  html, body {{ margin: 0; background: #1b1820; color: #eee; font: 13px system-ui, sans-serif; }}
  main {{ padding: 12px; display: grid; gap: 8px; }}
  #out {{ width: min(100%, 1280px); aspect-ratio: {w} / {h}; height: auto; background: #F3EBDC; }}
  .p5Canvas {{ display: none !important; }}
</style>
<script src="{modules_prefix}/p5/lib/p5.min.js"></script>
<script src="{modules_prefix}/p5.brush/dist/p5.brush.js"></script>
</head>
<body>
<main>
  <canvas id="out" width="{w}" height="{h}"></canvas>
</main>
<script src="src/config.js"></script>
<!-- quality.js 必须在 core.js **之前**：core.js 顶层就读 window.QUALITY
     算出纸纹系数，加载晚了会退化成"系数 undefined → 全画质"，
     表现是选了「快」却没变快。 -->
<script src="src/quality.js"></script>
<script src="src/core.js"></script>
<script src="src/clawd.js"></script>
<script src="src/timeline.js"></script>
{karaoke_tags}<script src="{scene_file}"></script>
</body>
</html>
"""


def _camera_for(camera: str, idx: int, total: int) -> tuple[float, float, float]:
    """把机位名映射成 ``(缩放, 相机中心 x 比例, 相机中心 y 比例)``，并按镜头序号做小幅位移。

    **后两个返回值是归一化比例（0-1），不是像素**。这是竖屏支持的关键：原来这里
    写死 ``w, h = 1920.0, 1080.0`` 并把像素中心（960 / 540）编进场景 JS，
    于是画幅一换成竖屏，相机中心就跑到画面外。改成比例之后，生成的 JS 用引擎的
    ``W`` / ``H`` 全局自己算，横屏竖屏都成立。

    真实项目里的机位名是「缓慢推镜，中景」这类**复合描述**（见 Shot.camera），
    不是干净的「中景」。所以这里用子串包含匹配：先找景别（决定缩放），
    再按运镜（推/拉/摇）叠加动态效果。没有景别信息时退回中景。

    没有机位信息时也给一个确定性的缓慢横移——因为每帧必须是纯函数，这里全部由
    镜头序号和时长算出，不依赖任何随机状态。
    """
    name = camera.strip()
    # 按景别关键词匹配（长词优先，避免「近景」被「景」误伤）
    # zoom 上限 1.55：实测 1.90 时 camBegin 的缩放会把角色推出画面下沿。
    # "特写"的感觉改由 _paintSubject 里的主体尺寸放大 + 主体居中来表达，
    # 而不是靠相机硬缩放。
    size_presets: list[tuple[str, float]] = [
        ("大特写", 1.55), ("特写", 1.48),
        ("中近景", 1.32), ("近景", 1.24),
        ("中景", 1.15),
        ("全景", 0.95), ("远景", 0.82), ("大远景", 0.70),
    ]
    zoom = 1.15
    for key, z in size_presets:
        if key in name:
            zoom = z
            break
    # 竖俯视等角度：把镜头中心上下挪（比例）
    cy = 0.50
    if "俯" in name or "俯拍" in name or "俯视" in name:
        cy = 0.60
    elif "仰" in name:
        cy = 0.38
    elif "航拍" in name or "高机位" in name:
        cy = 0.66
    elif "低机位" in name:
        cy = 0.42

    # 缓慢横移，给静态画面一点生气；镜头越靠后移动越明显。
    # 位移同样用比例表达——原先是 w*0.5 ± 130px、cy ± 40px，按 1920×1080 标定。
    drift = (idx - (total - 1) / 2) / max(1, total - 1) if total > 1 else 0.0
    cx = 0.5 + drift * (130.0 / 1920.0)
    return zoom, cx, cy + drift * (40.0 / 1080.0)


def _mood_for(act: str, idx: int) -> str:
    """按四幕（起承转合）映射角色表情，呼应引擎自带的 emotions 模型表。

    **只能用引擎 EMO 表里真实存在的键**。原来返回的 ``curious`` / ``calm``
    两个都不在表里（表里共 31 种，见 clawd.js 的 ``const EMO``），而 ``feel()``
    是 ``EMO[name] || EMO.neutral`` 的**静默回退**——四幕里两幕悄悄变成 neutral，
    画面上就是"每一镜表情都一样"。所以改用语义最接近的合法表情：
    承 → ``thinking``、合 → ``relieved``。tests/test_pipeline.py 会拿
    clawd.js 的真实键表逐个核对，防止以后再写出表外的名字。
    """
    table = {"起": "happy", "承": "thinking", "转": "surprised", "合": "relieved"}
    if act in table:
        return table[act]
    return ["happy", "thinking", "surprised", "relieved"][idx % 4]


def _caption_for(narration: str, limit: int = 24) -> str:
    """把解说词压成一句适合上屏的字幕。

    实测踩过的坑：直接 ``narration[:28]`` 会把长句砍成半句（例如"羊骨吊足八小时的
    高汤，是这里最不声张的讲究"被砍成前半截，读起来是断的）。所以这里优先在
    标点处断开；实在没有标点才硬截，并去掉尾部逗号。
    """
    text = (narration or "").strip()
    if not text:
        return ""
    text = text.split("\n")[0].strip()
    if len(text) <= limit:
        return text
    # 在limit 以内的最后一个标点处断
    window = text[: limit + 4]
    for mark in ("。", "；", "，", "、", "！", "？", "!", "?", ",", ";"):
        pos = window.rfind(mark)
        if pos >= 10:  # 太靠前的标点会截得只剩几个字，不采用
            return window[:pos].strip()
    return text[:limit].rstrip("，,、；; ")


def _build_shot_fn(
    idx: int,
    dur: float,
    zoom: float,
    cx: float,
    cy: float,
    mood: str,
    narration: str,
    pal: dict[str, Any],
) -> str:
    """生成单个镜头的绘制函数。

    结构刻意保持简单且全部由 t 推导：天空 → 三层远景 → 主体 → 前景草叶 → 字幕。
    每层都先 boilSeed 再画，保证"静止的东西每帧线条一致"（引擎的手绘沸腾机制）。

    注意：这里的 W / H / PAL 都是 **JS 侧** core.js 里的常量，不是在 Python 里算的。
    本函数只负责拼字符串，所以不要在 Python 里引用 W/H——踩过一次 NameError。
    相机中心现在传的是**比例**（见 _camera_for），在这里乘回 W/H，
    这样竖屏（1080×1920）不用改任何 Python 数值就能对。
    """
    narr_js = _js_str(_caption_for(narration)) if narration else "null"
    cam_x = f"W * {_js_num(cx)}"
    cam_y = f"H * {_js_num(cy)}"
    return f"""
// ---- 镜头 {idx + 1}（{_js_num(dur)}s，{mood}）----
function shot{idx}(t, lt, dur) {{
  boilSeed('shot{idx}cam');
  // 轻微呼吸式缩放 + 入场推近，避免画面完全静止
  const z = {zoom} * (1 - 0.03 * ease(seg(lt, 0, 1.2)));
  camBegin(kf(t, [[0, {cam_x}], [{dur}, {cam_x}]]), kf(t, [[0, {cam_y}], [{dur}, {cam_y}]]), z);
  _paintSky(t, {idx}, dur);
  _paintHills(t, {idx}, dur);
  _paintSubject(t, {idx}, dur, {mood!r}, {zoom});
  _paintProp(t, {idx}, dur, {zoom});
  _paintForeground(t, {idx}, dur);
  camEnd();
  _paintIrisOut(t);
  // 字幕画在 camEnd() **之后**：相机变换会把它一起缩放平移，特写(zoom=1.9)时
  // 底板会被推出画面下沿——实测出现过"特写镜头字幕整个消失"。字幕必须免疫相机。
  const _cap = {narr_js};
  if (_cap) _paintCaption(_cap, lt, dur);
}}
"""


#: 场景里用到的通用绘制层。抽出来是因为每个镜头结构相同，
#: 参数化后可以按镜头序号换配色与装饰密度，避免 20 个镜头长得一模一样。
SCENE_HELPERS_JS = r"""
// ---------- 通用绘制层（由 pipeline/painted.py 生成）----------
//
// 配色纪律（实测踩过的坑）：远/中/近三层各自用**预混好的独立颜色**，
// 不要在绘制时再往天空色上 mix——混多了三层会糊成同一个颜色，层次全丢。

function _paintSky(t, idx, dur) {
  boilSeed('sky' + idx);
  paint(rectPts(-600, -400, W + 1200, H + 800), { wash: SKY, ink: null });
  // 云翳：亮部只往纸白提亮，不往远山色混，保持"天比山亮"的关系
  const cloud = mixCol(SKY, '#FFF5E2', .55);
  paint(ellPts(W * (.30 + .14 * hash(idx)), H * .20, W * .38, H * .17, 26, 12),
    { fill: cloud, fillOp: 130, bleed: .26, tex: .5, ink: null });
  paint(ellPts(W * (.72 - .12 * hash(idx + 9)), H * .31, W * .42, H * .15, 24, 12),
    { fill: mixCol(SKY, '#FFF5E2', .30), fillOp: 105, bleed: .30, tex: .48, ink: null });
  // 一轮暖光，给画面一个视觉锚点
  boilSeed('sun' + idx);
  const sx = W * (.16 + .10 * hash(idx * 3)), sy = H * .17;
  glow(sx, sy, 240 + 30 * wob(t, .2, idx), HOT, .30);
  paint(ellPts(sx, sy, 74, 74, 26, 1.5), { wash: mixCol(HOT, '#FFF5E2', .55), fillOp: 190, ink: null });
}

function _paintHills(t, idx, dur) {
  // 三层远景。k 是地平线高度，越靠前越低；颜色已预配好空气透视。
  const layers = [
    { k: .46, col: FAR, rx: W * 1.02, ry: H * .26, ink: .5 },
    { k: .60, col: MID, rx: W * .94, ry: H * .24, ink: .8 },
    { k: .75, col: NEAR, rx: W * .84, ry: H * .22, ink: 1.1 },
  ];
  for (let li = 0; li < layers.length; li++) {
    const L = layers[li];
    boilSeed('hill' + idx + '_' + li);
    // 地平线做极缓慢的呼吸，让静止画面"活"起来（幅度只 6px，不至于看出是动效）
    const breath = Math.sin((t + li * 2.1 + idx) * .42) * 6;
    const cy = H * L.k + breath;
    // 每层的山脊左右错开，避免三层同形
    const skew = (li - 1) * W * .11;
    paint(ellPts(W * .5 + skew, cy, L.rx, L.ry, 46, 3),
      { wash: L.col, fill: L.col, fillOp: 205, bleed: .05, tex: .66, ink: PAL.ink, sw: L.ink });
    // 山脊墨线草：越近越高越密，强化景深
    const tufts = (4 + li * 5);
    for (let i = 0; i < tufts * 3; i++) {
      const a = -Math.PI / 2 + (hash(i * 7 + li * 31 + idx) - .5) * 1.35;
      const x = W * .5 + skew + Math.cos(a) * L.rx * (.70 + .28 * hash(i + li * 13));
      const y = cy + Math.sin(a) * L.ry + 6;
      const sw = wob(t, .40, hash(i) * 3) * 7;
      const hgt = (18 + 30 * hash(i * 3 + li)) * (1 + li * .18);
      inkLine([[x, y], [x + sw * .5, y - hgt * .6], [x + sw, y - hgt]], .8,
        mixCol(L.col, '#FFF5E2', .34), 'inkfine', .45);
    }
  }
}

function _paintSubject(t, idx, dur, mood, frame) {
  // 主体：引擎自带角色，单一绘制函数保证跨镜头造型一致——这是本引擎的核心价值。
  // frame = 当前景别（0.70远景 … 1.90 特写）。景别不只靠 camBegin 的 zoom，
  // 还要真正改变主体在画面里的占比与位置——否则"特写"和"远景"看起来一样。
  boilSeed('subject' + idx);
  const kIn = ease(seg(t, 0, 1.0));
  const base = (34 + 7 * hash(idx * 5)) * U;
  // k ∈ 0..1 表示从远景到特写。实测踩过的坑：把景别系数直接线性加到 groundY 上，
  // frame=1.9 时会把主体推到 H*1.16 —— 整个人掉到画面外。所以这里先把景别
  // 归一化成 k，再让所有位移都在**安全区间内**插值。
  const k = clamp((frame - 0.70) / (1.90 - 0.70), 0, 1);
  // 上限 1.35：实测放到 1.75 时，配合 camBegin 的 zoom 会让角色被裁成"只剩上半身"，
  // 看起来像 bug。这里保留"特写更大"的效果，但不让身体出画。
  const u = base * lerp(0.85, 1.35, k);
  // 远景：主体偏左、地平线高（多留天空与山）；特写：主体居中偏上、地平线压低
  const x = lerp(W * .24, W * .52, kIn) + wob(t, .26, idx) * 12 + k * W * .10;
  // 地平线上限0.86H：再低角色就会踩到字幕带（H*0.945 上下）
  const groundY = H * lerp(.78, .86, k);
  const bob = Math.abs(Math.sin(t * 1.05 + idx)) * 6 * U;
  const sq = .07 * Math.sin(t * 2.2 + idx);
  // 主体身后一圈暖光，把角色从背景里"抠"出来（否则深色山体上会糊）
  glow(x, groundY - u * 1.5, u * 2.6, HOT, .20 + .10 * Math.sin(t * 1.6 + idx));
  // 落地阴影先画，压在脚下把角色"钉"住，避免漂浮感
  boilSeed('shadow' + idx);
  paint(ellPts(x, groundY + 12 * U, u * 1.7, u * .30, 24, 2),
    { fill: FORE, fillOp: 90, ink: null });
  // feel() 的结果必须**并进 o**（合并成一个对象）再传：clawd 的签名是
  // clawd(x, y, u, o)，只有 4 个参数——以前把 {sq, view} 当第 5 个参数传，
  // 被直接丢弃，于是机位永远 front、压扁也从来没生效（"每镜构图雷同"的直接原因）。
  clawd(x, groundY - bob, u, { ...feel(mood, t), sq, view: idx % 3 === 2 ? 'q' : 'front' });
  // 注意：本段属于 SCENE_HELPERS_JS，是**普通字符串**不是 f-string，
  // JS 的花括号必须写单层（写成 {{ }} 会原样输出成语法错误，实测踩过）。
}

function _paintForeground(t, idx, dur) {
  // 前景只做一道**浅**压边（原实测用了 150不透明度、占屏 30%，直接抢戏压住了主体）。
  // 这里的职责是"把视线收回画面中央"，不是画一片地面。
  boilSeed('fore' + idx);
  paint(ellPts(W * .5, H * 1.20, W * 1.15, H * .22, 40, 4),
    { wash: FORE, fill: FORE, fillOp: 88, bleed: .10, tex: .70, ink: null });
  const n = 3 + (idx % 3);
  for (let i = 0; i < n; i++) {
    const x = W * (.10 + .80 * hash(i * 11 + idx * 5));
    const y = H * 1.10;
    const hgt = 90 + 110 * hash(i + idx * 3);
    const sway = wob(t, .45, hash(i * 7)) * 16;
    boilSeed('blade' + idx + '_' + i);
    inkLine([[x, y], [x + sway * .4, y - hgt * .62], [x + sway, y - hgt]], 1.3,
      mixCol(FORE, PAL.cream, .22), 'ink', .5);
  }
}

function _paintProp(t, idx, dur, frame) {
  // 题材道具剪影：让每类片子一眼可辨（锅=美食、楼=城市、伞=人物、箱=产品、檐=古建）。
  // 远景时道具小而靠边，特写时放大并移出画外中心，避免和主体抢位置。
  boilSeed('prop' + idx);
  // 景别归一化。变量名不能叫 k —— 下面已有一个 const k 是道具淡入进度，
  // 同名会触发 "Identifier 'k' has already been declared"（踩过一次）。
  const fk = clamp((frame - 0.70) / (1.90 - 0.70), 0, 1);
  const u = 30 * lerp(0.70, 1.45, fk) * U;
  // 特写时道具退到画面右缘外，避免和被放大的主体抢视觉重心
  const x = W * lerp(.80, .92, fk), y = H * lerp(.80, .90, fk);
  const k = ease(seg(t, .3, 1.2));          // 道具淡入，和主体入场错开
  if (k < .02) return;
  const a = .55 + .45 * Math.sin(t * .8 + idx) * .12 + .45 * k;
  push(); translate(x, y);
  if (idx % 5 === 0) {                       // 烟火美食：灶台 + 升腾热气
    paint(ellPts(0, -u * .2, u * 1.5, u * .55, 26, 2), { fill: FORE, fillOp: 210 * a, ink: PAL.ink, sw: 1 });
    paint(ellPts(0, -u * .2, u * 1.2, u * .40, 22, 2), { wash: HOT, fillOp: 120 * a, ink: null });
    for (let i = 0; i < 3; i++) {
      boilSeed('steam' + idx + i);
      const ph = (t * .5 + i * .33) % 1;
      const sy = -u * .6 - ph * u * 2.2;
      const sx = wob(t, .7, i * 2) * 16;
      inkLine([[sx, sy], [sx + 6, sy - u * .5], [sx - 4, sy - u]], 2.4,
        mixCol('#FFF5E2', HOT, .4), 'inkfine', .6);
    }
  } else if (idx % 5 === 1) {                // 城市宣传：楼群剪影
    for (let i = 0; i < 4; i++) {
      const bw = u * (.5 + .5 * hash(i + idx)), bh = u * (1.2 + 2.4 * hash(i * 3 + idx));
      paint(rectPts(-u * 1.5 + i * u * .78, -bh, bw, bh, 1),
        { fill: FORE, fillOp: 195 * a, ink: PAL.ink, sw: .8 });
      for (let r = 1; r < Math.floor(bh / (u * .34)); r++)
        for (let c = 0; c < 2; c++)
          if (hash(i * 31 + r * 7 + c) > .45)
            paint(rectPts(-u * 1.5 + i * u * .78 + u * .12 + c * u * .28, -bh + r * u * .34, u * .13, u * .17, 0),
              { fill: HOT, fillOp: 150 * a, ink: null });
    }
  } else if (idx % 5 === 2) {                // 人物故事：一把伞 + 一位远景人影
    paint(ellPts(-u * 1.4, -u * 1.5, u * .9, u * .85, 24, 1.5),
      { fill: PROP, fillOp: 200 * a, ink: PAL.ink, sw: 1 });
    inkLine([[-u * 1.4, -u * .7], [-u * 1.4, 0]], 2.0, FORE, 'ink', .3);
  } else if (idx % 5 === 3) {                // 产品商业：一块展台 + 高光
    paint(rrPts(-u * 1.6, -u * .9, u * 2.2, u * .9, u * .12),
      { fill: FORE, fillOp: 205 * a, ink: PAL.ink, sw: 1 });
    paint(rrPts(-u * 1.35, -u * .68, u * 1.7, u * .5, u * .08),
      { wash: HOT, fillOp: 130 * a, ink: null });
  } else {                                   // 国风古建：飞檐一角
    paint(ellPts(0, -u * .5, u * 2.0, u * .42, 28, 2), { fill: FORE, fillOp: 205 * a, ink: PAL.ink, sw: 1.2 });
    paint(ellPts(-u * 1.8, -u * .34, u * .5, u * .30, 18, 1), { fill: FORE, fillOp: 205 * a, ink: PAL.ink, sw: 1 });
    paint(ellPts(u * 1.8, -u * .34, u * .5, u * .30, 18, 1), { fill: FORE, fillOp: 205 * a, ink: PAL.ink, sw: 1 });
    paint(rectPts(-u * .1, -u * 2.0, u * .2, u * .6, 0), { fill: FORE, fillOp: 190 * a, ink: null });
  }
  pop();
}

function _paintIrisOut(t) {
  // 结尾收束用**矩形**而不是引擎自带的 iris()。
  // iris() 是圆形遮罩，而本片是 16:9 横屏——圆半径一旦超过 H/2，垂直方向就被填满，
  // 最后一帧整个画面只剩中央一个小圆（实测踩过）。所以这里改为四边向内收的
  // 矩形遮罩，形状与画幅一致，收起来不会切掉画面两侧。
  const k = seg(t, _lastEnd - 0.30, _lastEnd);
  if (k <= 0) return;
  const e = easeIn(k);
  const bw = W * (1 - e), bh = H * (1 - e);
  if (e >= 0.999) { paint(rectPts(-60, -60, W + 120, H + 120), { wash: PAL.ink, ink: null }); return; }
  const x0 = (W - bw) / 2, y0 = (H - bh) / 2;
  const th = Math.max(4, e * H);
  // 四条边
  paint(rectPts(x0 - th, -60, th, H + 120), { wash: PAL.ink, ink: null });
  paint(rectPts(x0 + bw, -60, th, H + 120), { wash: PAL.ink, ink: null });
  paint(rectPts(x0 - th, y0 - th, bw + th * 2, th), { wash: PAL.ink, ink: null });
  paint(rectPts(x0 - th, y0 + bh, bw + th * 2, th), { wash: PAL.ink, ink: null });
}

function _paintCaption(txt, lt, dur) {
  // 字幕板：贴底部，字随入场淡入。中文字体走引擎回退，实测可正常显示。
  // 长句自动降字号并加宽底板——实测28 字的长句在固定 38px + 1400宽下会被截断。
  boilSeed('capplate');
  // 淡入窗口要短：实测 0.15~0.55 在低帧率下会让镜头前 1/3 完全没字幕。
  const a = ease(seg(lt, .05, .30));
  // y 用 0.90H 而不是 0.945H：底部那道前景压边（FORE 椭圆，中心在 1.20H）
  // 会盖住太靠下的字幕——实测 0.945 时字幕只剩一条白边，整句字看不见。
  const y = H * .90;
  const size = (txt.length > 22 ? 30 : txt.length > 16 ? 34 : 38) * U;
  const bw = Math.min(W * .92, txt.length * size * 1.05 + 120 * U);
  // 先铺一层深色底再铺纸色，保证任何背景上都读得清（实测橙色山体上浅色底板不够）
  paint(rrPts(W * .5 - bw / 2 - 8, y - size * .86, bw + 16, size * 1.72, 10),
    { fill: PAL.ink, fillOp: 90 * a, ink: null });
  paint(rrPts(W * .5 - bw / 2, y - size * .78, bw, size * 1.56, 8),
    { fill: '#FFF5E2', fillOp: 240 * a, ink: PAL.ink, sw: .6 });
  letter(txt, W * .5, y, size, '#2B2233', { alpha: a, bold: true });
}
"""

# ---------------------------------------------------------------- 渲染驱动

def _kill_process_tree(pid: int) -> int:
    """连同子孙进程一起结束某个进程，返回清理掉的进程数。

    为什么必须有：``subprocess.kill()`` 只终止**直接子进程**。渲染命令是
    ``node render.mjs``，而真正耗 CPU 的是它 launch 出来的 msedge.exe——
    杀 node 不会带走浏览器，孤儿进程会一直挂在后台抢 CPU（实测单个残留
    实例累计吃掉 385 秒 CPU），让全机变慢且找不到原因。

    Windows 用 ``taskkill /T /F``（自带、按父子关系整棵树杀）；
    非 Windows 退化为 process group kill。
    """
    if pid <= 0:
        return 0
    if os.name == "nt":
        try:
            r = run_hidden(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True, timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return 0 if r.returncode == 0 else 1
        except (OSError, subprocess.SubprocessError):
            return 0
    try:
        os.killpg(os.getpgid(pid), 9)
        return 1
    except (OSError, AttributeError):
        return 0


def _headless_browser_pids() -> list[tuple[int, str]]:
    """列出浏览器进程 (pid, 命令行)。

    用 PowerShell 的 CIM 而不是 WMIC：Windows 11 已把 WMIC 标记为弃用，
    新版/精简系统上直接 FileNotFoundError，那会让清场功能静默失效。
    """
    if os.name != "nt":
        return []
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' or "
        "Name='chrome.exe'\" | Select-Object ProcessId,CommandLine | "
        "ConvertTo-Json -Compress"
    )
    try:
        r = run_hidden(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, timeout=45, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return []
    txt = (r.stdout or "").strip()
    if not txt:
        return []
    try:
        data = json.loads(txt)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]
    out: list[tuple[int, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        pid = item.get("ProcessId")
        cmd = str(item.get("CommandLine") or "")
        if isinstance(pid, int) and pid > 0:
            out.append((pid, cmd))
    return out


def purge_stale_browsers(exclude_pids: set[int] | None = None) -> int:
    """清掉渲染中断留下的**孤儿**无头浏览器。

    识别条件是**三条同时成立**，缺一不可：
      1. 命令行含 ``--headless`` 或 ``--remote-debugging-port``
      2. user-data-dir 指向本项目缓存目录（含 ``painted``）
      3. 不在 ``exclude_pids`` 里

    为什么不能只看 ``--headless``：Windows 自带 Edge 的常驻后台进程也是
    msedge.exe（``--no-startup-window`` / ``--type=gpu-process``），挂在
    explorer 下、共用用户自己的 ``Edge\\User Data``。曾按"命令行含 painted
    就杀"去清，差点误杀用户正在用的浏览器。真正的判据是 **user-data-dir
    归属**——渲染用 puppeteer 自建的临时 profile，跟日常 profile 不是同一个。
    """
    keep = exclude_pids or set()
    killed = 0
    for pid, cmd in _headless_browser_pids():
        low = cmd.lower()
        if "--headless" not in low and "--remote-debugging-port" not in low:
            continue
        if "painted" not in low:
            continue
        if pid in keep:
            continue
        try:
            run_hidden(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            killed += 1
        except (OSError, subprocess.SubprocessError):
            pass
    return killed


def _run(cmd: list[str], cwd: Path, timeout: int = 900) -> tuple[int, str]:
    """执行外部命令，返回 (退出码, 合并输出)。

    Windows 下用 CREATE_NO_WINDOW，否则每个 node/ffmpeg 子进程都会弹黑框。
    编码强制 utf-8 errors=replace：子进程输出中文时若用gbk 解码会抛异常，
    这里踩过一次——报错本身把真正的错误信息吃掉了。
    """
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        p = run_hidden(
            cmd, cwd=str(cwd), capture_output=True, timeout=timeout,
            encoding="utf-8", errors="replace", **kwargs,
        )
    except subprocess.TimeoutExpired:
        return 124, f"{Path(cmd[0]).name} 超时（>{timeout}s）"
    except FileNotFoundError as exc:
        return 127, f"找不到可执行文件：{cmd[0]}（{exc}）"
    return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()


def _run_stream(cmd: list[str], cwd: Path, timeout: int = 900,
                idle_timeout: float = RENDER_IDLE_TIMEOUT) -> tuple[int, str]:
    """执行长任务并把输出**实时**转发到 log，最后仍返回合并文本。

    渲染 15 秒片要几分钟，如果用 ``_run`` 那种 capture_output，用户界面会一直
    停着不动、看不到"渲到第几帧"。所以这里逐行读并回调，让进度可见。
    仍然强制 utf-8 errors=replace——子进程中文输出用gbk 解码会抛异常，
    而那个异常会把真正的错误信息吃掉。

    **空闲超时是必须的**：原来只有读流循环**之后**的 ``proc.wait(timeout=)``，
    而渲染中途真卡住时 stdout 不会关闭，``for line in proc.stdout`` 会永远阻塞，
    那个超时根本轮不到执行——表现就是管线无声挂死。所以读流挪到子线程，
    主线程按"多久没有新输出"判卡死；总超时只当兜底。
    """
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            encoding="utf-8", errors="replace", bufsize=1, **kwargs,
        )
    except FileNotFoundError as exc:
        return 127, f"找不到可执行文件：{cmd[0]}（{exc}）"

    collected: list[str] = []
    lines: "queue.Queue[str | None]" = queue.Queue()

    def _reader() -> None:
        try:
            assert proc.stdout is not None
            for ln in proc.stdout:
                lines.put(ln.rstrip())
        except Exception:  # noqa: BLE001
            pass
        finally:
            lines.put(None)          # 结束哨兵

    threading.Thread(target=_reader, daemon=True).start()
    deadline = time.monotonic() + max(1, timeout)
    try:
        while True:
            left = deadline - time.monotonic()
            # 还没吐出第一行 = 仍在启动，不能按"空闲"算。
            # 冷启动要加载解释器、跑 Node、再拉起无头浏览器，这台机器实测
            # 4.7 秒才开始有输出；若把这段计入空闲超时，慢机器（冷缓存、
            # 杀软扫描）上会稳定误杀一个正在正常启动的渲染进程，
            # 报出来的还是"超时"，排查方向整个是错的。
            idle = idle_timeout if collected else max(idle_timeout, STARTUP_GRACE)
            try:
                line = lines.get(timeout=max(0.1, min(idle, left)))
            except queue.Empty:
                reason = (f"总时长超过 {timeout}s" if left <= 0
                          else f"连续 {idle:.0f}s 没有任何输出")
                proc.kill()
                # 只杀 node 不够：它 launch 的无头浏览器会变孤儿并持续吃 CPU
                _kill_process_tree(proc.pid)
                return 124, (f"{Path(cmd[0]).name} 超时（{reason}）\n"
                             + "\n".join(collected[-20:]))
            if line is None:
                break
            collected.append(line)
            if _progress_sink is not None and line:
                _progress_sink(line)
        proc.wait(timeout=10)
        # 正常完成也清一次残留：render.mjs 末尾虽有 browser.close()，但 Edge 上
        # 偶发"关了主进程留子进程"，实测残留实例能累计吃掉几百秒 CPU。
        _kill_process_tree(proc.pid)
        return proc.returncode, "\n".join(collected).strip()
    finally:
        if proc.stdout is not None:
            proc.stdout.close()


def ms_per_frame_hint(output: str) -> int:
    """从 node 的进度输出里解析 ms/frame。解析不到就返回 0，不影响主流程。"""
    for line in reversed(output.splitlines()):
        if "ms/frame" in line:
            try:
                return int(line.split("ms/frame")[0].strip().split()[-1])
            except (ValueError, IndexError):
                return 0
    return 0


#: 渲染过程中的行输出回调。由 render_shots 调用方设置，避免日志把函数签名搞长。
_progress_sink: Any = None

#: 帧缓存签名文件名。不叫 f*.jpg，所以不会被引擎的帧计数与清理误伤。
FRAMES_KEY_FILE = ".render_key"


def frames_cache_key(scene_js: str, config_js: str, fps: int, extra: str = "") -> str:
    """帧缓存的签名：同一支片 → 同一签名；换了分镜/时长/帧率 → 不同签名。

    为什么需要它：引擎（render.mjs:122）只在「文件存在且 >1000 字节」时跳过渲染，
    所以上一次渲染残留的旧帧会被**静默当成新帧复用**。实测踩过——请求 15 秒的片子
    却产出 18 秒，画面还是上一次的，两个验证脚本都因此"假通过"。

    但又不能无脑清空：长片渲染动辄几十分钟，断点续渲是刚需。用签名区分二者：
    签名相同 = 同一次渲染，保留已渲帧继续；签名不同 = 换了片子，先清空再渲。

    ``extra``：不进场景 JS 但会改变画面的东西（目前是歌词）。
    开了逐字卡拉OK 之后场景 JS 里已经没有字幕了，只换歌词时场景 JS 完全一样——
    不把歌词纳入签名就会复用旧帧，画面上还是上一版的词。
    """
    h = hashlib.sha256()
    h.update(f"fps={int(fps)}\n".encode("utf-8"))
    h.update(scene_js.encode("utf-8"))
    h.update(b"\n--config--\n")
    h.update(config_js.encode("utf-8"))
    if extra:
        h.update(b"\n--extra--\n")
        h.update(extra.encode("utf-8"))
    return h.hexdigest()[:32]


def prepare_frames_dir(frames_dir: Path, key: str, log: Any = None) -> int:
    """让帧缓存与本次渲染匹配，返回被清掉的旧帧数。

    签名一致 → 一张不动（断点续渲）；签名不一致 → 清掉所有 f*.jpg 再渲。
    写入签名文件失败不致命（只读目录下引擎自己会报错），所以吞掉 OSError。
    """
    frames_dir = Path(frames_dir)
    stamp = frames_dir / FRAMES_KEY_FILE
    old = ""
    if stamp.exists():
        try:
            old = stamp.read_text(encoding="utf-8").strip()
        except OSError:
            old = ""

    if old == key:
        frames_dir.mkdir(parents=True, exist_ok=True)
        return 0

    removed = 0
    if frames_dir.exists():
        for f in frames_dir.glob("f*.jpg"):
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    if removed and callable(log):
        log(f"代码绘制：渲染内容已变化，清理了 {removed} 张旧帧（否则会串片）")

    frames_dir.mkdir(parents=True, exist_ok=True)
    try:
        stamp.write_text(key, encoding="utf-8")
    except OSError:
        pass
    return removed


def build_encode_args(
    frames_pattern: Path,
    video: Path,
    fps: int,
    ffmpeg: str,
    audio: Path | None = None,
) -> list[str]:
    """构造「JPEG 帧序列 → MP4」的 FFmpeg 参数。

    单独抽成纯函数是为了能在单测里断言音轨有没有接上——真实渲染一帧要 1 秒以上，
    放进单测会拖垮整套测试。

    音轨策略与合成层（``compose.mux_audio``）保持一致：**永远输出一条音轨**。
    给了音频就用它（配音 / 音乐），没给就铺等长静音（``-shortest`` 在视频结束时收尾）。
    这样成片在剪辑软件里不会因缺音轨而报错。

    踩过的坑：这里原来只有 ``-i f%05d.jpg``，所以「代码绘制」出来的成片**没有音轨**，
    配音整体丢失——而界面/小结照样显示「配音：有」。
    """
    args = [
        ffmpeg, "-y", "-loglevel", "error",
        "-framerate", str(int(fps)),
        "-i", str(frames_pattern),
    ]
    if audio is not None and Path(audio).exists():
        args += ["-i", str(audio)]
    else:
        # 无音频输入：用 lavfi 生成等长静音轨道，不落临时文件
        args += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
    args += [
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "medium", "-crf", "19",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        "-shortest",
        str(video),
    ]
    return args


def render_shots(
    shots: list[dict[str, Any]],
    out_dir: Path,
    *,
    genre_label: str = "",
    fps: int = 24,
    workers: int = 3,
    timeout: int = 0,
    log: Any = None,
    progress: Any = None,
    encode: bool = True,
    audio: Path | None = None,
    bpm: float = 0.0,
    beat_offset: float = 0.0,
    lyrics: list[dict[str, Any]] | None = None,
    size: tuple[int, int] = DEFAULT_SIZE,
    quality: str = DEFAULT_QUALITY,
) -> dict[str, Any]:
    """用引擎把分镜渲染成 MP4。

    quality：'high'（出厂画面）/ 'medium'（默认，纸纹减半）/ 'fast'（无纸纹）。
    详见 QUALITY_PRESETS 的实测数据。

    encode=False 时只渲染帧、不做 FFmpeg 合成。用于受限环境（沙箱 / 杀软会禁止
    FFmpeg 访问磁盘并报 Permission denied）下验证画面正确性。

    流程：编译场景 JS → 写入模板 → node render.mjs --frames（并行、可断点续渲）
    → FFmpeg 编码合成。返回 {ok, video, frames, ms_per_frame, log}。

    帧数多时耗时可观（水彩填充比Pillow 慢一到两个数量级），所以用 --frames
    而不是 --clip：前者可断点续渲，中断后重跑不会从零开始。

    ``audio``：要并入成片的音轨（配音或音乐）。为 None 时铺等长静音轨，
    保证成片始终有一条音轨。返回值里的 ``has_audio`` 表示**是否真的并入了音频**
    （而非静音兜底），调用方据此如实上报，不能把静音说成有配音。

    ``bpm`` / ``beat_offset``：音乐节拍。引擎真的会用它们驱动角色呼吸与舞步，
    所以必须传真实值；为 0 时回落 ``DEFAULT_BPM``（见 build_config_js）。

    ``lyrics``：歌词行（``[{start,end,text}]``）。给定时启用**逐字卡拉OK**：
    写入 ``src/lyrics.js``、加载引擎自带的 ``karaoke.js``，并**关掉场景自带的整句字幕**
    （否则两套字幕会叠在一起）。karaoke.js 画的是一条毛笔字条，
    字符随演唱从左到右填充，画在纸纹之上、不受相机变换影响。
    """
    def _say(msg: str) -> None:
        if callable(log):
            log(msg)

    global _progress_sink
    _progress_sink = progress if callable(progress) else None
    try:
        return _render_shots_impl(
            shots, out_dir, genre_label=genre_label, fps=fps,
            workers=workers, timeout=timeout, log=log, encode=encode,
            audio=audio, bpm=bpm, beat_offset=beat_offset, lyrics=lyrics,
            size=size, quality=quality,
        )
    finally:
        _progress_sink = None
        # 最后一道清场：无论正常、失败、超时还是用户中断，都把本次可能残留的
        # 无头浏览器收掉。漏掉这一步的代价是"程序跑完但机器变慢"——孤儿
        # msedge 会在后台持续吃 CPU，且不会自己退出。
        purge_stale_browsers()


def _render_shots_impl(
    shots: list[dict[str, Any]],
    out_dir: Path,
    *,
    genre_label: str,
    fps: int,
    workers: int,
    timeout: int,
    log: Any,
    encode: bool = True,
    audio: Path | None = None,
    bpm: float = 0.0,
    beat_offset: float = 0.0,
    lyrics: list[dict[str, Any]] | None = None,
    size: tuple[int, int] = DEFAULT_SIZE,
    quality: str = DEFAULT_QUALITY,
) -> dict[str, Any]:
    def _say(msg: str) -> None:
        if callable(log):
            log(msg)

    if not shots:
        return {"ok": False, "error": "没有可渲染的镜头", "video": "", "frames": 0,
                "has_audio": False, "audio": ""}

    st = check_env()
    if not st.ok:
        return {"ok": False, "error": st.detail, "video": "", "frames": 0,
                "has_audio": False, "audio": ""}

    duration = sum(float(s.get("duration") or 0.0) for s in shots) or 15.0
    duration = max(3.0, round(duration * 2) / 2)
    # 超时按帧数估：写死 1800s 时 3 分钟片（约 4320 帧）必然被杀在半路。
    # 另外 _run_stream 还有空闲超时兜底，所以这里给足总量是安全的。
    total_frames = max(1, int(round(duration * fps)))
    if timeout <= 0:
        timeout = render_timeout_for(total_frames)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 写到模板目录里（render.mjs 依赖相对路径 studio.html），用固定文件名覆盖，
    # 避免每次渲染都新增一个场景文件把模板目录搞脏。
    work = work_dir()
    scene_path = work / "src" / "scenes" / "yiyyan.js"
    # 有歌词就走逐字卡拉OK：场景自身的整句字幕必须关掉，否则两套字幕叠在一起
    karaoke = bool(lyrics)
    scene_js = build_scene_js(shots, genre_label=genre_label, fps=fps,
                              captions=not karaoke)
    config_js = build_config_js(duration, fps=fps, beat_bpm=bpm, beat_offset=beat_offset,
                                size=size)
    lyrics_js = build_lyrics_js(lyrics) if karaoke else ""
    quality_src = quality_js(quality)
    try:
        scene_path.parent.mkdir(parents=True, exist_ok=True)
        scene_path.write_text(scene_js, encoding="utf-8")
        (work / "src" / "config.js").write_text(config_js, encoding="utf-8")
        # 必须在 studio.html 之前落盘：studio.html 里 <script src="src/quality.js">
        # 是硬引用，文件缺失会 404，core.js 读到 undefined 就退回全画质。
        (work / "src" / "quality.js").write_text(quality_src, encoding="utf-8")
        if karaoke:
            (work / "src" / "lyrics.js").write_text(lyrics_js, encoding="utf-8")
        (work / "studio.html").write_text(
            build_studio_html("src/scenes/yiyyan.js",
                              modules_prefix=modules_prefix(),
                              karaoke=karaoke, size=size), encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"写入场景文件失败：{exc}", "video": "", "frames": 0,
                "has_audio": False, "audio": ""}

    # 帧缓存必须与本次渲染匹配：签名不一致说明换了片子/时长/帧率/歌词，先清掉旧帧，
    # 否则引擎会把上一次的帧当成本次的（静默串片 + 成片时长错乱 + 歌词停留在上一版）。
    # 签名一致则保留，断点续渲不受影响。
    prepare_frames_dir(work / "out" / "frames",
                       # 画质档位也要进签名：换档后画面（纸纹强度）确实变了，
                       # 不进签名就会复用旧质感的帧——和"串片"同一类静默错误。
                       frames_cache_key(scene_js, config_js, fps,
                                        extra=lyrics_js + "\n--quality--\n" + quality_src),
                       log=_say)
    if karaoke:
        _say(f"代码绘制：逐字卡拉OK 已启用（{len(lyrics or [])} 行歌词）")

    _say(f"代码绘制：开始渲染 {duration:.1f}s / {len(shots)} 镜（{fps}fps）…")
    # 浏览器路径必须**显式**传给 render.mjs：它只认CHROME_PATH 环境变量和
    # --chrome 参数，而自己的候选列表里没有 Windows 版Edge。踩过一次——
    # 体检全绿但渲染报 "Chrome not found"，就是因为只在体检里探测、没透传给渲染。
    code, output = render_with_retry(
        lambda: _run_stream(
            [_node_exe(), "render.mjs", "--frames",
             f"--chrome={st.browser}",
             f"--workers={max(1, int(workers))}", f"--fps={int(fps)}"],
            cwd=work, timeout=timeout,
        ),
        say=_say,
    )
    frames_dir = work / "out" / "frames"
    rendered = len(list(frames_dir.glob("f*.jpg"))) if frames_dir.exists() else 0
    if code != 0:
        return {
            "ok": False,
            "error": f"渲染失败（node 退出码 {code}）：{output[-600:] or '无输出'}",
            "video": "", "frames": rendered, "has_audio": False, "audio": "",
        }

    if not encode:
        _say(f"代码绘制：已渲染 {rendered} 帧（按参数跳过 FFmpeg 合成）")
        return {
            "ok": True,
            "video": "",
            "frames": rendered,
            "ms_per_frame": ms_per_frame_hint(output),
            "error": "", "has_audio": False, "audio": "",
            "karaoke": karaoke,
        }

    video = out_dir / "painted.mp4"
    # FFmpeg 用项目自用的，和管线其他环节保持一致（用户不需要另装）
    ffmpeg = ROOT / "bin" / "ffmpeg.exe"
    ffmpeg = str(ffmpeg) if ffmpeg.exists() else (shutil.which("ffmpeg") or "ffmpeg")
    # 音轨：给了就并进去，没给就铺静音。has_audio 表示"真的并入了音频"，
    # 调用方必须照此上报，不能把静音轨说成有配音（历史上出过这个问题）。
    audio_path = Path(audio) if audio else None
    has_audio = bool(audio_path and audio_path.exists())
    if audio_path and not has_audio:
        _say(f"代码绘制：音轨不存在，已铺静音轨（{audio_path}）")
    code, enc_out = _run(
        build_encode_args(frames_dir / "f%05d.jpg", video, int(fps), ffmpeg,
                          audio=audio_path if has_audio else None),
        cwd=work, timeout=600)
    if code != 0 or not video.exists():
        return {
            "ok": False,
            "error": f"FFmpeg 合成失败（退出码 {code}）：{enc_out[-600:]}",
            "video": "", "frames": rendered, "has_audio": False, "audio": "",
        }

    ms_per_frame = ms_per_frame_hint(output)

    _say(f"代码绘制：成片 {video.name}（{video.stat().st_size / 1048576:.1f} MB）"
         + (f" · 已并音轨 {audio_path.name}" if has_audio else " · 静音轨"))
    return {
        "ok": True,
        "video": str(video),
        "frames": rendered,
        "ms_per_frame": ms_per_frame,
        "error": "",
        "has_audio": has_audio,
        "audio": str(audio_path) if has_audio else "",
        "karaoke": karaoke,
    }


def clean_frames(frames_dir: Path | None = None, log: Any = None) -> float:
    """清掉帧目录并返回释放的 MB。

    编码成功后才该调用：**3 分钟 1080p 约 2.4 GB**（均值 594 KB/帧）。
    清掉之后断点续渲就没了，但那时 MP4 已经产出，不需要再续。
    """
    d = Path(frames_dir) if frames_dir else (work_dir() / "out" / "frames")
    if not d.exists():
        return 0.0
    freed = _dir_size_mb(d)
    for f in list(d.glob("*")):
        try:
            if f.is_dir():
                shutil.rmtree(f, ignore_errors=True)
            else:
                f.unlink()
        except OSError:
            pass
    if callable(log):
        log(f"代码绘制：已清理帧缓存，释放 {freed:.0f} MB")
    return freed


def apply_painted(
    result: dict[str, Any],
    *,
    log: Any = None,
    progress: Any = None,
    fps: int = 24,
    workers: int | None = None,
    clean_frames_after: bool = False,
    quality: str = DEFAULT_QUALITY,
) -> dict[str, Any]:
    """把 ``result["compose"]`` 就地换成代码绘制（水彩逐帧）版本。

    **为什么这段逻辑放在这里而不是界面层**：命令行也该能复现 MV 演示
    （GUI 与 CLI 共用一条通路），而这段逻辑又长到不该复制两份。

    三件事一次做对，每一件都是踩过的坑：

    1. **复用上游已生成的分镜**（``result["storyboard"]``），不额外烧一次 LLM。
    2. **失败不抛异常**：保留原成片 + 写 notes。可选的锦上添花功能，
       不该把一条本来能出的片子搞失败。
    3. **自动接上音轨、真实 BPM/首拍、歌词（逐字卡拉OK）**——
       这三样分别对应"成片无声"、"引擎收到假节拍"、"画面没有歌词"三个历史缺陷。

    ``log(level, message)`` / ``progress(stage, ratio)``：可选回调，与界面层同签名。
    """
    def _log(level: str, msg: str) -> None:
        if callable(log):
            log(level, msg)

    def _prog(stage: str, pct: float) -> None:
        if callable(progress):
            progress(stage, pct)

    board = result.get("storyboard") or {}
    shots = board.get("shots") or []
    if not shots:
        result.setdefault("notes", []).append("代码绘制：没有分镜数据，已跳过")
        return result

    genre_label = str((result.get("brief") or {}).get("style") or "")
    project_dir = Path(result.get("project_dir") or ROOT / "runs" / str(result.get("run_id") or ""))
    out_dir = project_dir / "painted"
    # 覆盖 comp 之前先把旧值取出来：原来的代码在 comp.update() 之后才读 old_video，
    # 那时它已经等于新视频，「原成片保留在…」这条 note 就永远写不出来（死代码）。
    comp_before = dict(result.get("compose") or {})
    old_video = str(comp_before.get("video") or "")
    audio_src = str(comp_before.get("audio") or "")
    music_info = dict(result.get("music") or {})
    bpm = float(music_info.get("bpm") or 0.0)
    beat_offset = float(music_info.get("offset") or 0.0)
    lyrics = list(result.get("lyrics") or [])
    # 画幅：引擎的画布尺寸由 config.js 下发（core.js 原来是写死的 1920×1080），
    # 所以竖屏/方形必须在这里读简报、一路传到 config.js 与 studio.html。
    size = size_for_aspect(str((result.get("brief") or {}).get("aspect_ratio") or ""))
    # 进度映射到 0.30~0.95，把前 30% 留给已经跑完的创意/决策/占位帧阶段
    _prog("代码绘制", 0.30)

    def _on_line(line: str) -> None:
        # node 每渲完 24 帧回报一次 ms/frame 与 eta，转成界面进度条
        if "ms/frame" not in line:
            return
        head = line.split("ms/frame")[0].strip()
        try:
            done = int(head.split()[0].split("/")[-1])
            total = int(head.split()[0].split("/")[0])
        except (ValueError, IndexError):
            return
        if total > 0:
            _prog("代码绘制", 0.30 + 0.65 * done / total)

    try:
        out = render_shots(
            shots, out_dir,
            genre_label=genre_label,
            fps=fps,
            workers=workers if workers else default_workers(),
            log=lambda m: _log("info", m),
            progress=_on_line,
            # 把上游已经做好的配音/音乐并进成片。以前这里不传，
            # 于是勾选「代码绘制」后成片没有音轨、配音整体丢失。
            audio=Path(audio_src) if audio_src else None,
            bpm=bpm,
            beat_offset=beat_offset,
            lyrics=lyrics,
            size=size,
            quality=quality,
        )
    except Exception as exc:  # noqa: BLE001
        result.setdefault("notes", []).append(f"代码绘制失败，已保留原成片：{exc}")
        _log("error", f"代码绘制失败：{exc}")
        return result

    if not out.get("ok"):
        result.setdefault("notes", []).append(
            f"代码绘制未生效，已保留原成片：{out.get('error', '未知原因')}")
        _log("warn", f"代码绘制未生效：{out.get('error', '')}")
        return result

    video = Path(out["video"])
    comp = dict(comp_before)
    attached = bool(out.get("has_audio"))
    comp.update({
        "video": str(video),
        "size_mb": round(video.stat().st_size / 1048576, 2),
        "backend": "painted",
        "frames": out.get("frames", 0),
        "karaoke": bool(out.get("karaoke")),
        # 如实上报：渲染后端真的并进了音频，才算「配音：有」
        "has_audio": attached,
        "audio_source": str(comp.get("audio_source") or "silent") if attached else "silent",
        "audio": str(out.get("audio") or ""),
    })
    result["compose"] = comp
    _log("env", f"代码绘制完成：{out.get('frames', 0)} 帧 / "
                f"{out.get('ms_per_frame', 0)} ms每帧 · {size[0]}x{size[1]}"
                + (" · 逐字卡拉OK" if out.get("karaoke") else "")
                + (" · 已并音轨" if attached else " · 静音轨"))
    # 长片磁盘卫生：编码成功后帧缓存就没用了（3 分钟 1080p 约 2.4 GB）。
    # 默认不动，由调用方显式要求——万一还想换个音轨重编码，帧还在。
    if clean_frames_after:
        clean_frames(log=lambda m: _log("info", m))
    # 原成片保留但标为被替换，方便用户对照"占位帧版 vs 水彩版"
    if old_video and Path(old_video).exists() and Path(old_video) != video:
        result.setdefault("notes", []).append(
            f"代码绘制已替换占位帧成片，原成片保留在 {old_video}")
    if audio_src and not attached:
        # 有音轨却没并上，必须说出来，不能静默产出无声成片
        result.setdefault("notes", []).append(
            f"代码绘制成片未能并入音轨（{audio_src}），当前为静音轨")
    return result
