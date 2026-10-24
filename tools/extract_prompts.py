#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
从 docs/ 下的 Markdown 规范文档抽取提示词，生成结构化提示词库。

设计原则：**文档是唯一事实源头**。
    docs/提示词库-V1.md  ──抽取──▶  prompts/prompts.yaml  ──加载──▶  管线代码
抽取结果带源文件与行号，可随时校验是否与文档同步（--check）。

用法：
    python tools/extract_prompts.py            # 生成 prompts/prompts.yaml + docs/抽取校验报告.md
    python tools/extract_prompts.py --check     # 只校验：产物是否与文档一致，不一致退出码 2
    python tools/extract_prompts.py --report    # 只打印抽取报告
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
PROMPT_DOC = DOCS / "提示词库-V1.md"
TECH_DOC = DOCS / "技术方案-V1.md"
OUT_YAML = ROOT / "prompts" / "prompts.yaml"
OUT_REPORT = DOCS / "抽取校验报告.md"

GENERATED_HEADER = "# 本文件由 tools/extract_prompts.py 从 docs/提示词库-V1.md 自动生成，请勿手改。\n"

# 需要的章节：编号 -> (名称, 层, 期望的代码块标签顺序)
SECTIONS: dict[str, tuple[str, str, list[str]]] = {
    "1.1": ("输入层 · 项目简报规范化", "creative", ["system", "user"]),
    "1.2": ("剧本生成", "creative", ["system", "user"]),
    "1.3": ("分镜拆解", "creative", ["system", "user"]),
    "1.4": ("文生图提示词工程", "creative", ["system", "user", "negative"]),
    "1.5": ("图生视频运动提示词工程", "creative", ["system", "user"]),
    "2.1": ("候选图文字描述（质检前置步骤）", "vision", ["system", "user"]),
    "2.2": ("分镜模型路由（model-route 预设）", "decision", []),
    "2.3": ("任务分级（route 预设）", "decision", []),
    "2.4": ("生成质检（原生 decisions）", "decision", []),
    "2.5": ("候选 N 选 1（原生 choice）", "decision", []),
    "2.6": ("重试策略（原生 choice）", "decision", []),
    "2.7": ("完成度终检（completion 预设）", "decision", []),
    "2.8": ("高危操作护栏（tool-guard 预设）", "decision", []),
    "3.1": ("工作流理解（社区工作流 → 说明书）", "rea", ["task"]),
    "3.2": ("工作流改造（说明书 → 项目定制版）", "rea", ["task"]),
    "3.3": ("工作流筛选（收集阶段）", "rea", ["task"]),
}

Z = "```"
# 只删表格对齐行（|-|-| 或 |:--:|），绝不误删提示词里的 "- 列表项"
HR = re.compile(r"^\|\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
HEAD = re.compile(r"^##\s+(\d+\.\d+)\s+(.*)$")
BLOCK_LABEL = re.compile(r"^\*\*(.+?)：?\*\*\s*$")
LABEL_TO_SLOT = {
    "系统提示词": "system",
    "用户消息模板": "user",
    "配套负面提示词（全局默认，可按题材增删）": "negative",
    "配套负面提示词": "negative",
}


@dataclass
class Block:
    """一个从文档中抽出的代码块。"""

    label: str
    lang: str
    text: str
    line: int
    inner_blocks: list["Block"] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {"label": self.label, "lang": self.lang, "line": self.line, "text": self.text}
        if self.inner_blocks:
            d["inner_blocks"] = [b.to_dict() for b in self.inner_blocks]
        return d


@dataclass
class Section:
    number: str
    title: str
    layer: str
    line: int
    blocks: list[Block]

    @property
    def block_map(self) -> dict[str, Block]:
        return {b.label: b for b in self.blocks}

    def to_dict(self) -> dict:
        return {
            "number": self.number,
            "title": self.title,
            "layer": self.layer,
            "source_line": self.line,
            "blocks": [b.to_dict() for b in self.blocks],
        }


def read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def numbered_lines(path: Path) -> list[tuple[int, str]]:
    """返回 [(真实行号, 文本)]，已剔除 Markdown 表格的对其行（它会让行号错位）。"""
    out: list[tuple[int, str]] = []
    for idx, ln in enumerate(read_lines(path), start=1):
        if HR.match(ln):
            continue
        out.append((idx, ln))
    return out


def parse_sections(numbered: list[tuple[int, str]]) -> list[Section]:
    """扫描全文，按 `## 1.1 标题` 切分，并抽取其中的围栏代码块。"""
    starts: list[tuple[int, int, str, str]] = []  # (列表下标, 真实行号, 编号, 标题)
    for i, (line_no, ln) in enumerate(numbered):
        m = HEAD.match(ln)
        if m and m.group(1) in SECTIONS:
            starts.append((i, line_no, m.group(1), m.group(2).strip()))

    sections: list[Section] = []
    for idx, (start, start_line, number, title) in enumerate(starts):
        end = starts[idx + 1][0] if idx + 1 < len(starts) else len(numbered)
        body = numbered[start + 1 : end]
        blocks = parse_body(body)
        sections.append(
            Section(
                number=number,
                title=title,
                layer=SECTIONS[number][1],
                line=start_line,
                blocks=blocks,
            )
        )
    return sections


def parse_body(body: list[tuple[int, str]]) -> list[Block]:
    """逐个围栏扫描。

    规则：裸 ``` 收尾当前块；带语言标记的 ```json 视为块内嵌的契约/示例（原样保留）。
    body 元素为 (真实行号, 文本)，保证产出的 line 能指回原文档。
    """
    blocks: list[Block] = []
    pending_label: str | None = None
    inner_label: str | None = None
    i = 0
    while i < len(body):
        line_no, raw = body[i]
        stripped = raw.strip()
        if stripped.startswith("#"):
            i += 1
            continue
        lm = BLOCK_LABEL.match(stripped)
        if lm and not stripped.startswith(Z):
            name = lm.group(1).strip()
            pending_label = LABEL_TO_SLOT.get(name, name)
            i += 1
            continue
        if stripped.startswith(Z) and not stripped.startswith(Z * 4):
            lang = stripped[3:].strip()
            cursor = i + 1
            outer: list[str] = []
            inner: list[Block] = []
            inner_buf: list[str] | None = None
            inner_lang = ""
            inner_start = 0
            while cursor < len(body):
                cur_line, cur = body[cursor]
                cur_s = cur.strip()
                is_fence = cur_s.startswith(Z) and not cur_s.startswith(Z * 4)
                has_lang = is_fence and len(cur_s) > 3
                if is_fence and not has_lang:
                    break  # 裸 ``` 收尾
                if inner_buf is None:
                    if has_lang:
                        inner_lang = cur_s[3:].strip()
                        inner_start = cur_line
                        inner_buf = []
                    else:
                        inner_lm = BLOCK_LABEL.match(cur_s)
                        if inner_lm:
                            inner_label = LABEL_TO_SLOT.get(
                                inner_lm.group(1).strip(), inner_lm.group(1).strip()
                            )
                        outer.append(cur)
                else:
                    if has_lang:
                        inner.append(
                            Block(
                                label=inner_label or f"inner_{len(inner) + 1}",
                                lang=inner_lang,
                                text="\n".join(inner_buf).strip("\n"),
                                line=inner_start,
                            )
                        )
                        inner_label = None
                        inner_lang = cur_s[3:].strip()
                        inner_start = cur_line
                        inner_buf = []
                    else:
                        inner_buf.append(cur)
                cursor += 1
            if inner_buf is not None:
                inner.append(
                    Block(
                        label=inner_label or f"inner_{len(inner) + 1}",
                        lang=inner_lang,
                        text="\n".join(inner_buf).strip("\n"),
                        line=inner_start,
                    )
                )
            while outer and not outer[-1].strip():
                outer.pop()
            blocks.append(
                Block(
                    label=pending_label or f"block_{len(blocks) + 1}",
                    lang=lang,
                    text="\n".join(outer).strip("\n"),
                    line=line_no,
                    inner_blocks=inner,
                )
            )
            pending_label = None
            i = cursor + 1
            continue
        i += 1
    return blocks


def collect_unlabeled_inner(section: Section, label: str) -> list[Block]:
    """取某个外层块里的内嵌代码块（例如 1.1 的 JSON 契约示例）。"""
    for b in section.blocks:
        if b.label == label:
            return b.inner_blocks
    return []


def build_catalog(sections: list[Section], source_sha: str) -> dict:
    catalog: dict = {
        "meta": {
            "generated_by": "tools/extract_prompts.py",
            "generated_on": date.today().isoformat(),
            "source": "docs/提示词库-V1.md",
            "source_sha256": source_sha,
            "note": "文档为唯一事实源头；本产物只做结构化，不修改任何提示词文字。",
        },
        "sections": {},
    }
    for s in sections:
        bmap = s.block_map
        # 未标注 **加粗标签** 的裸代码块，按期望槽位顺序兜底认领（如 3.1/3.2/3.3 的 REA 任务提示词）
        spare = [b for b in s.blocks if re.fullmatch(r"block_\d+", b.label)]
        spare_iter = iter(spare)
        slots: dict = {}
        for slot in SECTIONS[s.number][2]:
            blk = bmap.get(slot)
            if blk is None:
                blk = next(spare_iter, None)
            if blk is None:
                raise SystemExit(f"[抽取失败] 章节 {s.number} 缺少代码块：{slot}")
            slots[slot] = blk.to_dict()
        contracts = collect_unlabeled_inner(s, "system")
        entry: dict = {
            "title": s.title,
            "layer": s.layer,
            "source_line": s.line,
            "slots": slots,
        }
        if s.layer == "decision":
            request = next((b for b in s.blocks if b.lang == "json"), None)
            if request is None:
                raise SystemExit(f"[抽取失败] 章节 {s.number} 缺少 json 请求体代码块")
            entry["request_example"] = request.to_dict()
        if contracts:
            entry["inner_contracts"] = [b.to_dict() for b in contracts]
        catalog["sections"][s.number] = entry
    return catalog


# ---------------------------------------------------------------- YAML 输出
def yaml_scalar(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value)
    if s == "" or s.strip() != s or "\n" in s or s[0] in "-?:,[]{}#&*!|>'\"%@`" or ": " in s:
        return yaml_block(s)
    return s


def yaml_block(s: str) -> str:
    """用 YAML 块标量（|）输出多行或含特殊字符的字符串，保持文字零改动。"""
    lines = s.split("\n")
    if len(lines) == 1 and not needs_block(s):
        return '"%s"' % s.replace("\\", "\\\\").replace('"', '\\"')
    indent = " " * 2
    out = ["|"]
    for ln in lines:
        out.append((indent + ln).rstrip() if ln.strip() else "")
    return "\n".join(out)


def needs_block(s: str) -> bool:
    return (
        "\n" in s
        or s == ""
        or s.strip() != s
        or s[0] in "-?:,[]{}#&*!|>'\"%@`"
        or ": " in s
        or " #" in s
    )


def dump_yaml(obj, indent: int = 0, key_ctx: bool = False) -> str:
    pad = " " * indent
    if isinstance(obj, dict):
        if not obj:
            return pad + "{}"
        out = []
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                if isinstance(v, dict) and not v:
                    out.append(f"{pad}{k}: {{}}")
                elif isinstance(v, list) and not v:
                    out.append(f"{pad}{k}: []")
                else:
                    out.append(f"{pad}{k}:")
                    out.append(dump_yaml(v, indent + 2))
            else:
                rendered = yaml_scalar(v)
                if "\n" in rendered or rendered.startswith("|"):
                    out.append(f"{pad}{k}: {rendered}")
                else:
                    out.append(f"{pad}{k}: {rendered}")
        return "\n".join(out)
    if isinstance(obj, list):
        if not obj:
            return pad + "[]"
        out = []
        for item in obj:
            if isinstance(item, (dict, list)):
                rendered = dump_yaml(item, indent + 2)
                rendered = rendered[indent + 2 :]
                out.append(f"{pad}- {rendered.lstrip()}")
                for extra in rendered.split("\n")[1:]:
                    out.append(extra)
            else:
                rendered = yaml_scalar(item)
                out.append(f"{pad}- {rendered}")
        return "\n".join(out)
    return pad + yaml_scalar(obj)


def render_catalog(catalog: dict) -> str:
    """手写渲染，保证 YAML 合法且中文/标点零转义。"""
    lines: list[str] = [GENERATED_HEADER.rstrip("\n"), ""]

    def block_scalar(key: str, text: str, indent: int) -> list[str]:
        pad = " " * indent
        body = text.split("\n")
        out = [f"{pad}{key}: |"]
        for ln in body:
            out.append((pad + "  " + ln).rstrip() if ln.strip() else "")
        return out

    meta = catalog["meta"]
    lines.append("meta:")
    for k, v in meta.items():
        lines.append(f"  {k}: {v}")
    lines.append("")
    lines.append("sections:")

    for num, sec in catalog["sections"].items():
        lines.append(f'  "{num}":')
        lines.append(f"    title: {sec['title']}")
        lines.append(f"    layer: {sec['layer']}")
        lines.append(f"    source_line: {sec['source_line']}")
        lines.append("    slots:")
        if not sec["slots"]:
            lines[-1] = "    slots: {}"
        for slot, blk in sec["slots"].items():
            lines.append(f"      {slot}:")
            lines.append(f"        lang: {blk['lang'] or 'text'}")
            lines.append(f"        source_line: {blk['line']}")
            lines.extend(block_scalar("text", blk["text"], 8))
        if "request_example" in sec:
            blk = sec["request_example"]
            lines.append("    request_example:")
            lines.append(f"      lang: {blk['lang']}")
            lines.append(f"      source_line: {blk['line']}")
            lines.extend(block_scalar("text", blk["text"], 6))
        if "inner_contracts" in sec:
            lines.append("    inner_contracts:")
            for blk in sec["inner_contracts"]:
                lines.append(f"      - label: {blk['label']}")
                lines.append(f"        lang: {blk['lang'] or 'text'}")
                lines.append(f"        source_line: {blk['line']}")
                lines.extend(block_scalar("text", blk["text"], 8))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------- 报告输出
def render_report(catalog: dict, sections: list[Section], stored_sha: str | None) -> str:
    meta = catalog["meta"]
    total_chars = sum(
        len(b["text"]) for sec in catalog["sections"].values() for b in sec["slots"].values()
    )
    total_inner = sum(
        len(b["text"]) for sec in catalog["sections"].values() for b in sec.get("inner_contracts", [])
    )
    lines = [
        "# 提示词抽取校验报告",
        "",
        "| 项 | 值 |",
        "|---|---|",
        "| 生成工具 | tools/extract_prompts.py |",
        f"| 生成日期 | {meta['generated_on']} |",
        f"| 源文档 | {meta['source']} |",
        f"| 源文档 SHA256 | `{meta['source_sha256'][:16]}…` |",
        f"| 抽取章节数 | {len(catalog['sections'])} |",
        f"| 抽取提示词字符数 | {total_chars}（另含内嵌契约 {total_inner} 字符） |",
        "",
        "## 逐章节抽取结果",
        "",
        "| 章节 | 名称 | 层 | 源行 | 抽出槽位 | 字符数 |",
        "|---|---|---|---|---|---|",
    ]
    for num, sec in catalog["sections"].items():
        slots = "、".join(
            f"{k}({len(v['text'])})" for k, v in sec["slots"].items()
        )
        chars = sum(len(v["text"]) for v in sec["slots"].values())
        extra = " + request_example" if "request_example" in sec else ""
        lines.append(
            f"| {num} | {sec['title']} | {sec['layer']} | L{sec['source_line']} | {slots}{extra} | {chars} |"
        )
    lines += [
        "",
        "## 完整性校验",
        "",
        "| 校验项 | 结果 |",
        "|---|---|",
        f"| 期望章节全部命中（{len(SECTIONS)} 节） | {'✅ 通过' if len(catalog['sections']) == len(SECTIONS) else '❌ 缺失'} |",
        f"| 每个创意/视觉章节含 system + user | {'✅ 通过' if _all_have(catalog, 'creative') and _all_have(catalog, 'vision') else '❌ 失败'} |",
        f"| 每个决策章节含 json 请求体 | {'✅ 通过' if all('request_example' in s for s in catalog['sections'].values() if s['layer'] == 'decision') else '❌ 失败'} |",
        f"| 产物与文档同步（SHA256） | {'✅ 一致' if stored_sha == meta['source_sha256'] else '⚠️ 已更新（本次重新生成）'} |",
        "",
        "## 说明",
        "",
        "- 抽取过程**不改动任何一个字**：提示词原文按块标量写入 `prompts/prompts.yaml`，占位符 `{{...}}` 保持原样。",
        "- 决策层请求体（2.2-2.8）以 `request_example` 形式保存，管线运行时先做占位符替换，再送 Jev。",
        "- 1.1 的 JSON 契约、1.3 的 storyboard 契约在提示词内部，已原文保留（`inner_contracts` 记录其行号便于溯源）。",
        "- 文档改动后重新运行本工具即可同步；`--check` 用于 CI 拦截「文档改了口径但代码没跟上」。",
        "",
    ]
    return "\n".join(lines)


def _all_have(catalog: dict, layer: str) -> bool:
    for sec in catalog["sections"].values():
        if sec["layer"] == layer and not {"system", "user"} <= set(sec["slots"]):
            return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="从 docs 抽取提示词，生成结构化提示词库")
    ap.add_argument("--check", action="store_true", help="只校验产物是否与文档一致")
    ap.add_argument("--report", action="store_true", help="只打印抽取报告")
    args = ap.parse_args()

    if not PROMPT_DOC.exists():
        print(f"[错误] 找不到源文档：{PROMPT_DOC}")
        return 1
    raw = PROMPT_DOC.read_text(encoding="utf-8")
    sha = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    numbered = numbered_lines(PROMPT_DOC)
    sections = parse_sections(numbered)
    catalog = build_catalog(sections, sha)
    yaml_text = render_catalog(catalog)

    if args.report:
        print(render_report(catalog, sections, sha))
        return 0

    if args.check:
        if not OUT_YAML.exists():
            print("[校验失败] 产物不存在，请先运行 python tools/extract_prompts.py")
            return 2
        current = OUT_YAML.read_text(encoding="utf-8")
        if current != yaml_text:
            print("[校验失败] prompts/prompts.yaml 与 docs/提示词库-V1.md 不同步，请重新运行抽取。")
            return 2
        print(f"[校验通过] 提示词库与文档一致（{len(catalog['sections'])} 节，SHA256 {sha[:16]}…）")
        return 0

    OUT_YAML.parent.mkdir(parents=True, exist_ok=True)
    OUT_YAML.write_text(yaml_text, encoding="utf-8")
    report = render_report(catalog, sections, sha)
    OUT_REPORT.write_text(report, encoding="utf-8")
    n_blocks = sum(len(s["slots"]) for s in catalog["sections"].values())
    print(f"[完成] 抽取 {len(catalog['sections'])} 节 / {n_blocks} 个提示词槽位 → {OUT_YAML.relative_to(ROOT)}")
    print(f"[完成] 校验报告 → {OUT_REPORT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
