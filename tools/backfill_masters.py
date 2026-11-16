# -*- coding: utf-8 -*-
"""给没有母版清单的旧项目补一份 `output/master.json`。

旧项目拍在母版层之前，只有 `output/final.mp4`。没有 manifest 就拿不到内容
哈希与 artifact_id：导出缓存键与审计追溯对这批项目全部失效。

默认只演练（dry-run），不会写盘。确认无误后加 `--apply` 才真正回填。

用法：
    python tools/backfill_masters.py                # 列出待回填项目
    python tools/backfill_masters.py --apply        # 真正写入
    python tools/backfill_masters.py --runs <目录>  # 指定 runs 根目录（演练副本）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server import artifacts  # noqa: E402
from server.artifacts import MASTER_FILE  # noqa: E402


def scan(runs: Path) -> tuple[list[tuple[str, str]], list[str]]:
    """返回 (待回填[(项目, 成片)], 跳过[原因])。"""
    todo: list[tuple[str, str]] = []
    skipped: list[str] = []
    for entry in sorted(runs.iterdir()):
        if not entry.is_dir() or entry.name.startswith((".", "_")):
            continue
        target = entry / "output" / MASTER_FILE
        legacy = entry / "output" / "final.mp4"
        if target.exists():
            skipped.append(f"{entry.name}：已有 manifest")
        elif not legacy.is_file():
            skipped.append(f"{entry.name}：没有 output/final.mp4")
        else:
            todo.append((entry.name, str(legacy.relative_to(entry).as_posix())))
    return todo, skipped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="真正写盘（默认只演练）")
    parser.add_argument("--runs", default=str(ROOT / "runs"), help="runs 根目录")
    args = parser.parse_args()

    runs = Path(args.runs)
    if not runs.is_dir():
        print(f"目录不存在：{runs}")
        return 2

    todo, skipped = scan(runs)
    print(f"待回填 {len(todo)} 个，跳过 {len(skipped)} 个")
    for reason in skipped:
        print(f"  跳过 {reason}")

    if not todo:
        print("没有需要回填的项目。")
        return 0
    if not args.apply:
        for name, video in todo:
            print(f"  将回填 {name} <- {video}")
        print("\n这是演练。确认无误后加 --apply 真正写入。")
        return 0

    ok = 0
    failed = 0
    for name, video in todo:
        try:
            manifest = artifacts.backfill_master(runs / name, video)
        except Exception as exc:  # noqa: BLE001
            print(f"  [FAIL] {name}: {exc}")
            failed += 1
            continue
        if manifest is None:
            print(f"  [SKIP] {name}: 成片不可读或为零字节")
            failed += 1
            continue
        print(f"  [OK] {name}: sha256={str(manifest.get('sha256'))[:12]}… "
              f"size={manifest.get('size')}")
        ok += 1
    print(f"\n完成：{ok} 个已回填，{failed} 个失败")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
