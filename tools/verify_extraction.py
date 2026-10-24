#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
抽取保真校验：把 prompts/prompts.yaml 里的每段提示词，逐行回查 docs/提示词库-V1.md，
确认**一个字都没被改动**（顺序一致、内容一致、行号对得上）。

用法：python tools/verify_extraction.py
退出码 0 = 完全一致；1 = 存在差异（会打印差异明细）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from extract_prompts import PROMPT_DOC, read_lines  # noqa: E402

YAML = ROOT / "prompts" / "prompts.yaml"


# ---------------------------------------------------------------- 极简 YAML 读取
def load_yaml(path: Path) -> dict:
    """只支持本产物用到的子集：嵌套 map、块标量 |、普通标量、简单列表。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    lines = [ln for ln in lines if not ln.lstrip().startswith("#")]

    def scalar(text: str) -> str:
        text = text.strip()
        if len(text) >= 2 and text[0] == text[-1] == '"':
            return text[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        return text

    def parse_block(i: int, indent: int):
        """返回 (值, 下一行索引)。"""
        node: dict = {}
        while i < len(lines):
            ln = lines[i]
            if not ln.strip():
                i += 1
                continue
            ind = len(ln) - len(ln.lstrip(" "))
            if ind < indent:
                break
            if ind > indent:
                raise ValueError(f"意外缩进：{ln!r}")
            key, _, rest = ln.strip().partition(":")
            rest = rest.strip()
            if rest == "|":
                i += 1
                buf = []
                while i < len(lines):
                    cur = lines[i]
                    if cur.strip() and (len(cur) - len(cur.lstrip(" "))) <= indent:
                        break
                    buf.append(cur[indent + 2 :] if len(cur) > indent + 2 else "")
                    i += 1
                while buf and not buf[-1].strip():
                    buf.pop()
                node[key] = "\n".join(buf)
            elif rest == "":
                child, i = parse_block(i + 1, indent + 2)
                node[key] = child
            elif rest in ("{}", "[]"):
                node[key] = {} if rest == "{}" else []
                i += 1
            else:
                node[key] = scalar(rest)
                i += 1
        return node, i

    tree, _ = parse_block(0, 0)
    return tree


def main() -> int:
    if not YAML.exists():
        print("[失败] 缺少 prompts/prompts.yaml，请先运行 tools/extract_prompts.py")
        return 1
    src_raw = read_lines(PROMPT_DOC)
    catalog = load_yaml(YAML)

    problems: list[str] = []
    checked = 0
    checked_chars = 0

    for num, sec in catalog["sections"].items():
        blobs: list[tuple[str, dict]] = [(k, v) for k, v in (sec.get("slots") or {}).items()]
        if "request_example" in sec:
            blobs.append(("request_example", sec["request_example"]))
        for slot, blk in blobs:
            checked += 1
            text = blk["text"]
            checked_chars += len(text)
            line_no = int(blk["source_line"])
            body = text.split("\n")
            # source_line 是 1-based 的围栏行号；切片用 0-based，正文从围栏的下一行开始
            actual = src_raw[line_no : line_no + len(body)]
            if not src_raw[line_no - 1].strip().startswith("```"):
                problems.append(
                    f"章节 {num}/{slot} 行号错位：L{line_no} 不是围栏行 → {src_raw[line_no - 1]!r}"
                )
                continue
            if actual != body:
                problems.append(
                    f"章节 {num}/{slot}（源行 L{line_no}）内容不一致：\n"
                    f"    文档: {actual[:1]!r} … 共 {len(actual)} 行\n"
                    f"    产物: {body[:1]!r} … 共 {len(body)} 行"
                )
                for j, (a, b) in enumerate(zip(actual, body)):
                    if a != b:
                        problems.append(f"      首个差异在第 {j + 1} 行：文档 {a!r} / 产物 {b!r}")
                        break

    # 统计文档中的围栏数，确认没有漏抽的提示词块
    fences = sum(1 for ln in src_raw if ln.strip().startswith("```"))
    extracted_blocks = sum(
        len(sec.get("slots") or {}) + (1 if "request_example" in sec else 0)
        for sec in catalog["sections"].values()
    )

    print("=== 提示词抽取保真校验 ===")
    print("源文档           : docs/提示词库-V1.md")
    print(f"文档围栏数       : {fences}（含提示词内嵌 JSON 契约的外层+内层围栏）")
    print(f"抽出提示词块     : {extracted_blocks} 个，共 {checked_chars} 字符")
    print(f"逐行回查         : {checked} 块全部比对")
    if problems:
        print(f"\n[失败] 发现 {len(problems)} 处差异：")
        for p in problems:
            print("  - " + p)
        return 1
    print("\n[通过] 每一段提示词与源文档逐行一致，零改动。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
