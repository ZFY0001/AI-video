"""一言片场 · 提示词库加载器。

真相源头只有一个：**docs/提示词库-V1.md**。
    docs/提示词库-V1.md  --tools/extract_prompts.py-->  prompts/prompts.yaml  --本模块--> 管线
代码里不允许再写死任何提示词文本，改提示词只改文档，然后重跑抽取。

用法：
    lib = PromptLibrary.load()
    sys_prompt = lib.system("1.2")
    user_msg   = lib.render_user("1.2", {"total_duration": 60, "project_brief_json": "..."})
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import yaml


def _resolve_catalog() -> Path:
    """定位提示词库文件。

    源码运行：在项目根的 prompts/ 下。
    打包运行：prompts/ 被 --add-data 打进包，落在 `sys._MEIPASS/prompts`，
    必须从那里读；用户目录下的 prompts/ 若存在则优先，便于用户自行修改提示词。
    """
    project_root = Path(__file__).resolve().parents[1]
    user_copy = project_root / "prompts" / "prompts.yaml"
    if user_copy.exists():
        return user_copy
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bundled = Path(meipass) / "prompts" / "prompts.yaml"
        if bundled.exists():
            return bundled
    return project_root / "prompts" / "prompts.yaml"


CATALOG = _resolve_catalog()

PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")


class PromptError(RuntimeError):
    pass


class PromptSlot:
    def __init__(self, section: str, name: str, text: str, source_line: int, lang: str = "text"):
        self.section = section
        self.name = name
        self.text = text
        self.source_line = source_line
        self.lang = lang

    @property
    def placeholders(self) -> list[str]:
        return PLACEHOLDER.findall(self.text)

    def render(self, values: dict[str, Any], strict: bool = True) -> str:
        missing: list[str] = []

        def sub(m: re.Match[str]) -> str:
            key = m.group(1)
            if key not in values:
                missing.append(key)
                return m.group(0)
            val = values[key]
            if isinstance(val, (dict, list)):
                import json

                return json.dumps(val, ensure_ascii=False, indent=2)
            return str(val)

        out = PLACEHOLDER.sub(sub, self.text)
        if strict and missing:
            raise PromptError(
                f"提示词 {self.section}/{self.name} 缺少变量：{sorted(set(missing))}"
            )
        return out

    def __repr__(self) -> str:  # pragma: no cover
        return f"<PromptSlot {self.section}/{self.name} L{self.source_line} {len(self.text)}字>"


class PromptLibrary:
    def __init__(self, data: dict[str, Any], path: Path):
        self.data = data
        self.path = path
        self.meta: dict[str, Any] = data.get("meta", {})
        self.sections: dict[str, dict[str, Any]] = data.get("sections", {})
        self._slots: dict[tuple[str, str], PromptSlot] = {}
        for num, sec in self.sections.items():
            for name, blk in sec.get("slots", {}).items():
                self._slots[(num, name)] = PromptSlot(
                    num, name, blk["text"], int(blk.get("source_line", 0)), blk.get("lang", "text")
                )

    # ------------------------------------------------------------ 加载
    @classmethod
    def load(cls, path: Path | None = None) -> "PromptLibrary":
        p = path or CATALOG
        if not p.exists():
            raise PromptError(
                f"找不到提示词库 {p}。请先运行：python tools/extract_prompts.py"
            )
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        return cls(data, p)

    # ------------------------------------------------------------ 取用
    def slot(self, section: str, name: str) -> PromptSlot:
        key = (section, name)
        if key not in self._slots:
            raise PromptError(f"提示词库中没有 {section}/{name}")
        return self._slots[key]

    def system(self, section: str) -> str:
        return self.slot(section, "system").text

    def user_template(self, section: str) -> str:
        return self.slot(section, "user").text

    def render_user(self, section: str, values: dict[str, Any], strict: bool = True) -> str:
        return self.slot(section, "user").render(values, strict=strict)

    def request_example(self, section: str) -> str:
        sec = self.sections.get(section, {})
        if "request_example" not in sec:
            raise PromptError(f"章节 {section} 没有请求体示例")
        return sec["request_example"]["text"]

    def title(self, section: str) -> str:
        return self.sections.get(section, {}).get("title", section)

    def negative(self) -> str:
        return self.slot("1.4", "negative").text

    # ------------------------------------------------------------ 自检
    def audit(self) -> dict[str, Any]:
        """提示词库自检：槽位齐全、占位符有清单、行号可溯源。"""
        expected = {
            "1.1": ["system", "user"],
            "1.2": ["system", "user"],
            "1.3": ["system", "user"],
            "1.4": ["system", "user", "negative"],
            "1.5": ["system", "user"],
            "2.1": ["system", "user"],
            "3.1": ["task"],
            "3.2": ["task"],
            "3.3": ["task"],
        }
        problems: list[str] = []
        for num, names in expected.items():
            for name in names:
                if (num, name) not in self._slots:
                    problems.append(f"缺少槽位 {num}/{name}")
        decisions = [n for n, s in self.sections.items() if s.get("layer") == "decision"]
        for num in decisions:
            sec = self.sections[num]
            if "request_example" not in sec:
                problems.append(f"决策章节 {num} 缺少 request_example")
            else:
                try:
                    import json

                    json.loads(sec["request_example"]["text"])
                except Exception as exc:  # noqa: BLE001
                    problems.append(f"决策章节 {num} 的请求体不是合法 JSON：{exc}")
        placeholders = {f"{k[0]}/{k[1]}": v.placeholders for k, v in self._slots.items()}
        return {
            "sections": len(self.sections),
            "slots": len(self._slots),
            "source": self.meta.get("source"),
            "source_sha256": self.meta.get("source_sha256"),
            "generated_on": self.meta.get("generated_on"),
            "placeholders": placeholders,
            "problems": problems,
        }


# ------------------------------------------------------------------ 自测
if __name__ == "__main__":  # pragma: no cover
    import json

    lib = PromptLibrary.load()
    print(json.dumps({k: v for k, v in lib.audit().items() if k != "placeholders"}, ensure_ascii=False, indent=2))
