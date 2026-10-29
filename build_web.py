#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一言片场 V4 · 打包成可迁移的独立程序（Web 版）。

产物：`dist/YiYanStudio-V4/YiYanStudio-V4.exe` + 同目录依赖。
目标用户**不需要装 Python**，解压即用。

## 为什么不再沿用 build.py
上一版（Tkinter）的 `build.py` 入口是 `yiyan_gui.py`、数据目录里没有 `web/dist`，
隐藏导入也不一样。V4 换掉界面后它已经没有用处，已随旧前端一起删除；
这里只留一个打包脚本，免得两个版本互相干扰。

## 四个必须注意的坑（都踩过）
1. **中文路径**：PyInstaller 6.x 在含非 ASCII 字符的路径下会报
   `Could not load PyInstaller's embedded PKG archive`。所以本脚本先把源码
   复制到 ASCII 临时目录再构建，构建完删掉临时目录。
2. **静态文件位置**：打包后 `web/dist` 在 `sys._MEIPASS`，不是 EXE 同级。
   `server/app.py` 必须用 `resource_dir("web")` 解析（已改）。
3. **后端不能 subprocess 起自己**：`desktop.py` 已改成同进程 uvicorn 线程。
4. **代码绘制的模板要整套带**：`render.mjs`、`package.json`、`node_modules`
   缺一个，打包版就渲不出帧——源码模式下永远看不出这个问题。

## 用法
    python build_web.py            # 默认 onedir（推荐：启动快，改文件方便）
    python build_web.py --onefile  # 单文件（启动要解压 400MB，很慢，不建议）
    python build_web.py --no-ffmpeg  # 不含 ffmpeg（产物小，但用户需自备）
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENTRY = ROOT / "desktop.py"
APP_NAME = "YiYanStudio-V6"
DIST_DIR = ROOT / "dist"
BUILD_DIR = ROOT / "build"

#: 随包数据（目录 → 打包后的相对路径）
#:
#: `vendor/painted-template` 必须**整套**进去：`pipeline/painted.py` 运行时会
#: 从模板目录复制 `render.mjs` / `package.json` 到可写的工作副本，并通过
#: `node_modules` 解析 `puppeteer-core`（ESM 只认入口文件所在目录逐级向上找，
#: 不看 NODE_PATH）。少带其中任何一样，打包版就是「渲不出帧」，
#: 而源码模式永远复现不出来。
DATA_DIRS = ["web/dist", "prompts", "workflows", "vendor/painted-template",
             "docs", "assets"]

#: 复制 vendor 时要排掉的东西：`out/` 是上游模板的渲染产物（可能有几十 MB），
#: `.git` 是仓库残留。注意**不能**排 `node_modules`——排掉就等于要求用户自己
#: 装 Node 再 npm install，离线可渲染的前提就没了。
VENDOR_EXCLUDE = ["out", "out.bak", ".git"]

#: uvicorn / fastapi / pywebview 有大量动态导入，PyInstaller 静态分析抓不全
HIDDEN = [
    "uvicorn", "uvicorn.logging", "uvicorn.loops", "uvicorn.loops.auto",
    "uvicorn.loops.asyncio", "uvicorn.protocols", "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto", "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets", "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan", "uvicorn.lifespan.on", "uvicorn.lifespan.off",
    "fastapi", "starlette", "pydantic", "anyio", "httptools", "h11",
    "webview", "webview.platforms", "webview.platforms.edgechromium",
    "clr_loader", "pythonnet", "server.app",
    # multipart：`POST /api/media/audio`（音轨上传）靠它解析表单。
    #
    # **必须显式列出。** FastAPI 是在 `ensure_multipart_is_installed()` 里用
    # `from python_multipart import __version__` 试导入的，而且外面套着
    # try/except ImportError——静态分析把它当成"可选依赖"跳过。结果是源码跑得
    # 好好的，打包版一上传就 500，报 "Form data requires python-multipart to be
    # installed"。
    #
    # 怎么验：别看 `_internal/` 有没有同名目录——纯 Python 包是被打进 EXE 内嵌的
    # `PYZ.pyz`，本来就不会以目录形式落盘。要看 `build/_pyi/<name>/PYZ-00.toc`
    # 里有没有这几条。2026-10-21 实测：不加这两行时整份产物 find 不到任何
    # multipart 文件，加上之后 PYZ 清单里 multipart / python_multipart 四模块齐全。
    "python_multipart", "python_multipart.multipart",
    "multipart",
]

#: 明确排除，避免把无关的大包带进去
EXCLUDE = ["tkinter", "matplotlib", "numpy", "scipy", "pandas", "PyQt5", "PySide2"]


def need(module: str, hint: str) -> bool:
    try:
        __import__(module)
        return True
    except ImportError:
        print(f"[缺失] 当前解释器没有 {module}：{hint}")
        return False


def ascii_stage() -> Path:
    """把源码复制到 ASCII 路径下构建，规避 PyInstaller 的中文路径缺陷。

    只复制构建必需的文件。bin/（426MB ffmpeg）也要进来，因为要一起打包。
    """
    staging = Path(tempfile.gettempdir()) / "yiyan_build_stage"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)

    include = ["app", "pipeline", "server", "prompts", "workflows", "web", "vendor",
               "docs", "assets", "bin"]
    for name in include:
        src = ROOT / name
        if not src.exists():
            continue
        dst = staging / name
        if name == "web":
            # 只带 dist，别把 node_modules（几百 MB、几万文件）拖进来
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("node_modules"))
        elif name == "vendor":
            # 渲染产物与仓库残留不进包；node_modules 必须留（见 VENDOR_EXCLUDE 注释）
            shutil.copytree(src, dst,
                            ignore=shutil.ignore_patterns(*VENDOR_EXCLUDE))
        else:
            shutil.copytree(src, dst)

    for f in ("desktop.py", "yiyan.py", "build_web.py", "requirements.txt",
              "LICENSE", ".env.example"):
        src = ROOT / f
        if src.exists():
            shutil.copy2(src, staging / f)
    return staging


def build(onedir: bool, with_ffmpeg: bool) -> int:
    if not ENTRY.exists():
        print(f"[错误] 缺少入口：{ENTRY}")
        return 1
    if not need("PyInstaller", "pip install pyinstaller"):
        return 1
    if not need("webview", "pip install pywebview"):
        return 1
    if not (ROOT / "web" / "dist" / "index.html").exists():
        print("[错误] 前端未构建：web/dist/index.html 不存在，先跑 npm run build")
        return 1

    print("[1/4] 复制源码到 ASCII 临时目录…")
    t0 = time.time()
    stage = ascii_stage()
    print(f"      {stage}（{time.time() - t0:.1f}s）")

    print("[2/4] 组装 PyInstaller 参数…")
    sep = ";" if os.name == "nt" else ":"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", APP_NAME,
        "--onedir" if onedir else "--onefile",
        "--windowed",                      # 不弹控制台
        "--icon", str(ROOT / "assets" / "icon.ico") if (ROOT / "assets" / "icon.ico").exists() else "",
        "--distpath", str(DIST_DIR / "_pyi"),
        "--workpath", str(BUILD_DIR / "_pyi"),
        "--specpath", str(BUILD_DIR),
    ]
    cmd = [c for c in cmd if c]

    for d in DATA_DIRS:
        src = stage / d
        if src.exists():
            cmd += ["--add-data", f"{src}{sep}{d}"]
    for mod in HIDDEN:
        cmd += ["--hidden-import", mod]
    for mod in EXCLUDE:
        cmd += ["--exclude-module", mod]

    if with_ffmpeg:
        for tool in ("ffmpeg", "ffprobe"):
            exe = stage / "bin" / f"{tool}.exe"
            if exe.exists():
                cmd += ["--add-binary", f"{exe}{sep}bin"]
                print(f"      随包 {tool}.exe（{exe.stat().st_size / 1048576:.0f} MB）")
            else:
                print(f"      [警告] 找不到 bin/{tool}.exe，产物将不带它")

    cmd.append(str(stage / "desktop.py"))

    print("[3/4] 运行 PyInstaller（这一步最慢，带 ffmpeg 约 2-5 分钟）…")
    t1 = time.time()
    proc = subprocess.run(cmd, cwd=str(stage))
    if proc.returncode != 0:
        print(f"[失败] PyInstaller 返回 {proc.returncode}")
        print(f"       临时目录保留在 {stage} 供排查")
        return proc.returncode
    print(f"      完成（{time.time() - t1:.0f}s）")

    print("[4/4] 整理产物…")
    built = DIST_DIR / "_pyi" / APP_NAME
    final = DIST_DIR / APP_NAME
    if not built.exists():
        print(f"[失败] PyInstaller 没有产出 {built}")
        return 1
    if final.exists():
        # 两个陷阱叠在一起，踩过一次（产物直接丢了）：
        #  a) rmtree 833 个文件会被 safe-delete 拦下（阈值 50），目录根本没删掉
        #  b) shutil.move 在目标已存在时，会把源目录**移进目标里面**
        #     （变成 final/APP_NAME/），之后再一删就把新产物一起删了
        # 改成 rename 到带时间戳的旁路目录：一次 rename，不触发任何删除。
        backup = final.with_name(f"{final.name}.old-{time.strftime('%H%M%S')}")
        final.rename(backup)
        print(f"      旧产物改名为 {backup.name}（确认新版可用后手动删）")
    shutil.move(str(built), str(final))
    shutil.rmtree(DIST_DIR / "_pyi", ignore_errors=True)
    shutil.rmtree(stage, ignore_errors=True)

    exe = final / f"{APP_NAME}.exe"
    if not exe.exists():
        print(f"[失败] 没找到 {exe}")
        return 1

    total = sum(f.stat().st_size for f in final.rglob("*") if f.is_file())
    print()
    print("=" * 58)
    print(f"产物：{final}")
    print(f"入口：{exe.name}")
    print(f"体积：{total / 1048576:.0f} MB")
    print("=" * 58)
    print("分发：把整个文件夹压缩发给对方，解压后双击 EXE 即可。")
    print("      对方**不需要**装 Python；Win10/11 自带 WebView2。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="打包 Web 版为独立程序")
    ap.add_argument("--onefile", action="store_true", help="单文件（启动慢，不建议）")
    ap.add_argument("--no-ffmpeg", action="store_true", help="不带 ffmpeg（产物小）")
    args = ap.parse_args()
    return build(onedir=not args.onefile, with_ffmpeg=not args.no_ffmpeg)


if __name__ == "__main__":
    sys.exit(main())
