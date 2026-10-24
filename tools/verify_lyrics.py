# -*- coding: utf-8 -*-
"""歌词端到端验证：解析 → 对齐 → 分配 → 三条渲染路径都真的上屏。

为什么要分三条路径验：项目里有**三个**把文字画到成片上的地方——
占位帧（Pillow）、代码绘制（p5.js 场景 JS）、外挂 SRT。它们各自读的是不同的字段。
如果只改了其中一个，就会出现"字幕文件是歌词、画面里是解说词"这种自相矛盾的产物，
而这种问题在成片里极难发现（要看两个地方才能对上）。

用法：
    python tools/verify_lyrics.py            # 快速：解析/对齐/分配/三条路径（不跑管线）
    python tools/verify_lyrics.py --run      # 额外真跑一条 20 秒 MV（约 1 分钟）
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline import painted as pb  # noqa: E402
from pipeline.genre import pick_genre  # noqa: E402
from pipeline.image_render import render_still  # noqa: E402
from pipeline.lyrics import (  # noqa: E402
    align_plain,
    assign_captions,
    lyric_srt,
    parse_lrc,
    parse_lyrics,
    plain_lines,
)
from pipeline.models import ProjectBrief, Shot, caption_of  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []

LRC = (
    "[ti:夜里的城市]\n[ar:测试]\n[offset:+250]\n"
    "[00:01.00]第一句歌词\n"
    "[00:04.50][00:09.00]重复的副歌\n"
    "[00:14.00]最后一句歌词\n"
)


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def build_shots(total: float, n: int = 4) -> list[Shot]:
    each = total / n
    shots = []
    for i in range(n):
        shots.append(Shot(id=i + 1, act="起", duration=each, narration=f"这是第 {i+1} 镜的解说词",
                          visual="一片开阔的水彩天空与远山", camera="缓慢推镜，中景"))
    t = 0.0
    for s in shots:
        s.start = round(t, 3)
        t += s.duration
    return shots


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true", help="额外真跑一条 MV 管线")
    args = ap.parse_args()

    print("=" * 70)
    print("歌词端到端验证（解析 / 对齐 / 上屏三条路径）")
    print("=" * 70)

    # ---------- 1. LRC 解析 ----------
    print("\n[1] LRC 解析")
    entries, offset = parse_lrc(LRC)
    check("解析出 offset 标签", abs(offset - 0.25) < 1e-9, str(offset))
    check("同行多时间戳展开成多条", len(entries) == 4, str(len(entries)))
    lines = parse_lyrics(LRC, 20.0)
    print(f"    解析结果：{[ (ln.start, ln.end, ln.text) for ln in lines ]}")
    check("offset 让歌词整体提前 250ms", abs(lines[0].start - 0.75) < 1e-9, str(lines[0].start))
    check("行区间首尾相接", all(abs(lines[i].end - lines[i + 1].start) < 1e-9
                                for i in range(len(lines) - 1)), "")
    check("末行补到整首末尾", abs(lines[-1].end - 20.0) < 1e-9, str(lines[-1].end))
    check("元信息与制作名单不进歌词",
          plain_lines(LRC + "作词：某某\n") == ["第一句歌词", "重复的副歌", "最后一句歌词"],
          str(plain_lines(LRC + "作词：某某\n")))

    # ---------- 2. 纯文本对齐 ----------
    print("\n[2] 纯文本对齐（无时间戳时的兜底）")
    plain = align_plain(["短", "这是一句明显更长的歌词内容"], 10.0)
    check("长句分到更多时间（按字数加权，不均分）", plain[1].span > plain[0].span,
          f"{plain[0].span:.2f} vs {plain[1].span:.2f}")
    check("铺满整首", abs(sum(ln.span for ln in plain) - 10.0) < 0.01,
          str(sum(ln.span for ln in plain)))
    beats = [i * 0.5 for i in range(21)]
    snapped = align_plain(["一二三四", "五六七八", "九十十一"], 10.0, beats)
    check("给了拍点就把歌词边界吸到拍上",
          all(abs(ln.start * 2 - round(ln.start * 2)) < 1e-6 for ln in snapped),
          str([ln.start for ln in snapped]))
    check("没有时长时不瞎猜", align_plain(["词"], 0.0) == [], "")

    # ---------- 3. 分配与上屏文字 ----------
    print("\n[3] 分配到镜头 + 上屏文字优先级")
    shots = build_shots(20.0, 4)
    filled = assign_captions(shots, lines)
    check("每个镜头都拿到歌词", filled == 4, str(filled))
    for s in shots:
        print(f"    镜{s.id} {s.start:5.2f}-{s.end:5.2f}  caption={s.caption!r}")
        print(f"                        narration={s.narration!r}")
    check("caption 进得了分镜契约（会序列化给渲染后端）",
          all(s.to_dict().get("caption") for s in shots), "")
    check("纯一句话模式不受影响（无 caption 时用解说词）",
          caption_of(Shot(id=1, act="起", duration=3, narration="解说", visual="", camera=""))
          == "解说", "")
    check("dict 形式也能取（代码绘制拿到的是 dict）",
          caption_of({"caption": "歌词", "narration": "解说"}) == "歌词", "")

    # ---------- 4. 三条渲染路径 ----------
    print("\n[4] 三条渲染路径都必须上歌词")
    tmp = Path(tempfile.mkdtemp(prefix="lyrics_"))
    try:
        # 4a. 外挂字幕
        srt = lyric_srt(lines, tmp / "s.srt")
        body = srt.read_text(encoding="utf-8-sig")
        check("SRT 按歌词自己的时间轴（不是按镜头切）",
              "00:00:00,750 --> 00:00:04,250" in body, body[:80])
        check("SRT 条数 = 歌词行数", body.count("-->") == len(lines), str(body.count("-->")))
        check("SRT 里没有解说词", "解说词" not in body, "")

        # 4b. 代码绘制（p5.js 场景 JS）
        js = pb.build_scene_js([s.to_dict() for s in shots], genre_label="城市宣传")
        hit = sum(1 for s in shots if s.caption and s.caption in js)
        narr_hit = sum(1 for s in shots if s.narration and s.narration[:6] in js)
        check("代码绘制场景含全部歌词", hit == len(shots), f"{hit}/{len(shots)}")
        check("代码绘制场景不含解说词", narr_hit == 0, f"命中 {narr_hit} 处")

        # 4c. 占位帧（Pillow 烧字）
        b = ProjectBrief(theme="夜里的城市", total_duration=20, aspect_ratio="16:9", style="冷色调")
        out = tmp / "cap.png"
        render_still(shots[0], b, pick_genre("夜里的城市"), out, total_shots=len(shots))
        check("占位帧渲染成功（字幕源取 caption）", out.exists() and out.stat().st_size > 10_000,
              f"{out.stat().st_size if out.exists() else 0} bytes")
        check("占位帧的字幕源是歌词", caption_of(shots[0]) == shots[0].caption,
              caption_of(shots[0]))

        # ---------- 5. 可选：真跑一条 MV ----------
        if args.run:
            print("\n[5] 真跑一条 MV 管线（音频 + LRC）")
            song = ROOT / "work" / "mv_test" / "beat_song.m4a"
            lrc = ROOT / "work" / "mv_test" / "song.lrc"
            if not song.exists() or not lrc.exists():
                check("测试素材存在（work/mv_test/beat_song.m4a + song.lrc）", False,
                      "先跑 Step 7/8 的准备步骤生成素材")
            else:
                proc = subprocess.run(
                    [sys.executable, "yiyan.py", "夜里的城市烟火", "--audio", str(song),
                     "--lyrics-file", str(lrc)],
                    cwd=str(ROOT), capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=900,
                )
                tail = (proc.stdout or "").strip().splitlines()[-6:]
                for ln in tail:
                    print("    " + ln)
                check("MV 管线跑通", proc.returncode == 0, (proc.stderr or "")[-300:])
                runs = sorted((ROOT / "runs").glob("*"), key=lambda p: p.stat().st_mtime)
                if runs:
                    out_srt = runs[-1] / "output" / "subtitles.srt"
                    text = out_srt.read_text(encoding="utf-8-sig") if out_srt.exists() else ""
                    check("成片字幕是歌词", "夜色落在窗台上" in text or "第一句歌词" in text,
                          text[:120])
                    check("成片字幕不是解说词", "先从一个细节说起" not in text, "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 70)
    print(f"结果：{len(PASS)} 项通过，{len(FAIL)} 项失败")
    for f in FAIL:
        print(f"  FAIL: {f}")
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
