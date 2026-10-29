# -*- coding: utf-8 -*-
"""打「前端交接包」：队友拿到能改前端、跑起来看效果。

和 `build_web.py` 的分工：
  · build_web.py → 面向**最终用户**的可执行程序（含 ffmpeg，约 500MB）
  · 本脚本       → 面向**开发者**的源码包（几 MB，不含依赖）

## 为什么不含这几样（每一条都踩过或想过）
- `.venv`：队友机器上必然失效。home 指向本机的用户目录，且中文路径下
  python.exe 读不到 pyvenv.cfg。给他只会制造"启动不了"的困惑。
- `bin/`：426MB 的 ffmpeg。队友主要改前端；需要出片时按说明装即可。
- `web/node_modules`、`vendor/*/node_modules`：三万多个文件，zip 要跑七八分钟，
  而队友一条 `npm ci` 一两分钟就装完，且 lock 文件能保证版本完全一致。
- `web/dist`、`runs/`、`work/`：构建产物与个人数据。
- `.env`：可能含真实 API Key。

## 用法
    python tools/pack_frontend.py [输出目录]
不传输出目录时，默认放到桌面的「一言片场V4-前端交接」。
"""
from __future__ import annotations

import os
import sys
import time
import zipfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

#: 项目根：本文件在 tools/ 下，往上一层才是项目
ROOT = Path(__file__).resolve().parents[1]
APP = "YiYanStudio-V6"

#: 按目录名整段排除（任意层级）
SKIP_DIR_NAMES = {
    ".git", ".venv", "__pycache__", ".mypy_cache", ".pytest_cache",
    "node_modules", "dist", "build", "runs", "work", "cache", "bin",
    "candidates", ".idea", ".vscode", ".vite",
}
#: 具体文件
SKIP_FILES = {".env", ".env.verify-backup"}
SKIP_EXT = {".pyc", ".pyo", ".log"}


def skip(rel: Path) -> bool:
    if any(part in SKIP_DIR_NAMES for part in rel.parts):
        return True
    if rel.name in SKIP_FILES or rel.suffix in SKIP_EXT:
        return True
    return False


def main() -> int:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        Path(os.path.expanduser("~")) / "Desktop" / "一言片场V4-前端交接")
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{APP}-源码包.zip"
    if out.exists():
        out.unlink()

    t0 = time.time()
    files = 0
    raw = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(ROOT.rglob("*")):
            rel = path.relative_to(ROOT)
            if skip(rel) or not path.is_file():
                continue
            try:
                zf.write(path, Path(APP) / rel)
                files += 1
                raw += path.stat().st_size
            except OSError as exc:
                print(f"  跳过 {rel}：{exc}")

    print("=" * 58)
    print(f"产物：{out}")
    print(f"文件：{files} 个")
    print(f"原始 {raw / 1048576:.1f} MB → 压缩后 {out.stat().st_size / 1048576:.1f} MB")
    print(f"耗时：{time.time() - t0:.1f} 秒")
    print("=" * 58)

    # 自检：关键文件必须在包里，缺一个队友就跑不起来
    need = [
        "web/package.json", "web/package-lock.json", "web/vite.config.ts",
        "web/src/App.tsx", "web/src/pages/ProfilePage.tsx", "web/src/lib/auth.ts",
        "server/app.py", "server/auth.py", "desktop.py", "requirements.txt",
        "app/version.py", "pipeline/pipeline.py", "prompts", "workflows",
        ".env.example", "README.md",
    ]
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
    missing = [n for n in need if not any(x == f"{APP}/{n}" or x.startswith(f"{APP}/{n}/") for x in names)]
    print("关键文件自检：" + ("全部就位" if not missing else f"★缺 {missing}★"))
    return 0 if not missing else 1


if __name__ == "__main__":
    sys.exit(main())
