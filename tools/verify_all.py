"""一条命令跑完本轮发布门槛：后端自检 + 前端类型检查/构建。

用法（项目根目录）：
    .venv/Scripts/python.exe tools/verify_all.py

不初始化 Git、不改用户数据；自检使用 tests 内部的隔离目录。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(label: str, args: list[str], cwd: Path, env: dict[str, str] | None = None) -> None:
    print(f"\n=== {label} ===")
    result = subprocess.run(args, cwd=str(cwd), env=env)
    if result.returncode != 0:
        raise SystemExit(f"{label} 失败（exit={result.returncode}）")


def main() -> int:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    run("后端全量自检",
        [sys.executable, str(ROOT / "tests" / "test_pipeline.py")], ROOT, env)

    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        raise SystemExit("找不到 npm；请先安装 Node.js")
    # build 脚本内部已经执行 tsc --noEmit。
    run("前端类型检查与构建", [npm, "run", "build"], ROOT / "web")
    print("\n全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())