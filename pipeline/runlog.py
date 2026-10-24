"""一言片场 · 日志。

一次运行产出两份日志：
- run.jsonl  机器可读，每次关键事件一行 JSON（决策日志、生成日志、错误）
- summary.md 人类可读的跑测小结
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import caption_of

SECRET_HINTS = ("key", "token", "secret", "authorization")


def _redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: ("***" if any(h in str(k).lower() for h in SECRET_HINTS) else _redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


def audio_label(compose_result: dict[str, Any]) -> str:
    """把音轨来源翻译成人话。

    以前只有一句「配音：有/无」，于是 MV 模式（音轨其实是音乐）被报成「配音：有」——
    套用了解说短片的口径，属于"说的和做的不一致"。按来源分开说：
    music → 音乐；silent/无 → 静音轨；其余（sapi / cloud-tts…）→ 配音。
    """
    src = str((compose_result or {}).get("audio_source") or "silent")
    if not (compose_result or {}).get("has_audio") or src == "silent":
        return "无（静音轨）"
    name = Path(str(compose_result.get("audio") or "")).name
    if src == "music+voice":
        return f"配音 + 音乐（{name}）" if name else "配音 + 音乐"
    if src == "music":
        return f"音乐（{name}）" if name else "音乐"
    return f"配音（{src}）"


def mode_text(live: bool, compose_result: dict[str, Any] | None = None) -> str:
    """小结的模式行。

    MV / 配乐必须一眼看出来，否则会跟「纯一句话解说短片」混在一起分不清。
    注意要同时覆盖 ``music``（MV，只有音乐）与 ``music+voice``（配乐，人声+音乐）——
    只判前者会漏标配乐模式。
    """
    src = str((compose_result or {}).get("audio_source") or "")
    text = "真实调用（--live）" if live else "离线演示（本地规则 + 占位画面）"
    if src == "music":
        text += " · MV（音乐驱动）"
    elif src == "music+voice":
        text += " · 配乐（人声 + 音乐）"
    return text


class RunLog:
    def __init__(self, path: Path, verbose: bool = False):
        self.path = path
        self.verbose = verbose
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.events: list[dict[str, Any]] = []
        self.counters: dict[str, int] = {}
        self.path.write_text("", encoding="utf-8")

    # ------------------------------------------------------------ 记录
    def event(self, kind: str, message: str, **fields: Any) -> dict[str, Any]:
        rec = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "kind": kind,
            "message": message,
            **{k: _redact(v) for k, v in fields.items()},
        }
        self.events.append(rec)
        self.counters[kind] = self.counters.get(kind, 0) + 1
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if self.verbose:
            extra = " ".join(
                f"{k}={_short(v)}" for k, v in fields.items() if k not in ("state", "request")
            )
            print(f"  · [{kind}] {message} {extra}".rstrip())
        return rec

    def decision(self, point: str, request: dict, response: dict, source: str, ms: float) -> dict:
        return self.event(
            "decision",
            f"{point} → {_decision_answer(response)}",
            point=point,
            source=source,
            latency_ms=round(ms, 1),
            request=request,
            response=response,
        )

    def error(self, where: str, error: str, **fields: Any) -> dict:
        return self.event("error", f"{where}: {error}", where=where, error=error, **fields)

    # ------------------------------------------------------------ 小结
    def write_summary(self, ctx, brief, script, board, compose_result) -> Path:
        counts = self.counters
        mode = mode_text(bool(ctx.live), compose_result)
        lines = [
            f"# 跑测小结 · {ctx.run_id}",
            "",
            f"- 模式：{mode}",
            f"- 主题：{brief.theme}",
            f"- 片名：{script.title}",
            f"- 镜头数：{len(board.shots)}，总时长：{board.total_duration:.1f}s，画幅：{brief.aspect_ratio}",
            f"- 决策次数：{counts.get('decision', 0)}（来源：{_sources(self.events)}）",
            f"- 生成尝试：{counts.get('generate', 0)}，质检：{counts.get('review', 0)}，重试：{counts.get('retry', 0)}",
            f"- 错误：{counts.get('error', 0)}",
            "",
            "## 成片",
            "",
        ]
        if compose_result:
            lines += [
                f"- 文件：`{compose_result.get('video')}`",
                f"- 时长：{compose_result.get('duration', 0):.1f}s，分辨率：{compose_result.get('resolution')}",
                f"- 音轨：{audio_label(compose_result)}",
                f"- 字幕：`{compose_result.get('subtitle')}`",
                "",
            ]
        lines += ["## 分镜台本", "", "| # | 段 | 时长 | 解说词 | 画面 | 路由 | 质量 | 状态 |", "|---|---|---|---|---|---|---|---|"]
        for s in board.shots:
            lines.append(
                f"| {s.id} | {s.act} | {s.duration:.1f}s | {caption_of(s)} | {s.visual[:28]} | {s.route or '-'} | {s.quality or '-'} | {s.status} |"
            )
        lines += ["", "## 决策明细", ""]
        for ev in self.events:
            if ev["kind"] == "decision":
                lines.append(
                    f"- `{ev['point']}` → **{_decision_answer(ev['response'])}**"
                    f"（{ev['source']}，{ev.get('latency_ms', 0)}ms）"
                )
        path = ctx.project_dir / "summary.md"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path


def _short(value: Any, limit: int = 60) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _decision_answer(response: dict) -> str:
    for key in ("decision", "route", "action", "best", "verdict", "quality", "complete", "status", "choice"):
        if key in response:
            return f"{key}={response[key]}"
    return "ok"


def _sources(events: list[dict]) -> str:
    tally: dict[str, int] = {}
    for ev in events:
        if ev["kind"] == "decision":
            src = ev.get("source", "?")
            tally[src] = tally.get(src, 0) + 1
    return "，".join(f"{k}×{v}" for k, v in sorted(tally.items())) or "无"
