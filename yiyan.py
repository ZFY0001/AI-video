#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一言片场 · 命令行入口。

    python yiyan.py "给我的家乡方城烩面做一条 15 秒宣传片"     # 离线跑通全流程
    python yiyan.py "..." --live                              # 接入真实模型（需 .env 配好 key）
    python yiyan.py --check-env                               # 只做环境体检
    python yiyan.py --audit-prompts                           # 检查提示词库与文档是否同步

设计原则：**默认离线可跑**。没配任何 key 也能从一句话跑到成片 MP4，
配置了 key 就自动换成真实模型，两条路径共用同一套管线代码。
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from pipeline.config import Settings, load_env_file, new_run_context, resource_dir  # noqa: E402
from pipeline.decide import JevClient, RuleDecider  # noqa: E402
from pipeline.llm import CachedClient, CreativeLayer, OpenAIChatClient  # noqa: E402
from pipeline.models import MusicInput  # noqa: E402
from pipeline.pipeline import Pipeline  # noqa: E402
from pipeline.prompts import PromptLibrary, PromptError  # noqa: E402
from pipeline.runlog import RunLog, audio_label  # noqa: E402
from pipeline.images import provider_status  # noqa: E402
from pipeline.tts import build_tts_chain  # noqa: E402
from pipeline.video import ffmpeg_available  # noqa: E402


def build_settings() -> Settings:
    return Settings(values=load_env_file())


def check_env(settings: Settings) -> int:
    print("=== 一言片场 · 环境体检 ===")
    ok_all = True

    print("\n[1] 提示词库")
    try:
        lib = PromptLibrary.load()
        audit = lib.audit()
        print(f"    产物：{lib.path.relative_to(ROOT)}")
        print(f"    章节 {audit['sections']} 个 / 槽位 {audit['slots']} 个")
        print(f"    源文档：{audit['source']}（SHA256 {str(audit['source_sha256'])[:16]}…）")
        if audit["problems"]:
            ok_all = False
            for p in audit["problems"]:
                print(f"    [问题] {p}")
        else:
            print("    状态：✅ 结构完整，决策请求体均为合法 JSON")
    except PromptError as exc:
        ok_all = False
        print(f"    [问题] {exc}")

    print("\n[2] 执行层")
    ff_ok, ff_detail = ffmpeg_available()
    print(f"    FFmpeg：{'✅' if ff_ok else '❌'} {ff_detail}")
    ok_all &= ff_ok
    for item in provider_status(settings, resource_dir("workflows")):
        mark = "✅" if item["available"] == "是" else "○"
        print(f"    出图 {item['provider']:<9} {mark} {item['detail']}")
        if item["provider"] == "comfy" and item["available"] == "否":
            print("        （ComfyUI 没在跑 → 自动使用离线占位帧，管线照样跑通）")
    for prov in build_tts_chain(settings):
        ok, why = prov.available()
        print(f"    配音 {prov.name:<10} {'✅' if ok else '○'} {why}")

    print("\n[3] 模型与密钥")
    for key in ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY", "JEV_BASE_URL", "JEV_API_KEY", "VLM_MODEL"):
        val = settings.masked(key) if key.endswith("KEY") else settings.get(key)
        print(f"    {key:<16} = {val or '(未配置)'}")
    live_ready = settings.has("LLM_API_KEY")
    print(f"    真实模式可用：{'✅ 是（--live）' if live_ready else '○ 否（缺 LLM_API_KEY，将走离线创意引擎）'}")
    print(f"    Jev 决策：{'✅ 走官方 API' if settings.has('JEV_API_KEY') else '○ 未配置 → 走本地规则引擎（阈值与升级规则一致）'}")

    print("\n[4] 结论")
    if ok_all:
        print("    环境就绪：可以离线跑通全流程。")
    else:
        print("    存在问题项，见上方 [问题] 标记。")
    return 0 if ok_all else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="yiyan",
        description="一言片场 · 文案进、成片出的 AI 视频管线",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：python yiyan.py \"给我的家乡方城烩面做一条 15 秒宣传片\"",
    )
    parser.add_argument("input", nargs="?", help="一句话主题 / 文案 / 脚本")
    parser.add_argument("--live", action="store_true", help="调用真实模型（需 .env 配好 key）")
    parser.add_argument("--duration", type=int, help="总时长（秒），覆盖简报默认值")
    parser.add_argument("--aspect", choices=["16:9", "9:16", "1:1"], help="画幅")
    parser.add_argument("--style", help="风格基调，如「烟火气、暖色调、纪实风」")
    parser.add_argument("--shot-len", type=float, default=5.0, help="平均镜长（秒），默认 5")
    parser.add_argument("--max-shots", type=int, help="限制镜头数（调试用）")
    parser.add_argument("--guides", action="store_true", help="在示意帧上叠加三分线与安全区")
    parser.add_argument("--check-env", action="store_true", help="只做环境体检")
    parser.add_argument("--audit-prompts", action="store_true", help="检查提示词库与文档是否同步")
    parser.add_argument("--no-cache", action="store_true", help="不使用大模型响应缓存")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印每一步细节")
    parser.add_argument("--json", action="store_true", help="把运行结果以 JSON 打到 stdout")
    # ---- MV 模式：给了音频就以音频为总时长权威，并把它当作成片音轨 ----
    parser.add_argument("--audio", help="音乐文件（mp3/wav/m4a…）。给了就是 MV 模式："
                                       "总时长以音频为准，不生成 TTS 配音")
    parser.add_argument("--lyrics", help="歌词原文（可带 LRC 时间戳 [00:12.34]）")
    parser.add_argument("--lyrics-file", help="歌词文件路径（.lrc/.txt），与 --lyrics 二选一")
    parser.add_argument("--music-role", choices=["song", "bgm"], default="song",
                        help="音乐角色：song=音乐即音轨（MV，默认，不生成配音）；"
                             "bgm=背景配乐（保留配音解说，音乐压低垫底）")
    parser.add_argument("--bpm", type=float, default=0.0,
                        help="手工指定 BPM（默认自动从音频估计；估计不准时用它覆盖）")
    parser.add_argument("--beat-offset", type=float, default=0.0,
                        help="手工指定首拍时刻（秒），配合 --bpm 使用")
    parser.add_argument("--no-beats", action="store_true",
                        help="不做节拍估计、不按拍切镜（画面切点仍按时长权重分配）")
    parser.add_argument("--painted", action="store_true",
                        help="代码绘制：用 p5.js 水彩引擎逐帧画出整段成片"
                             "（不用任何出图模型；较慢，需 Node + Edge/Chrome）")
    parser.add_argument("--painted-fps", type=int, default=24,
                        help="代码绘制的帧率，默认 24。水彩逐帧约 1 秒/帧，"
                             "长片可降到 12 换时间（帧数与耗时线性相关）")
    parser.add_argument("--clean-frames", action="store_true",
                        help="编码成功后清掉帧缓存（3 分钟 1080p 约 2.4 GB）。"
                             "会失去断点续渲能力，但那时的成片已经产出")
    args = parser.parse_args(argv)

    settings = build_settings()

    if args.audit_prompts:
        import subprocess

        rc = subprocess.call([sys.executable, str(ROOT / "tools" / "extract_prompts.py"), "--check"])
        rc2 = subprocess.call([sys.executable, str(ROOT / "tools" / "verify_extraction.py")])
        return 0 if rc == 0 and rc2 == 0 else 1

    if args.check_env:
        return check_env(settings)

    if not args.input:
        parser.print_help()
        print("\n[提示] 至少要给一句话主题，或用 --check-env 先做环境体检。")
        return 2

    lib = PromptLibrary.load()
    audit = lib.audit()
    if audit["problems"]:
        print("[警告] 提示词库存在问题，先修文档再跑：")
        for p in audit["problems"]:
            print("   - " + p)

    ctx = new_run_context(args.input, settings, live=args.live, dry_run=False, verbose=args.verbose)
    ctx.prepare()
    log = RunLog(ctx.log_path, verbose=args.verbose)

    # 组装创意层：有 key 且 --live 才走真实模型
    client = None
    if args.live and settings.has("LLM_API_KEY"):
        inner = OpenAIChatClient(
            base_url=settings.get("LLM_BASE_URL"),
            api_key=settings.get("LLM_API_KEY"),
            model=settings.get("LLM_MODEL"),
        )
        client = inner if args.no_cache else CachedClient(inner, ROOT / "cache" / "llm")
    elif args.live:
        print("[提示] --live 已指定，但 .env 里没有 LLM_API_KEY，本次仍走离线创意引擎。")

    # 组装决策层：有 key 就用 Jev，否则本地规则
    if settings.has("JEV_API_KEY"):
        decider = JevClient(base_url=settings.get("JEV_BASE_URL"), api_key=settings.get("JEV_API_KEY"), lib=lib)
    else:
        decider = RuleDecider(lib)

    creative = CreativeLayer(lib, client)
    pipe = Pipeline(settings, lib, ctx, log, creative, decider, avg_shot_len=args.shot_len, guides=args.guides)

    # MV 模式：组装音乐输入。歌词允许来自参数或文件；文件读不到时明确报错，
    # 不静默忽略——否则用户以为歌词生效了，实际成片是解说词。
    music = None
    if args.audio:
        lyrics = args.lyrics or ""
        if args.lyrics_file:
            lf = Path(args.lyrics_file)
            if not lf.exists():
                print(f"[失败] 歌词文件不存在：{lf}")
                return 1
            lyrics = lf.read_text(encoding="utf-8", errors="replace")
        music = MusicInput(
            path=args.audio,
            lyrics=lyrics,
            role=args.music_role,
            bpm=args.bpm,
            offset=args.beat_offset,
            auto_beats=not args.no_beats,
        )

    mode = "真实模型" if client else "离线引擎"
    dec_mode = "Jev 官方 API" if settings.has("JEV_API_KEY") else "本地规则引擎"
    if music is None:
        tag = "解说短片"
    elif music.is_bgm:
        tag = "配乐模式（人声 + 背景音乐）"
    else:
        tag = "MV 模式（音乐驱动）"
    print(f"=== 一言片场 === {tag} ｜ 创意层：{mode} ｜ 决策层：{dec_mode} ｜ 运行号：{ctx.run_id}")
    try:
        result = pipe.run(
            args.input,
            total_duration=args.duration,
            aspect_ratio=args.aspect,
            style=args.style,
            max_shots=args.max_shots,
            music=music,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("run", str(exc), traceback=traceback.format_exc()[-1500:])
        print(f"\n[失败] {exc}")
        print(f"详细日志：{ctx.log_path}")
        if args.verbose:
            traceback.print_exc()
        return 1

    # 代码绘制（水彩逐帧）：与界面共用同一条通路。失败不改变退出码——
    # 它是锦上添花，不该把一条本来能出的片子判成失败（原成片会保留）。
    if args.painted:
        from pipeline import painted as pb

        _st = pb.check_env()
        if not _st.ok:
            print(f"\n[失败] 代码绘制环境不可用：{_st.detail}")
            return 1
        print(f"\n[代码绘制] 环境就绪（{_st.detail}）。逐帧渲染较慢，请耐心等待…")
        result = pb.apply_painted(
            result,
            log=lambda level, msg: print(f"    [{level}] {msg}"),
            progress=lambda stage, pct: print(f"    {stage} {pct * 100:.0f}%"),
            fps=max(4, int(args.painted_fps or 24)),
            clean_frames_after=bool(args.clean_frames),
        )
        for note in result.get("notes", []):
            if "代码绘制" in note:
                print(f"    · {note}")

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        comp = result["compose"]
        print(f"\n[完成] 成片：{comp['video']}")
        print(f"        时长 {comp['duration']:.1f}s ｜ {comp['resolution']} ｜ {comp['size_mb']} MB ｜ "
              f"音轨：{audio_label(comp)}")
        print(f"        字幕：{comp['subtitle']}")
        print(f"        分镜：{comp['storyboard']}")
        print(f"        决策 {result['decisions']} 次 ｜ 小结：{result['summary']}")
        if result["notes"]:
            print("\n[需要人工关注]")
            for note in result["notes"]:
                print("  - " + note)
    return 0


if __name__ == "__main__":
    sys.exit(main())
