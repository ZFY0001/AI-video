# -*- coding: utf-8 -*-
"""真实大模型通路验证：接上 key 之后，管线**真的**会走大模型吗？

为什么需要这个工具
------------------
管线对 LLM 是**逐阶段降级**的：`_ask_json` 解析失败或缺必需字段时，会往
`creative.degradations` 记一条、然后**退回离线引擎继续出片**。
这个设计本身是对的（不会因为模型抽风就整片失败），但副作用是：

    **key 填错、端点写错、余额不足 —— 片子照样出来，只是文案是离线引擎写的。**

用户只会觉得"我明明接了 key，文案怎么还是这么平"，却看不出哪里不对。

更糟的是：**默认离线路径本身测试得很充分，而真实模型路径只有在真的有 key 时才被走到。**
所以这里用一个**模拟 OpenAI 兼容服务**把真实通路完整跑一遍：
mock 返回的内容里带一个 ASCII 标记，最终如果成片/字幕里出现这个标记，
就证明真实模型通路真的被走到了（离线引擎不可能凭空造出这个标记）。

覆盖两条路径
------------
1. **通**：指向 mock → 标记必须出现在产出的 narration / 分镜里，且无「创意层降级」
2. **不通**：指向一个死端口 → 片子仍要出得来（降级兜底），且**必须**留下降级记录

用法：
    python tools/verify_llm_live.py            # 默认 9 秒
    python tools/verify_llm_live.py --duration 6
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

#: 只有真实模型通路才可能产出这个标记（离线引擎的语料里没有）
MARKER = "LIVEPATH"

#: 离线兜底句。最终字幕里出现这些，说明解说词没被用上（空镜了）。
#: 注意：**这不等于真实模型没被调用**——`distribute_narration` 会按每镜时长
#: 重新切分解说词，句子比镜头预算长就整句丢弃，丢弃光了才触发兜底。
#: 所以样本句必须与镜头时长匹配（本工具默认 12s / 3 镜 = 4s/镜，预算 16 字）。
FILLERS = ("镜头继续。", "画面还在走。", "故事没有停。")

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def _mock_body(duration: int) -> dict:
    """构造一份**同时满足三个阶段**的合法返回。

    简报要 `theme`、剧本要 `narration_full`、分镜要 `shots` —— 干脆一次都给，
    这样不管 mock 被问到哪一阶段，返回都是合法的。结构直接借用离线生成器
    保证形状正确，只在文案里塞标记。
    """
    from pipeline.llm import _offline_brief, _offline_script, _offline_storyboard
    from pipeline.models import ProjectBrief, Script

    bd = _offline_brief("方城烩面")
    brief = ProjectBrief(
        theme="方城烩面（真实模型通路验证）",
        total_duration=duration,
        aspect_ratio="16:9",
        style="烟火气、暖色调、纪实风",
        language="zh",
    )
    sd = _offline_script(brief)
    # 解说词按 4 秒/镜的预算（16 字）写：每句都装得下，且两句连排会超预算，
    # 于是贪心分配正好一镜一句、不剩句子、不触发空镜兜底。
    narration = f"{MARKER}。羊骨吊足八小时，汤色才会发白。面条要现揉现拉。"
    script = Script(
        title=f"{MARKER} 一碗面的时间",
        logline=f"{MARKER} 凌晨四点的汤，熬到天亮。",
        tone="暖",
        target_audience="本地受众",
        hook=f"{MARKER} 先看这一碗。",
        acts=list(sd.get("acts") or []),
        narration_full=narration,
        key_subjects=list(sd.get("key_subjects") or []),
    )
    sb = _offline_storyboard(brief, script, max(2.0, duration / 3.0))
    # 每镜解说也塞上标记，方便在最终字幕里定位
    for i, shot in enumerate(sb.get("shots") or []):
        shot["narration"] = f"{MARKER}。第 {i + 1} 个镜头的旁白。"
        shot["visual"] = f"{MARKER} 主体清晰的水彩画面 {i + 1}"
    sb["project"] = {"title": script.title, "style": brief.style,
                     "aspect_ratio": "16:9", "total_duration": duration}
    return {**bd, **script.to_dict(), **sb}


def _serve_mock() -> tuple[HTTPServer, int, list[str]]:
    """起一个 OpenAI 兼容的假服务，记录收到的请求路径。"""
    seen: list[str] = []
    payload = _mock_body(_DURATION)

    class H(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            seen.append(self.path)
            n = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(n)
            body = json.dumps({
                "id": "chatcmpl-mock",
                "object": "chat.completion",
                "model": "deepseek-chat",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant",
                                "content": json.dumps(payload, ensure_ascii=False)},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # noqa: D102
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1], seen


def _run_cli(env_extra: dict, duration: int, tag: str) -> tuple[int, str, Path]:
    """跑一次 CLI，并**把真实的 .env 挪开**。

    为什么必须挪开：`Settings` 的优先级是 **.env > 系统环境变量 > 默认值**
    （见 pipeline/config.py 的说明：".env 里出现过的键一律以 .env 为准"）。
    本工具靠环境变量注入模拟端点，如果 .env 里已经有真实的 `LLM_BASE_URL`
    和 `LLM_API_KEY`（用户在界面里保存过），**真端点会把模拟端点盖掉**——
    于是这个验证变成了真的去调用户的付费账号，既拿不到预期的标记，
    又白白花钱。实测踩过：一次全绿的工具突然 12/18，就是 .env 被填了真 key。

    所以这里临时把 .env 改名，跑完立刻恢复（finally 保证）。
    """
    env = dict(os.environ)
    env.update(env_extra)
    env["PYTHONIOENCODING"] = "utf-8"

    env_file = ROOT / ".env"
    backup = ROOT / ".env.verify-backup"
    moved = False
    if env_file.exists():
        if backup.exists():
            backup.unlink()
        env_file.rename(backup)
        moved = True
    before = {p.name for p in (ROOT / "runs").glob("*") if p.is_dir()}
    try:
        proc = subprocess.run(
            [sys.executable, "yiyan.py", "方城烩面", "--live", "--duration", str(duration),
             "--no-cache"],
            cwd=str(ROOT), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=900,
        )
    finally:
        if moved:
            backup.rename(env_file)      # 绝对要恢复，否则会弄丢用户的 key
    after = {p.name for p in (ROOT / "runs").glob("*") if p.is_dir()}
    fresh = sorted(after - before)
    run_dir = (ROOT / "runs" / fresh[-1]) if fresh else Path("")
    return proc.returncode, (proc.stdout or "") + (proc.stderr or ""), run_dir


def _read_run(run_dir: Path) -> str:
    """把一次运行的所有文本产物拼起来，便于找标记。"""
    chunks: list[str] = []
    for pat in ("output/storyboard.json", "output/subtitles.srt", "summary.md",
                "output/narration.txt", "run.log"):
        p = run_dir / pat
        if p.exists():
            chunks.append(p.read_text(encoding="utf-8", errors="replace"))
    for p in run_dir.rglob("*.json"):
        try:
            chunks.append(p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass
    return "\n".join(chunks)


def main() -> int:
    global _DURATION
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=int, default=12,
                    help="总时长。默认 12 秒（3 镜 × 4 秒），使解说词预算与样本句匹配")
    args = ap.parse_args()
    _DURATION = args.duration

    print("=" * 74)
    print(f"真实大模型通路验证（模拟 OpenAI 兼容服务，标记 {MARKER}）")
    print("=" * 74)

    # ---------- 0. 端点填法容错（DeepSeek 的几种常见写法）----------
    print("\n[0] 端点填法容错（决定你 key 旁边那一栏怎么填）")
    from pipeline.llm import LLMError, OpenAIChatClient

    want = "https://api.deepseek.com/v1/chat/completions"
    for form in ("https://api.deepseek.com", "https://api.deepseek.com/",
                 "https://api.deepseek.com/v1", "https://api.deepseek.com/v1/",
                 want):
        c = OpenAIChatClient(base_url=form, api_key="x", model="deepseek-chat")
        got = c._endpoint()
        check(f"填 {form} → {want}", got == want, f"得到 {got}")
    try:
        OpenAIChatClient(base_url="api.deepseek.com", api_key="x", model="m")._endpoint()
        check("缺 http:// 前缀时明确报错", False, "没有报错")
    except LLMError as exc:
        check("缺 http:// 前缀时明确报错", "http" in str(exc), str(exc))

    # ---------- 1. 通：真实模型通路真的被走到 ----------
    print("\n[1] 指向可用的模型服务（模拟 DeepSeek）")
    srv, port, seen = _serve_mock()
    try:
        code, out, run_dir = _run_cli({
            "LLM_BASE_URL": f"http://127.0.0.1:{port}/v1",
            "LLM_API_KEY": "sk-mock-for-verification",
            "LLM_MODEL": "deepseek-chat",
        }, args.duration, "ok")
        check("整条管线跑通（退出码 0）", code == 0, out[-400:])
        check("运行目录已生成", bool(run_dir and run_dir.exists()), str(run_dir))
        check("模型服务确实被调用了", len(seen) >= 3,
              f"只收到 {len(seen)} 次请求：{seen}")
        print(f"    服务收到 {len(seen)} 次请求，路径样例：{sorted(set(seen))}")

        text = _read_run(run_dir) if run_dir.exists() else ""
        check(f"产出里出现标记 {MARKER}（证明走的是真实模型，不是离线引擎）",
              MARKER in text, f"产物文本 {len(text)} 字符里没找到标记")
        check("没有「创意层降级」记录",
              "创意层降级" not in text and "创意层降级" not in out,
              "出现了降级，说明模型返回没被采纳")

        srt = run_dir / "output" / "subtitles.srt"
        if srt.exists():
            srt_text = srt.read_text(encoding="utf-8", errors="replace")
            check("最终字幕用的是模型给的解说词", MARKER in srt_text, srt_text[:240])
            hit = [f for f in FILLERS if f in srt_text]
            check("字幕里没有空镜兜底句（说明解说词真的排进了镜头）",
                  not hit, f"出现兜底句：{hit}")
        vid = run_dir / "output" / "final.mp4"
        check("成片 MP4 已产出", vid.exists() and vid.stat().st_size > 10000,
              f"{vid} 存在={vid.exists()}")
    finally:
        srv.shutdown()
        srv.server_close()

    # ---------- 2. 端点不通时的**真实行为**（如实断言，不按我的猜测写）----------
    print("\n[2] 指向一个连不上的端点（模拟 key 填错 / 网络不通）")
    # 127.0.0.1:1 是保留端口，必然连不上
    code2, out2, run_dir2 = _run_cli({
        "LLM_BASE_URL": "http://127.0.0.1:1/v1",
        "LLM_API_KEY": "sk-wrong",
        "LLM_MODEL": "deepseek-chat",
    }, args.duration, "dead")
    # 创意层**没有兜底**（决策层有）：端点不通时整片直接失败。
    # 这是有意的取舍——用户明确勾了「真实模型」，就绝不能偷偷用离线文案糊过去，
    # 否则又会回到"接了 key 却看不出用没用上"那个坑。这里把这个行为钉住。
    check("端点不通时**明确失败**（不偷偷降级出片）", code2 != 0, f"退出码 {code2}（预期非 0）")
    check("失败信息点明了模型名与连接原因",
          "语言大模型调用失败" in out2 and "deepseek-chat" in out2, out2[-300:])
    check("失败信息里带上可排查的原始原因（不是一句「失败」了事）",
          "10061" in out2 or "拒绝" in out2 or "urlopen" in out2, out2[-300:])
    text2 = _read_run(run_dir2) if run_dir2.exists() else ""
    check("失败时没有产出带标记的文案（确实没走通，不是假装成功）",
          MARKER not in text2, "")

    print("\n" + "=" * 74)
    print(f"结果：{len(PASS)} 项通过，{len(FAIL)} 项失败")
    for f in FAIL:
        print(f"  FAIL: {f}")
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
