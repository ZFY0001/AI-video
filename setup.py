#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""环境准备：检查并补齐运行所需的全部外部依赖。

**为什么需要它**：仓库里只有源码（约 2 MB）。两样必需的东西因为体积太大
**故意没有提交**：

| 缺的东西 | 体积 | 为什么不能进仓库 |
|---|---|---|
| `bin/ffmpeg.exe` + `ffprobe.exe` | 426 MB | 单文件 213 MB，超过 GitHub 的 100 MB 上限 |
| `vendor/painted-template/node_modules/` | 73 MB | 数万个文件，且可由 `npm ci` 精确重建 |

所以别人 `git clone` 之后**必须先跑一次本脚本**，否则能开界面但出不了片。

用法：
    python setup.py            # 只体检，列出缺什么、怎么补（推荐先跑这个）
    python setup.py --install  # 体检 + 自动装 Python 依赖 + npm ci
    python setup.py --ffmpeg   # 额外自动下载 FFmpeg（约 213 MB，需联网）
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BIN = ROOT / "bin"
VENDOR = ROOT / "vendor" / "painted-template"

#: FFmpeg Windows 构建（gyan.dev 的 full build，含 libx264/aac，够本项目用）
FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

OK, WARN, BAD = "  [有] ", "  [缺] ", "  [错] "


def _size(p: Path) -> str:
    try:
        return f"{p.stat().st_size / 1024 / 1024:.0f} MB"
    except OSError:
        return "?"


def check_python() -> bool:
    v = sys.version_info
    good = (v.major, v.minor) >= (3, 10)
    print(f"{OK if good else BAD}Python {v.major}.{v.minor}.{v.micro}"
          + ("" if good else "（需要 3.10+）"))
    return good


def check_py_deps() -> list[str]:
    missing = []
    for mod, pkg in (("PIL", "Pillow"), ("yaml", "PyYAML")):
        try:
            __import__(mod)
            print(f"{OK}{pkg}")
        except ImportError:
            print(f"{WARN}{pkg}（pip install {pkg}）")
            missing.append(pkg)
    return missing


def check_ffmpeg() -> bool:
    """优先看随包 bin/，其次看 PATH。"""
    local = BIN / "ffmpeg.exe"
    if local.exists():
        print(f"{OK}FFmpeg（随包 bin/，{_size(local)}）")
        return True
    found = shutil.which("ffmpeg")
    if found:
        print(f"{OK}FFmpeg（PATH: {found}）")
        return True
    print(f"{WARN}FFmpeg —— **没有它出不了 MP4**")
    print("       补法二选一：")
    print("         a) python setup.py --ffmpeg   （自动下载到 bin/，约 213 MB）")
    print(f"         b) 自行下载 {FFMPEG_URL}")
    print("            解压后把 ffmpeg.exe / ffprobe.exe 放进 bin/")
    print("       或：winget install Gyan.FFmpeg（装到 PATH 也能被识别）")
    return False


def check_node() -> bool:
    node = shutil.which("node")
    if node:
        try:
            v = subprocess.run([node, "--version"], capture_output=True,
                               text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            v = "?"
        print(f"{OK}Node.js {v}")
        return True
    print(f"{WARN}Node.js —— 只有「代码绘制」需要它，不装也能出占位帧的片子")
    print("       补法：https://nodejs.org/ （LTS 版即可）")
    return False


def check_vendor_modules() -> bool:
    mods = VENDOR / "node_modules"
    if mods.is_dir() and any(mods.iterdir()):
        n = sum(1 for _ in mods.iterdir())
        print(f"{OK}p5 渲染依赖（vendor/.../node_modules，{n} 个包）")
        return True
    print(f"{WARN}p5 渲染依赖 —— 「代码绘制」需要，约 73 MB")
    print("       补法：python setup.py --install（会在 vendor 目录跑 npm ci）")
    return False


def check_browser() -> bool:
    """渲染要用无头 Edge/Chrome。Windows 10+ 自带 Edge。"""
    cands = [
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
        / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
        / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
        / "Google/Chrome/Application/chrome.exe",
    ]
    for c in cands:
        if c.exists():
            print(f"{OK}无头浏览器（{c.name}）")
            return True
    print(f"{WARN}没找到 Edge/Chrome —— 「代码绘制」需要它（Windows 10+ 一般自带 Edge）")
    return False


def check_env_file() -> None:
    env = ROOT / ".env"
    if not env.exists():
        ex = ROOT / ".env.example"
        if ex.exists():
            shutil.copyfile(ex, env)
            print(f"{OK}.env（已从 .env.example 复制，Key 留空也能跑）")
        else:
            print(f"{WARN}.env 不存在，且没有 .env.example")
        return
    filled = []
    for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip().endswith("_API_KEY") and v.strip():
            filled.append(k.strip())
    if filled:
        print(f"{OK}.env（已填 {len(filled)} 个 Key；**别提交它**）")
    else:
        print(f"{OK}.env（Key 全空 —— 走离线引擎也能出片）")


def install_py(missing: list[str]) -> None:
    if not missing:
        return
    print(f"\n>>> 安装 Python 依赖：{' '.join(missing)}")
    subprocess.run([sys.executable, "-m", "pip", "install", *missing], check=False)


def install_vendor() -> None:
    if not (VENDOR / "package.json").exists():
        print("\n>>> 跳过 npm：vendor/painted-template/package.json 不存在")
        return
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        print("\n>>> 跳过 npm：PATH 里没有 npm（先装 Node.js）")
        return
    print("\n>>> 安装 p5 渲染依赖（npm ci，约 73 MB）")
    # 用 ci 而不是 install：package-lock.json 在仓库里，ci 能装出**完全一致**的版本
    cmd = [npm, "ci"] if (VENDOR / "package-lock.json").exists() else [npm, "install"]
    subprocess.run(cmd, cwd=str(VENDOR), check=False)


def download_ffmpeg() -> bool:
    if (BIN / "ffmpeg.exe").exists():
        print("\n>>> bin/ffmpeg.exe 已存在，跳过下载")
        return True
    BIN.mkdir(parents=True, exist_ok=True)
    tmp = BIN / "_ffmpeg_download.zip"
    print(f"\n>>> 下载 FFmpeg（约 213 MB）\n    {FFMPEG_URL}")
    try:
        with urllib.request.urlopen(FFMPEG_URL, timeout=60) as resp, \
                open(tmp, "wb") as f:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total:
                    pct = done * 100 // total
                    print(f"\r    {done/1048576:.1f}/{total/1048576:.1f} MB ({pct}%)",
                          end="", flush=True)
        print()
    except Exception as exc:  # noqa: BLE001
        print(f"    下载失败：{exc}")
        print("    请按上面的链接手动下载，把 ffmpeg.exe / ffprobe.exe 放进 bin/")
        tmp.unlink(missing_ok=True)
        return False

    try:
        with zipfile.ZipFile(tmp) as z:
            for name in z.namelist():
                base = os.path.basename(name)
                if base in ("ffmpeg.exe", "ffprobe.exe"):
                    with z.open(name) as src, open(BIN / base, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    print(f"    解出 bin/{base}（{_size(BIN / base)}）")
    except Exception as exc:  # noqa: BLE001
        print(f"    解压失败：{exc}")
        return False
    finally:
        tmp.unlink(missing_ok=True)

    ok = (BIN / "ffmpeg.exe").exists()
    print("    FFmpeg 就绪" if ok else "    没解出 ffmpeg.exe，请手动放置")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="一言片场 · 环境准备")
    ap.add_argument("--install", action="store_true", help="安装 Python 依赖与 npm 依赖")
    ap.add_argument("--ffmpeg", action="store_true", help="自动下载 FFmpeg 到 bin/")
    args = ap.parse_args()

    print("=" * 58)
    print("一言片场 · 环境体检")
    print("=" * 58)

    py_ok = check_python()
    missing = check_py_deps()
    ff_ok = check_ffmpeg()
    node_ok = check_node()
    mod_ok = check_vendor_modules()
    check_browser()   # 仅提示，不阻断
    check_env_file()

    if args.install:
        install_py(missing)
        if node_ok and not mod_ok:
            install_vendor()
    if args.ffmpeg:
        ff_ok = download_ffmpeg() or ff_ok

    print("\n" + "=" * 58)
    blockers = []
    if not py_ok:
        blockers.append("Python 版本过低（需 3.10+）")
    if not ff_ok:
        blockers.append("缺 FFmpeg → 出不了 MP4")
    if blockers:
        print("还不能出片，先解决：")
        for b in blockers:
            print(f"  · {b}")
        print("\n最省事：python setup.py --install --ffmpeg")
        return 1

    print("可以出片了。")
    if not (node_ok and mod_ok):
        print("（「代码绘制」还需 Node.js + npm ci；不装也能出占位帧的片子）")
    print("\n启动：双击 启动客户端.bat（原生窗口），或 启动Web版.bat（浏览器）")
    print("出片：python yiyan.py \"给你的家乡做一条 15 秒宣传片\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
