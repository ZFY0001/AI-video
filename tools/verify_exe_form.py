#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验证「代码绘制后端在打包形态（EXE）下能不能跑」。

为什么需要这个脚本
------------------
打包后的模板在 PyInstaller 的 `sys._MEIPASS` 临时解包目录里，而那个目录
**只读、且进程退出即消失**。这类问题在源码模式下永远看不出来（模板就在项目里、
可写），一旦发给别人就必炸，而且症状五花八门：

  -复制出来的文件是只读的→ 写config.js 时 Permission denied，渲染 0 帧
  - render.mjs 用 ESM `import 'puppeteer-core'`，Node 只从入口文件所在目录
    逐级向上找 node_modules →工作副本没有 node_modules 就 ERR_MODULE_NOT_FOUND
  - studio.html 里的 p5 路径是相对的 → 同样 ERR_MODULE_NOT_FOUND，控制台一片空白

所以这里**模拟**打包形态来验，不用真的每次打一个 EXE（打一次要两分钟，
而且改一行Python 就得重打，验起来太慢）。做三件事：

1. 把模板复制到临时目录，模拟 `--add-data` 实际打进去的内容（不含 out/）
2. `sys._MEIPASS` 指向它，并把文件 chmod 成只读，模拟只读解包目录
3. 真的跑一次 render_shots，看能不能渲出帧

用法：
    python tools/verify_exe_form.py              # 渲 2 镜 / 4fps（约 30 秒）
    python tools/verify_exe_form.py --fps 8--shots 3
    python tools/verify_exe_form.py --check-only   # 只查环境与依赖解析，不渲染

注意：这里**跳过 FFmpeg 合成**（encode=False）。受控终端/沙箱常禁止 FFmpeg
读写磁盘并报看不懂的 "No such file or directory"，那是环境限制不是代码问题；
帧能渲出来就说明渲染链路是通的，合成在普通终端下另行验证。
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline import painted as pb  # noqa: E402

DEMO_SHOTS = [
    {"duration": 3.0, "narration": "老汤在瓦罐里咕嘟了八个小时",
     "camera": "缓慢推镜，中景", "act": "起"},
    {"duration": 3.0, "narration": "面条滑进碗里的那一秒最关键",
     "camera": "固定机位，特写", "act": "承"},
    {"duration": 3.0, "narration": "三分钟出一碗，讲究都在时间里",
     "camera": "缓慢拉镜，全景", "act": "转合"},
]


def simulate_bundle(verbose: bool = True) -> Path:
    """搭一个「假的 PyInstaller 解包目录」，返回 _MEIPASS 路径。

    三个细节必须做对，否则测出来的结论是假的：
    1. 目录结构是 <_MEIPASS>/vendor/painted-template —— 少一层 template_dir()
       就找不到模板，程序会以为不在打包环境，测试全部走空。
    2. 排除 out/ —— 那是渲染产物，--add-data 实际不会打进去（build_web.py 有排除）。
    3. chmod 只读 —— 权限位是本脚本要抓的主要 bug 之一（copy2 会连权限位一起复制）。
    """
    meipass = Path(tempfile.mkdtemp(prefix="fake_meipass_"))
    fake = meipass / "vendor" / "painted-template"
    fake.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        pb.template_dir(), fake,
        ignore=shutil.ignore_patterns("out", ".git", "__pycache__"),
    )
    for p in fake.rglob("*"):
        try:
            p.chmod(0o555 if p.is_dir() else 0o444)
        except OSError:
            pass  # 某些文件系统不支持权限位，跳过（后面会实测写权限）
    sys._MEIPASS = str(meipass)
    if verbose:
        print(f"模拟解包目录：{meipass}")
    return meipass


def _report(ok: bool, name: str, detail: str = "") -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    return ok


def check_bundle_form(verbose: bool = True) -> bool:
    """只查环境与依赖解析，不渲染（几秒钟，适合改完代码快速回归）。"""
    print("\n[1] 打包形态识别")
    ok = _report(pb._in_bundle(), "识别为打包环境（_in_bundle）")
    ok &= _report(pb.template_dir().exists(), "模板可定位", str(pb.template_dir()))

    print("\n[2] 工作副本")
    work = pb.work_dir()
    print(f"  work_dir = {work}")
    for name in ("render.mjs", "studio.html", "package.json",
                 "src/config.js", "src/core.js", "src/clawd.js",
                 "src/timeline.js", "src/scenes"):
        ok &= _report((work / name).exists(), f"已复制 {name}")

    print("\n[3] 依赖解析（打包形态最易炸的地方）")
    mods = work / "node_modules"
    ok &= _report(mods.exists(), "node_modules 可从工作副本解析")
    ok &= _report((mods / "p5/lib/p5.min.js").exists(), "p5 可解析（studio.html 要加载）")
    ok &= _report((mods / "puppeteer-core/package.json").exists(),
                  "puppeteer-core 可解析（render.mjs 用 ESM import）")
    prefix = pb.modules_prefix()
    print(f"  modules_prefix = {prefix}")
    if pb._in_bundle():
        # 打包形态下工作副本里没有 node_modules，p5 必须走绝对路径去模板目录加载。
        # 反过来说，如果 prefix 还是相对的"node_modules"，studio.html 一定加载失败。
        ok &= _report(prefix.startswith(("http://", "https://", "/", "file:")) or ":" in prefix,
                      "p5 用绝对路径加载（EXE 下工作副本无 node_modules）",
                      "" if ":" in prefix else f"仍是相对路径：{prefix}")

    print("\n[4] 写权限（copy2 会连只读权限一起复制）")
    probe = work / "src" / "config.js"
    try:
        probe.write_text(probe.read_text(encoding="utf-8"), encoding="utf-8")
        ok &= _report(True, "工作副本可写")
    except OSError as exc:
        ok &= _report(False, "工作副本可写", str(exc))

    print("\n[5] 体检接口")
    st = pb.check_env()
    ok &= _report(st.ok, "check_env() 通过", st.detail)
    return bool(ok)


def render(shots: list[dict], fps: int, workers: int) -> bool:
    print(f"\n[6] 真实渲染 {len(shots)} 镜 / {fps}fps（跳过 FFmpeg 合成）")
    out_dir = ROOT / "work" / "verify_exe_form"
    res = pb.render_shots(shots, out_dir, genre_label="烟火美食", fps=fps,
                          workers=workers, encode=False,
                          log=lambda m: print("  ", m),
                          progress=lambda l: print("  >", l) if "ms/frame" in l else None)
    print(f"  ok={res['ok']} frames={res['frames']} err={res.get('error', '')}")
    return bool(res.get("ok") and res.get("frames"))


def main() -> int:
    ap = argparse.ArgumentParser(description="验证代码绘制后端在打包形态（EXE）下可用")
    ap.add_argument("--fps", type=int, default=4, help="帧率（越低越快，默认 4）")
    ap.add_argument("--shots", type=int, default=2, help="镜头数（默认 2，最多 3）")
    ap.add_argument("--workers", type=int, default=4, help="并行渲染进程数")
    ap.add_argument("--check-only", action="store_true", help="只查环境与依赖，不渲染")
    args = ap.parse_args()

    print("=== 代码绘制 · 打包形态（EXE）验证 ===")
    print("背景：模板在 sys._MEIPASS 下只读，源码模式下测不出这类问题。")
    simulate_bundle()

    ok = check_bundle_form()
    if args.check_only:
        print("\n结果：" + ("全部通过" if ok else "有失败项"))
        return 0 if ok else 1

    if not ok:
        print("\n[跳过] 环境与依赖解析就没过，渲染必然失败，先修上面的问题。")
        return 1

    shots = DEMO_SHOTS[: max(1, min(args.shots, len(DEMO_SHOTS)))]
    ok &= render(shots, args.fps, args.workers)

    print("\n结果：" + ("全部通过 —— 打包形态可用" if ok else "有失败项"))
    print("提示：这只证明渲染链路可用。真实 EXE 还需在纯英文路径下双击跑一次 --selftest。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())