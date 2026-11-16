"""把组件级 CSS 收进 @layer components，让 Tailwind utilities 能正常覆盖。

未分层的 `.field-input{width:100%}` 会压过 `@layer utilities` 里的 `w-[248px]`，
表现是「写了定宽却按容器宽度渲染」。分层后优先级回到 utilities > components > base。
"""
from pathlib import Path

p = Path(__file__).resolve().parents[2] / "web" / "src" / "index.css"
lines = p.read_text(encoding="utf-8").split("\n")

start = next(i for i, l in enumerate(lines) if l.startswith("/* ====") and "光场" in lines[i + 1])
head = lines[:start]
body = lines[start:]

wrapped = ["@layer components {"]
for line in body:
    wrapped.append(("  " + line) if line.strip() else line)
wrapped.append("}")

p.write_text("\n".join(head + wrapped), encoding="utf-8")
print(f"wrapped {len(body)} lines from line {start + 1}")