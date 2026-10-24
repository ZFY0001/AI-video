#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ComfyUI 模型下载助手。

网络受限时与其手忙脚乱，不如给一份可执行的清单：本脚本会
1) 探测当前网络能否访问模型源
2) 报告ComfyUI 目录与磁盘可用空间
3) 打印对应平台的下载命令

用法：
    python tools/fetch_model.py            # 体检+ 给出下载建议
    python tools/fetch_model.py --path D  # 指定 ComfyUI 所在盘符
"""

from __future__ import annotations

import argparse
import shutil
import sys
import urllib.request
from pathlib import Path

# 可用的模型源（按国内网络友好度排序）
SOURCES = [
    ("HuggingFace 官方", "https://huggingface.co", "~4GB/s"),
    ("HF 国内镜像", "https://hf-mirror.com", "国内可用"),
    ("ModelScope 魔搭", "https://www.modelscope.cn", "国内可用"),
    ("GitHub", "https://github.com", "需代理"),
]

# (文件名, 大小GB, 推荐程度, 说明, 下载地址)
MODELS = [
    (
        "v1-5-pruned-emaonly.safetensors",
        4.3,
        "★ 首选",
        "SD 1.5，8GB 显存轻松跑，约 5-8 秒/张。先用它验证整条链路。",
        "https://huggingface.co/runwayml/stable-diffusion-v1-5/resolve/main/v1-5-pruned-emaonly.safetensors",
    ),
    (
        "sd_xl_base_1.0.safetensors",
        6.9,
        "○ 次选",
        "SDXL Base，画质更好，8GB 显存需 --lowvram，约 30-60 秒/张。",
        "https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/resolve/main/sd_xl_base_1.0.safetensors",
    ),
]


def probe(url: str, timeout: int = 8) -> str:
    """探测站点可达性。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return f"✅ {r.status}"
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "code", None)
        if code:
            return f"❌ HTTP {code}"
        return f"❌ {exc.__class__.__name__}"


def disk_free(drive: str = "D") -> tuple[int, int]:
    usage = shutil.disk_usage(f"{drive}:\\")
    return usage.total // 1024**3, usage.free // 1024**3


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", default="D", help="ComfyUI 所在盘符，默认 D")
    args = ap.parse_args()

    print("=" * 66)
    print("  一言片场 · ComfyUI 接入助手")
    print("=" * 66)

    # ---- 1. 磁盘
    print("\n【1】磁盘空间")
    root = Path(f"{args.path}:\\")
    if root.exists():
        total, free = disk_free(args.path)
        need = 15  # ComfyUI+torch+模型 约需 15GB
        flag = "✅ 够用" if free >= need else f"⚠️ 建议 ≥{need}GB，当前 {free}GB 可能不够"
        print(f"    {args.path}: 共 {total}GB，可用 {free}GB   {flag}")
    else:
        print(f"    {args.path}: 盘不存在")

    # ---- 2. 目标目录
    print("\n【2】ComfyUI 目录")
    comfy = root / "ComfyUI"
    ckpt_dir = comfy / "models" / "checkpoints"
    if comfy.exists():
        print(f"    ✅ 已存在：{comfy}")
        if ckpt_dir.exists():
            models = [p for p in ckpt_dir.glob("*.safetensors")]
            if models:
                print(f"    ✅ 模型目录有 {len(models)} 个 checkpoint：")
                for m in models:
                    print(f"       · {m.name}（{m.stat().st_size / 1024**3:.1f}GB）")
            else:
                print(f"    ○ 模型目录为空：{ckpt_dir}")
                print("       把下面任一模型下载到这里即可")
        else:
            print("    ○ 未找到 models/checkpoints，需先启动一次 ComfyUI 生成目录结构")
    else:
        print(f"    ○ 未找到 {comfy}")
        print(f"       部署命令：cd {args.path}:\\ && git clone --depth 1 https://github.com/Comfy-Org/ComfyUI.git")

    # ---- 3. 网络
    print("\n【3】模型源可达性")
    reachable = []
    for name, url, note in SOURCES:
        result = probe(url)
        print(f"    {name:<16} {result:<20} {note}")
        if result.startswith("✅"):
            reachable.append((name, url))
    if not reachable:
        print("\n    ⚠️  所有模型源均不可达 —— 当前网络无法下载模型。")
        print("       可选做法：")
        print("       1) 换网络环境（手机热点 / 公司网络 / 换DNS）")
        print("       2) 在能联网的机器上下载，再拷贝到")
        print(f"          {ckpt_dir}")

    # ---- 4. 下载建议
    print("\n【4】推荐下载")
    total_need, free = disk_free(args.path)
    for name, size, level, desc, url in MODELS:
        budget = "✓空间够" if free >= size + 2 else "✗ 空间不足"
        print(f"\n    [{level}] {name}（{size}GB）  {budget}")
        print(f"      {desc}")
        if reachable:
            print("      下载：")
            print(f"        curl -L -C - -o \"{ckpt_dir / name}\" \"{url}\"")
        else:
            print(f"      手动下载后放入：{ckpt_dir}")

    # ---- 5. 启动命令
    print("\n【5】启动命令")
    print(f'    cd {comfy}')
    print("    venv\\Scripts\\python main.py --port 8188 --disable-dynamic-vram --lowvram")
    print("    （--disable-dynamic-vram 必须加：否则每次请求都重载模型，实测多花 300 秒/次）")

    # ---- 6. 校验
    print("\n【6】接入校验")
    print("    回到一言片场目录后：")
    print("    python yiyan.py --check-env")
    print("    （图形界面：设置与环境 → 环境体检）")
    print('    看到「出图 comfy ✅」即接入成功')

    print("\n" + "=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())