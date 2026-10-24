#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一言片场 · 自检测试（不依赖 pytest，直接 python tests/test_pipeline.py）。

覆盖：
1. 输入清洗与题材识别（把口语指令洗成创作主题）
2. 提示词库加载与占位符清单
3. 决策层阈值：2.4 质检分档、2.5 N 选 1、2.6 重试升级规则
4. 数据契约：时长累加严格等于总时长
5. ComfyUI 工作流模板：合法性 + 管线改写（分辨率/提示词/种子注入）
6. 管线端到端（无外网、无 FFmpeg 情况下降级）冒烟
7. 主题引擎：深浅令牌一一对应、模式切换与跟随系统
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline.decide import RuleDecider, apply_escalation  # noqa: E402
from pipeline.genre import pick_genre, sanitize_input, theme_tokens  # noqa: E402
from pipeline.models import Shot, build_timeline  # noqa: E402
from pipeline.prompts import PromptLibrary  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASSED if condition else FAILED).append(name)
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail and not condition else ""))


def test_net_proxy_bypass() -> None:
    """本机地址必须绕过系统代理。

    这不是理论问题：这台机器上 ``ProxyEnable=1``、``ProxyServer=http://127.0.0.1:12450``、
    ``ProxyOverride`` 为空，于是 ``urlopen('http://127.0.0.1:8188/system_stats')``
    被代理接管、返回 **502**，而体检把它报成"ComfyUI 未响应（HTTPError）"——
    排查方向整个是错的。同样的坑会打中任何 ``LLM_BASE_URL=http://127.0.0.1:...``
    （Ollama / vLLM）。所以这里**真起一个本地服务**去取，代理还在就必然失败。
    """
    print("\n[14] 本机请求绕过系统代理")
    import urllib.error
    import urllib.request

    from pipeline import net

    for url, want in [
        ("http://127.0.0.1:8188/system_stats", True),
        ("http://127.9.9.9/x", True),
        ("http://localhost:11434/api/generate", True),
        ("http://[::1]:8080/", True),
        ("http://0.0.0.0:8000/", True),
        ("https://api.openai.com/v1/chat/completions", False),
        ("https://dashscope.aliyuncs.com/api/v1", False),
        ("", False),
    ]:
        check(f"本机判定：{url or '(空)'} → {want}", net.is_loopback(url) is want, "")

    req = urllib.request.Request("http://127.0.0.1:8188/prompt", method="POST")
    check("Request 对象也能取到主机名", net.host_of(req) == "127.0.0.1", net.host_of(req))
    check("不带代理的 opener 确实没有任何 ProxyHandler",
          not [h for h in net._NO_PROXY_OPENER.handlers if isinstance(h, urllib.request.ProxyHandler)],
          "")

    # 真起一个本地服务：如果 net.open_url 走了系统代理，这里会拿到 502
    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # noqa: D102
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{port}/system_stats"
        try:
            with net.open_url(url, timeout=8) as r:
                body = r.read().decode()
            check("本地服务能直连拿到 200（代理在就会被换成 502）",
                  r.status == 200 and body == '{"ok": true}', f"status={r.status} body={body}")
        except urllib.error.HTTPError as exc:
            check("本地服务能直连拿到 200（代理在就会被换成 502）", False,
                  f"HTTPError {exc.code} —— 请求被代理接管了")
        except Exception as exc:  # noqa: BLE001
            check("本地服务能直连拿到 200（代理在就会被换成 502）", False, repr(exc))
    finally:
        srv.shutdown()
        srv.server_close()

    # 错误翻译：把"代理劫持"和"服务没起"分开，否则排查方向会错
    px = net.system_proxy()
    hijack = urllib.error.HTTPError("http://127.0.0.1:8188/system_stats", 502, "Bad Gateway", {}, None)
    msg = net.describe_net_error("http://127.0.0.1:8188/system_stats", hijack)
    if px:
        check("代理劫持本机请求时，提示要指向代理而不是 ComfyUI",
              "代理" in msg and "NO_PROXY" in msg, msg)
    else:
        check("无代理时不会误报代理问题", "代理劫持" not in msg, msg)
    refused = urllib.error.URLError(ConnectionRefusedError(10061, "refused"))
    check("服务没在跑时说「连接被拒绝」而不是甩 HTTP 错误",
          "连接被拒绝" in net.describe_net_error("http://127.0.0.1:8188/x", refused), "")
    check("云端地址的 HTTP 错误不提本机代理",
          "本机请求被系统代理劫持" not in net.describe_net_error(
              "https://api.example.com/v1", hijack), "")
    check("超时有专门提示（ComfyUI 显存不足会卡住不返回）",
          "超时" in net.describe_net_error("http://127.0.0.1:8188/x",
                                          urllib.error.URLError(TimeoutError("timed out"))), "")


def test_launchers() -> None:
    """启动器必须双击就能用。

    这里守的是四个**真踩过的坑**：

    1. **工作目录**：双击 .bat 时 cmd 的当前目录是系统目录（System32），
       不是脚本所在目录。不写 `cd /d "%~dp0"` 的话，后面所有相对路径全落空，
       表现为「双击一闪就没了」。
    2. **编码**：cmd.exe 按 OEM 代码页（中文系统是 GBK）读 .bat，
       文件里混进 UTF-8 中文注释会被解成乱命令，脚本在中途炸掉。所以必须纯 ASCII。
    3. **报错要停住**：所有失败分支都得 `pause`，否则窗口一闪而过，
       用户只看到「没反应」，根本不知道缺的是 .venv 还是 web\\dist。
    4. **前端产物要先构建**：客户端装的是 `web/dist`，没有它 pywebview 会开一个白窗口。
       所以启动前要检查 `web\\dist\\index.html` 并给出构建命令。

    V3 的 Tkinter 启动器（`一言片场.vbs` / `启动桌面版.bat`）已随旧界面一起删除，
    这里改为守 V4 的两个入口。
    """
    print("\n[15] 启动器（双击即可用）")

    client = ROOT / "启动客户端.bat"
    web = ROOT / "启动Web版.bat"
    check("两个启动器、桌面入口、前端工程都在",
          client.exists() and web.exists()
          and (ROOT / "desktop.py").exists()
          and (ROOT / "web" / "package.json").exists(), "")

    for name, bat in (("客户端", client), ("Web 版", web)):
        raw = bat.read_bytes()
        check(f"{name} bat 是纯 ASCII（cmd 按 GBK 读，中文会变成乱命令）",
              all(b < 128 for b in raw), "存在非 ASCII 字节")
        txt = raw.decode("ascii", "replace")
        check(f"{name} bat 先切到脚本所在目录（双击时 cwd 是 System32）",
              'cd /d "%~dp0"' in txt, "")
        check(f"{name} bat 用项目内 .venv 的解释器",
              ".venv\\Scripts" in txt, "")
        check(f"{name} bat 的失败分支会 pause（否则一闪就没）",
              "pause" in txt, "")

    ctxt = client.read_text(encoding="ascii", errors="replace")
    check("客户端 bat 启动前检查 web\\dist\\index.html",
          "web\\dist\\index.html" in ctxt, "")
    check("客户端 bat 缺产物时给出构建命令",
          "npm run build" in ctxt, "")
    check("客户端 bat 用 pythonw 起窗口（不留黑框）",
          "pythonw.exe" in ctxt and "desktop.py" in ctxt, "")

    wtxt = web.read_text(encoding="ascii", errors="replace")
    check("Web 版 bat 起的是 server 包（不是旧 GUI 入口）",
          "-m server" in wtxt, "")
    check("Web 版 bat 支持 rebuild 参数", "rebuild" in wtxt, "")
    check("Web 版 bat 会找 node（PATH 里没有就补常见安装目录）",
          "where node" in wtxt and "%ProgramFiles%\\nodejs" in wtxt, "")



def test_sanitize() -> None:
    print("\n[1] 输入清洗与题材识别")
    cases = [
        ("给我的家乡方城烩面做一条 15 秒宣传片", "家乡方城烩面", "food"),
        ("帮我生成一个 30 秒的视频，主题是城市夜景", "城市夜景", "city"),
        ("做条 60s 短片，讲一位老木匠的故事", "一位老木匠的故事", "person"),
        ("请给我来一个新品手机发布视频", "新品手机发布", "product"),
        ("来一条关于古镇非遗的短片 45 秒", "古镇非遗", "culture"),
    ]
    for raw, expect_clean, expect_genre in cases:
        cleaned = sanitize_input(raw)
        check(f"清洗：{raw[:14]}… → {cleaned}", cleaned == expect_clean, f"期望 {expect_clean!r}，得到 {cleaned!r}")
        genre = pick_genre(raw)
        check(f"题材：{raw[:14]}… → {genre.label}", genre.key == expect_genre, f"得到 {genre.key}")
    tokens = theme_tokens("方城烩面")
    check("主题槽位：方城烩面 → 地点=方城/主体=烩面",
          tokens["place"] == "方城" and tokens["subject"] == "烩面", json.dumps(tokens, ensure_ascii=False))
    tokens2 = theme_tokens("一位老木匠的故事")
    check("主题槽位：一位老木匠的故事 → 主体=老木匠（不吞进「故事」）",
          tokens2["subject"] == "老木匠", json.dumps(tokens2, ensure_ascii=False))
    check("清洗不产生空串", sanitize_input("做一条视频") != "")


def test_narration_quality() -> None:
    """解说词质量回归（病句检测）。

    守的是实际踩过的坑：模板把"的"硬插进主体词，生成"一位老木的匠的故事"这类病句。
    句式一旦改动必须重跑本用例。
    """
    print("\n[1b] 解说词质量（病句回归）")
    from pipeline.common import split_sentences
    from pipeline.llm import CreativeLayer

    lib = PromptLibrary.load()
    creative = CreativeLayer(lib, None)
    bad_patterns = ("的的", "的匠的", "老木的", "的面的", "，，", "。。", "；；", "老木老木", "老木人")
    for raw in (
        "给我的家乡方城烩面做一条 15 秒宣传片",
        "讲一位老木匠的故事",
        "帮我生成一个 30 秒的视频，主题是城市夜景",
        "新品手机发布宣传片",
        "古镇非遗短片",
    ):
        brief = creative.make_brief(raw, {"total_duration": 30})
        script = creative.make_script(brief)
        text = script.narration_full
        hit = [p for p in bad_patterns if p in text]
        check(f"解说词无病句：{raw[:12]}…", not hit, f"命中 {hit}｜全文：{text[:70]}")
        sents = split_sentences(text)
        check(f"解说词句句收尾：{raw[:12]}…",
              bool(sents) and all(s.endswith(("。", "！", "？")) for s in sents), str(sents[:3]))
        check(f"片名干净：{raw[:12]}… → {script.title}",
              "秒" not in script.title and "的的" not in script.title, script.title)


def test_prompts() -> None:
    print("\n[2] 提示词库")
    lib = PromptLibrary.load()
    audit = lib.audit()
    check("章节与槽位齐全", not audit["problems"], "; ".join(audit["problems"]))
    check("决策请求体全部是合法 JSON", all(
        _is_json(lib.request_example(n)) for n in lib.sections if lib.sections[n].get("layer") == "decision"
    ))
    sys_p = lib.system("1.2")
    check("剧本系统提示词含时长占位符", "{{total_duration}}" in sys_p)
    rendered = lib.slot("1.2", "user").render({"project_brief_json": {"theme": "测试"}})
    check("用户模板渲染成功", "测试" in rendered and "{{" not in rendered, rendered[:60])
    missing = False
    try:
        lib.slot("1.3", "user").render({})
    except Exception:  # noqa: BLE001
        missing = True
    check("缺变量时严格模式报错", missing)
    check("负面提示词非空", len(lib.negative()) > 50)


def _is_json(text: str) -> bool:
    try:
        json.loads(text)
        return True
    except ValueError:
        return False


def test_decisions() -> None:
    print("\n[3] 决策层规则")
    d = RuleDecider()

    r = d.model_route(
        "5s close-up noodle bowl",
        [
            {"id": "comfy-sdxl-base", "description": "Local SDXL, ~3s per image, near-zero cost, medium quality"},
            {"id": "comfy-flux-gguf", "description": "Local FLUX GGUF, ~8s per image, best local quality"},
            {"id": "cloud-x", "description": "Cloud API, billed per image, high quality"},
        ],
        ["quality", "cost", "latency"],
        ["8GB VRAM local only"],
        "medium",
    )
    check("模型路由：本地约束下不选云端", not str(r["decision"]).startswith("cloud"), str(r["decision"]))
    check("模型路由：给出 confidence", "confidence" in r)

    check("任务分级：关键镜头 → deep_review", d.route_task("t", {"is_key_shot": True})["route"] == "deep_review")
    check("任务分级：普通镜头 → proceed_fast", d.route_task("t", {})["route"] == "proceed_fast")

    clean = json.dumps({"defects": [], "style_match": "strong", "composition": "close-up",
                        "lighting": "warm tone, sharp"}, ensure_ascii=False)
    bad = json.dumps({"defects": ["left rim deformed", "steam weak"], "style_match": "partial",
                      "lighting": "cold cast"}, ensure_ascii=False)
    good_review = d.review_quality({"acceptance": "主体清晰、无畸变、色调温暖", "candidate_desc": clean, "attempt": 1})
    bad_review = d.review_quality({"acceptance": "主体清晰、无畸变、色调温暖", "candidate_desc": bad, "attempt": 1})
    check("质检：合格画面 → accept", good_review["verdict"] == "accept", json.dumps(good_review, ensure_ascii=False))
    check("质检：带缺陷画面 → 不 accept", bad_review["verdict"] != "accept", json.dumps(bad_review, ensure_ascii=False))
    check("质检：分档落在 1-4", 1 <= good_review["quality"] <= 4 and 1 <= bad_review["quality"] <= 4)

    pick = d.pick_best({
        "acceptance": "主体清晰、无畸变、色调温暖",
        "candidates": [
            {"index": "1", "desc": json.dumps({"defects": ["rim deformed"], "lighting": "cold cast"})},
            {"index": "2", "desc": json.dumps({"defects": [], "lighting": "warm tone, sharp, clear"})},
        ],
    })
    check("N 选 1：选出无缺陷候选", pick["best"] == "2", json.dumps(pick, ensure_ascii=False))

    # 2.6 升级规则
    s = {"defect_type": "anatomy", "attempt": 1, "max_attempts": 3, "attempt_history": []}
    plain = d.retry_strategy(s)
    check("重试：首次失败 = retry_same", plain["action"] == "retry_same", json.dumps(plain, ensure_ascii=False))
    s2 = dict(s, attempt_history=[{"defect_type": "anatomy"}])
    esc = apply_escalation({"action": "retry_same"}, s2)
    check("重试：同缺陷 ×2 → 强制 rewrite_prompt",
          esc["action"] == "rewrite_prompt" and esc.get("escalated"), json.dumps(esc, ensure_ascii=False))
    s3 = dict(s, attempt_history=[{"defect_type": "anatomy"}, {"defect_type": "anatomy"}])
    esc3 = apply_escalation({"action": "retry_same"}, s3)
    check("重试：同缺陷 ×3 → 强制 skip", esc3["action"] == "skip", json.dumps(esc3, ensure_ascii=False))
    s4 = {"defect_type": "anatomy", "attempt": 3, "max_attempts": 3, "attempt_history": []}
    esc4 = apply_escalation({"action": "retry_same"}, s4)
    check("重试：attempt ≥ max → 强制 switch_model", esc4["action"] == "switch_model", json.dumps(esc4, ensure_ascii=False))
    esc5 = apply_escalation({"action": "switch_model"}, s3)
    check("重试：保险层只升不降（skip 不会被降级）", esc5["action"] == "skip")

    allow = d.tool_guard("call_paid_video_api", "gen", [], [], ["Frame passed Jev quality review at 4/4", "Budget check passed"], [], "not_reversible")
    deny = d.tool_guard("call_paid_video_api", "gen", [], [], ["Budget exceeded"], [], "not_reversible")
    check("护栏：证据齐全 → allow", allow["decision"] == "allow")
    check("护栏：预算缺失 → deny", deny["decision"] == "deny")
    confirm = d.tool_guard("call_paid_video_api", "gen", [], [], ["Frame passed Jev quality review at 4/4", "Budget check passed", "stakes=high"], [], "not_reversible")
    check("护栏：高风险 → confirm", confirm["decision"] == "confirm")


def test_timeline() -> None:
    print("\n[4] 数据契约")
    shots = [Shot(id=i + 1, act="起", duration=d, narration="", visual="", camera="") for i, d in enumerate([5, 5, 5])]
    build_timeline(shots, 15)
    check("时间轴累加 = 总时长", abs(sum(s.duration for s in shots) - 15) < 0.01,
          str([s.duration for s in shots]))
    check("首镜起点为 0", shots[0].start == 0)
    check("末镜终点 = 总时长", abs(shots[-1].end - 15) < 0.01, str(shots[-1].end))
    odd = [Shot(id=i + 1, act="起", duration=d, narration="", visual="", camera="") for i, d in enumerate([3, 4, 5, 3])]
    build_timeline(odd, 15)
    check("非均分时长也能对齐", abs(sum(s.duration for s in odd) - 15) < 0.01, str([s.duration for s in odd]))

    # 末镜负时长回归：原来末镜直接吃残差 total - t，镜头一多就被算成负数
    # （实测 10 镜 / 15 秒 → -3.00 秒）。负时长会流进配音变速、字幕时间码与出片命令。
    many = [Shot(id=i + 1, act="起", duration=5, narration="", visual="", camera="") for i in range(10)]
    build_timeline(many, 15)
    check("10 镜 / 15 秒不产生负数时长",
          all(s.duration > 0 for s in many), str([s.duration for s in many]))
    check("10 镜 / 15 秒仍严格等于总时长",
          abs(sum(s.duration for s in many) - 15) < 0.01, str(round(sum(s.duration for s in many), 3)))
    check("10 镜 / 15 秒起点连续无跳变",
          all(abs(many[i].start - sum(x.duration for x in many[:i])) < 0.01 for i in range(10)), "")

    dense = [Shot(id=i + 1, act="起", duration=5, narration="", visual="", camera="") for i in range(12)]
    build_timeline(dense, 15)
    check("12 镜 / 15 秒不产生负数时长",
          all(s.duration > 0 for s in dense), str([s.duration for s in dense]))
    check("12 镜 / 15 秒仍等于总时长",
          abs(sum(s.duration for s in dense) - 15) < 0.01, str(round(sum(s.duration for s in dense), 3)))

    # 单镜与充裕时长：不能因为摊派逻辑把原有分配改坏
    one = [Shot(id=1, act="起", duration=5, narration="", visual="", camera="")]
    build_timeline(one, 7)
    check("单镜时全长给该镜", abs(one[0].duration - 7) < 0.01, str(one[0].duration))
    roomy = [Shot(id=i + 1, act="起", duration=5, narration="", visual="", camera="") for i in range(3)]
    build_timeline(roomy, 30)
    check("时长充裕时按权重等比分配（回归）",
          [s.duration for s in roomy] == [10.0, 10.0, 10.0], str([s.duration for s in roomy]))


def test_music_input() -> None:
    """MV 模式：音乐输入契约 + 「总时长以音频为准」这条立身之本。"""
    print("\n[4c] MV 模式 · 音乐输入与时长权威")
    from pipeline.models import MusicInput
    from pipeline.pipeline import Pipeline
    from pipeline.tts import silence
    from pipeline.video import probe_duration

    check("空音乐输入不启用 MV 模式", not MusicInput().enabled)
    check("只给空白路径也不算 MV 模式", not MusicInput(path="   ").enabled)
    check("给了路径即启用 MV 模式", MusicInput(path="a.mp3").enabled)
    check("默认上屏文字来源是歌词", MusicInput(path="a.mp3").caption_source == "lyrics")

    lyr = "第一句歌词\n\n第二句歌词\n   \n第三句歌词\n"
    mi = MusicInput(path="a.mp3", lyrics=lyr)
    check("歌词行数统计忽略空行", mi.to_dict()["lyrics_lines"] == 3,
          str(mi.to_dict()["lyrics_lines"]))
    check("音乐摘要可序列化（含 duration）", "duration" in mi.to_dict(), str(mi.to_dict()))
    check("音乐摘要不泄漏整段歌词原文",
          "第一句歌词" not in json.dumps(mi.to_dict(), ensure_ascii=False), "")

    # 时长权威：真的探测一个音频文件，再用它重排时间轴，断言
    # 「画面总时长 == 音频时长」。这是 MV 模式与解说模式的根本区别——
    # 画面服从音乐，而不是让音乐迁就画面。
    with tempfile.TemporaryDirectory() as _md:
        song = Path(_md) / "song.m4a"
        silence(song, 12.7)
        probed = probe_duration(song)
        check("能探测到音频时长", probed > 0, str(probed))
        shots = [Shot(id=i + 1, act="起", duration=5, narration="", visual="", camera="")
                 for i in range(4)]
        build_timeline(shots, probed)
        total = sum(s.duration for s in shots)
        check("时间轴严格对齐音频时长（含小数）",
              abs(total - probed) < 0.01, f"音频 {probed:.3f}s / 画面 {total:.3f}s")
        check("短音乐不会被退回默认时长",
              abs(total - 15) > 1, f"画面 {total:.3f}s")

    # 音乐文件不存在时必须明确失败，不能静默退回解说模式
    # （用户以为在做 MV，实际拿到一条带解说的片子，是最难查的那种错）
    ghost = Pipeline.__new__(Pipeline)
    raised = ""
    try:
        ghost.run("主题", music=MusicInput(path=str(ROOT / "tests" / "_no_such_song.mp3")))
    except RuntimeError as exc:
        raised = str(exc)
    check("音乐文件不存在时明确报错", "不存在" in raised, raised or "没有抛异常")

    # 汇报口径：MV 模式的音轨是音乐，不能报成「配音：有」
    from pipeline.runlog import audio_label
    mv_comp = {"has_audio": True, "audio_source": "music", "audio": "a/song.mp3"}
    check("音轨标注：音乐不自称配音", audio_label(mv_comp) == "音乐（song.mp3）",
          audio_label(mv_comp))
    check("音轨标注：无音频时如实说是静音轨",
          "静音" in audio_label({"has_audio": False, "audio_source": "silent"}), "")
    check("音轨标注：真实配音如实标出来源",
          "配音" in audio_label({"has_audio": True, "audio_source": "sapi"}), "")
    check("音轨标注：空结果不抛异常", bool(audio_label({})), "")


def test_music_mix() -> None:
    """音乐混音：人声 + 音乐必须真的能共存。

    此前全仓 `amix` / `loudnorm` / `volume=` **零命中**，也就是说"既能配乐又能配音"
    这句话从来没有实现过——音乐与人声在结构上不可能同框。这里守住新接上的链路。
    """
    print("\n[4d] 音乐混音（amix / loudnorm / 淡入淡出）")
    from pipeline.compose import BGM_GAIN, LOUDNORM, MUSIC_FADE_SECONDS, build_mix_args
    from pipeline.models import MusicInput
    from pipeline.runlog import audio_label

    voice, music = Path("vo.m4a"), Path("song.m4a")
    args2 = build_mix_args([(voice, 1.0, False), (music, BGM_GAIN, True)],
                           Path("mix.m4a"),
                           duration=18.5, fade=MUSIC_FADE_SECONDS, loudnorm=True)
    fc = args2[args2.index("-filter_complex") + 1]
    check("两轨进 amix", "amix=inputs=2" in fc, fc[:120])
    check("amix 关闭自动衰减（normalize=0，否则人声白掉 6dB）", "normalize=0" in fc, "")
    check("音乐被压到背景垫底音量", f"volume={BGM_GAIN:.4g}" in fc, fc[:170])
    check("人声保持原音量", "volume=1" in fc, "")
    check("每轨统一为 44100 立体声（避免声道不一致而报错）",
          fc.count("channel_layouts=stereo") == 2, str(fc.count("channel_layouts=stereo")))
    check("响度归一已接上", LOUDNORM in fc, "")
    check("淡入淡出已接上", "afade=t=in" in fc and "afade=t=out" in fc, "")
    check("音轨补齐到整片长度（音轨短于画面会截断画面）",
          "apad=whole_dur=18.500" in fc, fc[-90:])
    check("音乐轨循环播放（配乐比解说短是常态）",
          args2.count("-stream_loop") == 1, str(args2[:10]))
    check("输出被限定为画面时长", "-t" in args2 and "18.500" in args2, str(args2[-4:]))
    check("输出映射到混音 pad", "[out]" in args2, "")

    # 只有音乐一轨时不套 amix（单轨 amix 会额外引入归一化行为，白改音量）
    args1 = build_mix_args([(music, BGM_GAIN, True)], Path("mix.m4a"),
                           duration=10.0, fade=0.0, loudnorm=True)
    fc1 = args1[args1.index("-filter_complex") + 1]
    check("单轨不套 amix", "amix" not in fc1, fc1)
    check("单轨仍过响度归一", LOUDNORM in fc1, "")

    # 淡入淡出不能超过半片长，否则 in/out 会互相覆盖、末尾反而变响
    short = build_mix_args([(music, 1.0, False)], Path("m.m4a"),
                           duration=1.0, fade=0.8)
    fcs = short[short.index("-filter_complex") + 1]
    check("淡入淡出不超过半片长", "d=0.500" in fcs, fcs[-100:])

    try:
        build_mix_args([], Path("m.m4a"))
        check("空轨道列表应报错", False, "没有抛异常")
    except ValueError:
        check("空轨道列表应报错", True)

    check("音乐角色默认 song（MV）", MusicInput(path="a.mp3").role == "song")
    check("role=bgm 时 is_bgm 为真", MusicInput(path="a.mp3", role="bgm").is_bgm)
    check("role=song 时 is_bgm 为假", not MusicInput(path="a.mp3", role="song").is_bgm)
    check("没给音乐时 is_bgm 为假", not MusicInput(role="bgm").is_bgm)
    check("role 进得了摘要", MusicInput(path="a.mp3", role="bgm").to_dict()["role"] == "bgm")

    lbl = audio_label({"has_audio": True, "audio_source": "music+voice", "audio": "w/mix.m4a"})
    check("音轨标注：配音与音乐都说明", "配音" in lbl and "音乐" in lbl, lbl)

    # 小结模式行：只判 audio_source=="music" 会漏标配乐模式（music+voice）
    from pipeline.runlog import mode_text
    check("小结模式行标出 MV", "MV" in mode_text(False, {"audio_source": "music"}),
          mode_text(False, {"audio_source": "music"}))
    check("小结模式行标出配乐（人声+音乐）",
          "配乐" in mode_text(False, {"audio_source": "music+voice"}),
          mode_text(False, {"audio_source": "music+voice"}))
    check("纯解说短片不误标 MV/配乐",
          "MV" not in mode_text(False, {"audio_source": "sapi"})
          and "配乐" not in mode_text(False, {"audio_source": "sapi"}),
          mode_text(False, {"audio_source": "sapi"}))
    check("live 标记保留", "live" in mode_text(True, None), mode_text(True, None))


def _click_track(path: Path, bpm: float, offset: float, seconds: float, sr: int = 8000) -> Path:
    """合成一条**已知 BPM 与首拍**的点击轨，纯标准库、确定性。"""
    import math
    import struct
    import wave

    step = 60.0 / bpm
    n = int(seconds * sr)
    buf = [0.0] * n
    t = offset
    while t < seconds:
        i0 = int(t * sr)
        for k in range(int(0.03 * sr)):
            i = i0 + k
            if 0 <= i < n:
                buf[i] += math.sin(2 * math.pi * 1000 * k / sr) * math.exp(-k / (0.004 * sr))
        t += step
    data = b"".join(struct.pack("<h", int(max(-1.0, min(1.0, v)) * 32000)) for v in buf)
    with wave.open(str(path), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(sr)
        fh.writeframes(data)
    return path


def _sine_track(path: Path, seconds: float, hz: float = 330.0, sr: int = 8000) -> Path:
    """一条没有节拍的纯正弦（用来验"该拒绝时要拒绝"）。"""
    import math
    import struct
    import wave

    n = int(seconds * sr)
    buf = [math.sin(2 * math.pi * hz * i / sr) * 0.6 for i in range(n)]
    data = b"".join(struct.pack("<h", int(v * 32000)) for v in buf)
    with wave.open(str(path), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(sr)
        fh.writeframes(data)
    return path


def test_beats() -> None:
    """节拍：BPM/首拍估计 + 按拍切镜。

    估计器是纯标准库实现（不引 librosa，见 pipeline/beats.py 的说明），
    所以用**合成点击轨**做端到端断言：真解码、真估计、真吸附。
    """
    print("\n[7] 节拍（BPM/首拍估计 + 切点吸附）")
    from pipeline.beats import EMPTY, estimate_beats, manual_grid, tempo_prior
    from pipeline.models import MIN_SHOT_SECONDS, build_timeline, snap_to_beats

    check("空网格 ok 为假", not EMPTY.ok)
    check("无网格时 nearest 原样返回", EMPTY.nearest(1.234) == 1.234)
    check("bpm<=0 的手工网格等于空网格", not manual_grid(0).ok)
    check("手工网格来源标为 manual", manual_grid(120).source == "manual")
    check("拍间隔 = 60/BPM", abs(manual_grid(120).interval - 0.5) < 1e-9, "")
    check("拍点数正确（0.25 起 / 0.5 间隔 / 到 2.0）",
          len(manual_grid(120, 0.25).beats_until(2.0)) == 4,
          str(manual_grid(120, 0.25).beats_until(2.0)))
    check("先验在 120BPM 处最大",
          tempo_prior(120) > tempo_prior(60) and tempo_prior(120) > tempo_prior(240), "")
    check("先验对 0 不炸", tempo_prior(0) == 0.0, "")

    # ---- 真解码 + 真估计：能不能估回已知真值 ----
    with tempfile.TemporaryDirectory() as td:
        for bpm, off in ((120.0, 0.25), (160.0, 0.10)):
            p = _click_track(Path(td) / f"c{int(bpm)}.wav", bpm, off, 12.0)
            est = estimate_beats(p)
            err = abs(est.bpm - bpm)
            # 相位允许差一个整拍（只差一个 offset 的整周期是等价的）
            phase = min(abs(est.offset - off),
                        abs(abs(est.offset - off) - est.interval))
            check(f"{bpm:.0f}BPM 估计准确（实测 {est.bpm:.2f}）", err < 2.0, f"误差 {err:.2f}")
            check(f"{bpm:.0f}BPM 首拍相位正确", phase < 0.04, f"相位差 {phase:.3f}s")
            check(f"{bpm:.0f}BPM 置信度够用", est.confidence > 0.25, str(est.confidence))

        # 反向用例：纯正弦没有节拍，必须拒绝而不是给一个"高置信度"的假网格。
        # 实测踩过：起音包络接近零向量时，自相关的置信度会荒谬地接近 1，
        # 一段纯正弦被报成 120BPM/conf=1.00——不拦就会拿假网格去切镜头。
        sine = estimate_beats(_sine_track(Path(td) / "sine.wav", 12.0))
        check("无节拍的纯音不产出节拍网格", not sine.ok, str(sine.to_dict()))

    # ---- 按拍切镜 ----
    beats = [i * 0.5 for i in range(24)]      # 120BPM：拍点 0 / 0.5 / 1.0 …
    snapped = snap_to_beats([2.45, 2.1, 2.45, 3.0], beats, floor=1.0)
    check("偏离拍点的切点被吸附到拍上", snapped == [2.5, 2.0, 2.5, 3.0], str(snapped))
    check("吸附后总时长不变", abs(sum(snapped) - 10.0) < 0.01, str(sum(snapped)))

    far = snap_to_beats([2.2, 2.2, 2.2, 2.2], [0.0, 5.0, 10.0], floor=1.0)
    check("离拍点太远时不硬吸（否则某一镜会被拉得离谱）",
          abs(far[0] - 2.2) < 1e-9, str(far))

    tight = snap_to_beats([1.05, 1.05, 1.05], [0.0, 1.0, 2.0, 3.0], floor=1.0)
    check("吸附不会把镜头压到下限以下",
          all(d >= 1.0 - 1e-9 for d in tight), str(tight))

    check("无拍点时原样返回", snap_to_beats([2.0, 3.0], [], 1.0) == [2.0, 3.0])
    check("单镜无需吸附", snap_to_beats([5.0], beats, 1.0) == [5.0])

    shots = [Shot(id=i + 1, act="起", duration=2.5, narration="", visual="", camera="")
             for i in range(4)]
    build_timeline(shots, 10.0, beats=[i * 0.5 for i in range(21)])
    check("按拍切镜：总时长仍严格等于目标", abs(shots[-1].end - 10.0) < 0.01, str(shots[-1].end))
    check("按拍切镜：每镜仍不低于下限",
          all(s.duration >= MIN_SHOT_SECONDS - 1e-9 for s in shots),
          str([s.duration for s in shots]))
    starts = [s.start for s in shots[1:]]
    check("按拍切镜：中间切点全落在拍点上",
          all(abs(s * 2 - round(s * 2)) < 1e-6 for s in starts), str(starts))
    # 不给拍点时行为必须与以前完全一致（纯一句话模式不受影响）
    plain = [Shot(id=i + 1, act="起", duration=5, narration="", visual="", camera="")
             for i in range(3)]
    build_timeline(plain, 15)
    check("不给拍点时不改变原有分配（回归）",
          [s.duration for s in plain] == [5.0, 5.0, 5.0], str([s.duration for s in plain]))


def test_lyrics() -> None:
    """歌词解析与上屏（Step 8）：LRC 优先，纯文本按字数加权。"""
    print("\n[8] 歌词解析与上屏")
    from pipeline.lyrics import (align_plain, assign_captions, lyric_srt,
                                 parse_lrc, parse_lyrics, plain_lines)

    lrc = (
        "[ti:测试曲目]\n[ar:某人]\n[offset:+250]\n"
        "[00:01.00]第一句歌词\n"
        "[00:04.50][00:09.00]重复的副歌\n"
        "[00:14.00]最后一句\n"
    )
    entries, offset = parse_lrc(lrc)
    check("LRC 解析出 offset 标签（+250ms）", abs(offset - 0.25) < 1e-9, str(offset))
    check("LRC 元信息标签被忽略", all("ti" not in b and "ar" not in b for _t, b in entries), "")
    check("同一行多个时间戳被展开成多条", len(entries) == 4, str(len(entries)))

    lines = parse_lyrics(lrc, 20.0)
    check("offset 语义：正值把歌词整体提前", abs(lines[0].start - 0.75) < 1e-9,
          str(lines[0].start))
    check("每行都拿到正的区间", all(ln.span > 0 for ln in lines), str([ln.to_dict() for ln in lines]))
    check("行区间首尾相接（前一行结束 = 后一行开始）",
          all(abs(lines[i].end - lines[i + 1].start) < 1e-9 for i in range(len(lines) - 1)), "")
    check("末行补到整首末尾", abs(lines[-1].end - 20.0) < 1e-9, str(lines[-1].end))
    check("重复的副歌出现两次", sum(1 for ln in lines if ln.text == "重复的副歌") == 2, "")

    check("plain_lines 去掉时间戳与元信息",
          plain_lines(lrc) == ["第一句歌词", "重复的副歌", "最后一句"], str(plain_lines(lrc)))
    check("plain_lines 跳过制作人员名单",
          plain_lines("作词：某某\n演唱：某人\n真正的歌词") == ["真正的歌词"],
          str(plain_lines("作词：某某\n演唱：某人\n真正的歌词")))

    # 纯文本：按字数加权，长句分到更多时间
    plain = align_plain(["短句", "这是一句明显更长的歌词内容"], 10.0)
    check("纯文本按字数加权：长句时间更长", plain[1].span > plain[0].span,
          f"{plain[0].span:.2f} vs {plain[1].span:.2f}")
    check("纯文本铺满整首", abs(sum(ln.span for ln in plain) - 10.0) < 0.01,
          str(sum(ln.span for ln in plain)))
    check("无时长时纯文本无法铺满（返回空而不是瞎猜）",
          align_plain(["词"], 0.0) == [], "")
    check("纯文本也能吸到拍上",
          all(abs(ln.start * 2 - round(ln.start * 2)) < 1e-6
              for ln in align_plain(["一二三四", "五六七八"], 4.0,
                                    beats=[i * 0.5 for i in range(9)])),
          str([ln.start for ln in align_plain(["一二三四", "五六七八"], 4.0,
                                              beats=[i * 0.5 for i in range(9)])]))
    check("空歌词返回空", parse_lyrics("", 10.0) == [] and parse_lyrics("   \n  ", 10.0) == [], "")

    # 上屏文字：caption 优先，回落 narration（纯一句话模式不受影响）
    from pipeline.models import Shot, caption_of
    s = Shot(id=1, act="起", duration=4, narration="解说词", visual="", camera="", start=0.0)
    check("没有 caption 时用解说词（纯一句话模式）", caption_of(s) == "解说词", caption_of(s))
    s.caption = "歌词一句"
    check("有 caption 时优先用歌词", caption_of(s) == "歌词一句", caption_of(s))
    check("caption 进得了分镜契约", s.to_dict().get("caption") == "歌词一句", "")
    check("dict 形式也能取（代码绘制后端拿到的是 dict）",
          caption_of({"caption": "词", "narration": "解说"}) == "词", "")

    # 歌词分配给镜头
    shots = [Shot(id=i + 1, act="起", duration=2.0, narration="", visual="", camera="")
             for i in range(5)]
    for i, sh in enumerate(shots):
        sh.start = i * 2.0
    filled = assign_captions(shots, parse_lyrics(lrc, 10.0))
    check("歌词被分配到镜头", filled == 5, str(filled))
    check("每个镜头都有上屏文字", all(sh.caption for sh in shots),
          str([sh.caption for sh in shots]))

    # SRT 按歌词自己的时间轴写（不是按镜头切）
    with tempfile.TemporaryDirectory() as td:
        out = lyric_srt(lines[:3], Path(td) / "s.srt")
        body = out.read_text(encoding="utf-8-sig")
        check("歌词 SRT 用歌词自己的时间轴", "00:00:00,750 --> 00:00:04,250" in body, body[:120])
        check("歌词 SRT 条数等于歌词行数", body.count("-->") == 3, str(body.count("-->")))
        check("歌词 SRT 带 BOM（Windows 播放器兼容）", out.read_bytes()[:3] == b"\xef\xbb\xbf", "")


def test_portrait() -> None:
    """竖屏 / 方形画幅（Step 11）：引擎与场景都必须是分辨率无关的。"""
    print("\n[11] 画幅无关性（竖屏 / 方形）")
    from pipeline import painted as pb
    from pipeline.image_render import RESOLUTIONS

    # 两处画幅表必须一致，否则成片画幅与占位帧画幅会对不上
    check("画幅表与 image_render.RESOLUTIONS 一致",
          dict(pb.ASPECT_SIZES) == dict(RESOLUTIONS),
          f"{pb.ASPECT_SIZES} vs {RESOLUTIONS}")
    check("未知/空画幅回落 16:9",
          pb.size_for_aspect("") == (1920, 1080) and pb.size_for_aspect("4:3") == (1920, 1080), "")
    check("9:16 是竖屏（宽 < 高）",
          pb.size_for_aspect("9:16")[0] < pb.size_for_aspect("9:16")[1], "")

    cfg = pb.build_config_js(15.0, size=pb.size_for_aspect("9:16"))
    check("config.js 下发画幅宽高", "width: 1080" in cfg and "height: 1920" in cfg, cfg)
    check("config.js 仍含 duration/bpm（老字段没被挤掉）",
          "duration: 15.0" in cfg and "bpm:" in cfg, cfg)

    html = pb.build_studio_html("src/scenes/yiyyan.js", karaoke=True,
                                size=pb.size_for_aspect("9:16"))
    check("studio 的输出画布尺寸跟随画幅",
          'width="1080"' in html and 'height="1920"' in html, "")
    check("studio 的 CSS aspect-ratio 跟随画幅", "aspect-ratio: 1080 / 1920" in html, "")
    check("横屏时画布仍是 1920x1080（回归）",
          'width="1920"' in pb.build_studio_html("s.js") and
          "aspect-ratio: 1920 / 1080" in pb.build_studio_html("s.js"), "")

    # 相机中心必须是**比例**：写死像素正是竖屏跑偏的根因
    zoom, cx, cy = pb._camera_for("缓慢推镜，中景", 0, 3)
    check("相机中心是归一化比例（0-1）", 0.0 < cx < 1.0 and 0.0 < cy < 1.0, f"{cx}, {cy}")
    check("相机比例与画幅无关（同一机位横竖屏同值）",
          pb._camera_for("俯拍，全景", 1, 4) == pb._camera_for("俯拍，全景", 1, 4), "")

    shots = [{"duration": 3, "act": "起", "narration": "这是字幕",
              "visual": "水彩天空", "camera": "固定机位，中景"}]
    js = pb.build_scene_js(shots, genre_label="城市宣传")
    check("场景注入单位换算 U（按短边 1080 基准）",
          "const U = Math.min(W, H) / 1080" in js, "")
    # 按 1080 标定的绝对像素尺寸必须乘 U；横屏 U=1，所以输出与以前一致
    for frag in ("const base = (34 + 7 * hash(idx * 5)) * U",
                 "const u = 30 * lerp(0.70, 1.45, fk) * U",
                 "groundY + 12 * U",
                 "* 6 * U"):
        check(f"绝对像素尺寸乘 U：{frag}", frag in js, "")
    check("相机中心用 W/H 全局而不是写死像素",
          "camBegin(kf(t, [[0, W * 0.5]" in js and "H * 0.5]]" in js, "")
    check("场景里不再出现写死的相机中心 960.0 / 540.0",
          "960.0" not in js and "540.0" not in js, "")

    # 引擎侧的两处 vendor 补丁必须还在（上游同步还原了就会静默退回横屏/字条跑偏）
    core = (ROOT / "vendor" / "painted-template" / "src" / "core.js").read_text(encoding="utf-8")
    check("core.js 从 PROJECT 读画幅（不再是写死 1920×1080）",
          "PROJECT.width" in core and "PROJECT.height" in core, "")
    kara = (ROOT / "vendor" / "painted-template" / "src" / "karaoke.js").read_text(encoding="utf-8")
    # 只检查**代码行**：文件里的解释性注释会提到"原文写死 1024/960"，
    # 用整份文件做子串匹配会把自己写的注释当成违规。
    kara_code = "\n".join(ln for ln in kara.splitlines() if not ln.strip().startswith("//"))
    check("karaoke.js 字条基线由 H 算（代码里不再写死 1024）",
          "const KBAR_Y = H - 56" in kara_code and "1024" not in kara_code, "")
    check("karaoke.js 水平中心由 W 算（代码里不再写死 960）",
          "const KCX = W / 2" in kara_code and "960" not in kara_code, "")
    check("karaoke.js 长句会缩字号（竖屏 W 更窄）", "maxText" in kara_code, "")

    # 帧缓存清理（长片磁盘卫生）
    with tempfile.TemporaryDirectory() as _fd:
        fdir = Path(_fd) / "frames"
        fdir.mkdir()
        for i in range(6):
            (fdir / f"f{i:05d}.jpg").write_bytes(b"x" * 100_000)
        freed = pb.clean_frames(fdir)
        check("clean_frames 清掉帧并报出释放量",
              freed > 0 and not list(fdir.glob("f*.jpg")), f"释放 {freed:.2f} MB")
        check("clean_frames 对不存在的目录不炸", pb.clean_frames(Path(_fd) / "nope") == 0.0, "")


def test_generated_js_syntax() -> None:
    """生成的场景 / config JS 必须能过 ``node --check``。

    这一条是被真事故逼出来的：``_paintSubject`` 属于 ``SCENE_HELPERS_JS``
    （**普通字符串**，不是 f-string），我按 f-string 的习惯写了 ``{{ }}``，
    结果原样输出成 ``{{ ... }}``、node 报 "Unexpected token '{'"。
    而这类错误**只有真渲染时才会炸**——纯 Python 单测全绿，白白浪费一次长渲染。
    """
    print("\n[13] 生成的 JS 语法（node --check）")
    import subprocess

    from pipeline import painted as pb

    try:
        node = pb._node_exe()
    except Exception:  # noqa: BLE001
        node = None
    if not node:
        print("  [SKIP] 未找到 node")
        return

    cfg = pb.build_config_js(15.0, fps=24, beat_bpm=120.0, size=(1080, 1920))
    shots = [
        {"duration": 4, "act": "起", "narration": "一句话文案",
         "visual": "水彩天空与远山", "camera": "缓慢推镜，中景"},
        {"duration": 4, "act": "承", "narration": "",
         "visual": "街景", "camera": "固定机位，特写"},
    ]
    scene = pb.build_scene_js(shots, genre_label="城市宣传")
    # 不做 "有没有 {{ }}" 这类启发式判断：`}}` 在 JS 里能合法出现（连续闭合两层块），
    # 会误报。node --check 才是权威判据。
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "check.js"
        # 场景依赖 core.js 的 W / H / PAL 等运行时常量；这里只查语法，补最小桩
        p.write_text(cfg + "const W = 1920, H = 1080;\n" + scene, encoding="utf-8")
        r = subprocess.run([node, "--check", str(p)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        check("生成的场景 JS 语法正确（node --check）", r.returncode == 0,
              (r.stderr or "")[-400:])
        # lyrics.js 也是生成的，同样要查
        ly = pb.build_lyrics_js([{"start": 0.5, "end": 2.5, "text": "夜色落在窗台上"}])
        p2 = Path(td) / "ly.js"
        p2.write_text(ly, encoding="utf-8")
        r2 = subprocess.run([node, "--check", str(p2)], capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
        check("生成的 lyrics.js 语法正确", r2.returncode == 0, (r2.stderr or "")[-300:])


def test_render_timeout() -> None:
    """长片稳定性：超时按帧数估，且空闲超时必须真能杀掉挂死的渲染进程。"""
    print("\n[12] 长片稳定性（超时与空闲判定）")
    import sys as _sys

    from pipeline import painted as pb

    check("短片超时有下限（不会因为帧少而过紧）",
          pb.render_timeout_for(30) >= 1800 and pb.render_timeout_for(360) >= 1800,
          f"{pb.render_timeout_for(30)}s")
    # 3 分钟 / 24fps ≈ 4320 帧，按 1s/帧约 72 分钟——写死 1800s 必然被杀在半路
    check("长片超时按帧数放大（3 分钟片不再被 1800s 杀在半路）",
          pb.render_timeout_for(4320) > 1800 * 4,
          f"{pb.render_timeout_for(4320)}s")
    check("超时随帧数单调增长",
          pb.render_timeout_for(1800) > pb.render_timeout_for(360), "")

    # 空闲超时：原来的实现只有读流循环**之后**的 proc.wait(timeout=)，
    # 渲染中途卡死时 stdout 不关闭 → 读流永远阻塞 → 超时根本轮不到执行。
    rc, out = pb._run_stream([_sys.executable, "-c", "import time; time.sleep(20)"],
                             cwd=ROOT, timeout=120, idle_timeout=2.0)
    check("空闲超时能杀掉卡死的渲染进程（不再无声挂死）",
          rc == 124 and "没有任何输出" in out, f"rc={rc} out={out[:70]}")
    rc2, out2 = pb._run_stream(
        [_sys.executable, "-c",
         "print('a', flush=True); print('b', flush=True)"],
        cwd=ROOT, timeout=60, idle_timeout=2.0)
    check("正常输出的进程不会被误杀", rc2 == 0 and "a" in out2 and "b" in out2, f"rc={rc2}")


def test_narration_pacing() -> None:
    """解说词节奏：每镜字数必须与镜头时长匹配（提示词库 1.3 第 7 条）。"""
    print("\n[4b] 解说词节奏（字数/秒）")
    from pipeline.llm import CHARS_PER_SEC, MAX_SHOT_CHARS, distribute_narration

    sentences = [
        "老木匠，先从一个细节说起。",
        "他的手停了一下，又继续。",
        "虎口上那道旧疤，是这里最不声张的讲究。",
        "低头专注的那一下，台灯下压着的图纸跟着动起来。",
        "抬手擦汗的这几秒，比什么话都有说服力。",
        "它不喧哗，却把一辈子只做一件事留了下来。",
    ]
    durations = [5.0] * 6
    chunks = distribute_narration(sentences, durations)
    check("切分后镜头数一致", len(chunks) == len(durations), str(len(chunks)))
    over = [
        (i + 1, len(c), round(d * CHARS_PER_SEC, 1))
        for i, (c, d) in enumerate(zip(chunks, durations))
        if len(c) > max(MAX_SHOT_CHARS, d * CHARS_PER_SEC)
    ]
    check("每镜字数不超上限", not over, f"超限镜头（镜号, 字数, 上限）：{over}")
    joined = "".join(chunks)
    check("切分不丢词", len(joined) >= sum(len(s) for s in sentences) - 12,
          f"原 {sum(len(s) for s in sentences)} 字 → 切分后 {len(joined)} 字")
    check("切分不产生空镜", all(c.strip() for c in chunks), str([len(c) for c in chunks]))


def test_offline_end_to_end() -> None:
    print("\n[5] 离线创意引擎端到端（不落盘视频）")
    from pipeline.llm import CreativeLayer
    from pipeline.image_render import render_still

    lib = PromptLibrary.load()
    creative = CreativeLayer(lib, None)
    brief = creative.make_brief("给我的家乡方城烩面做一条 15 秒宣传片", {"total_duration": 15})
    check("简报：时长为 15", brief.total_duration == 15)
    check("简报：风格已推断", bool(brief.style), brief.style)
    script = creative.make_script(brief)
    check("剧本：解说词非空", len(script.narration_full) > 20, script.narration_full[:40])
    check("剧本：起承转合四段", len(script.acts) == 4)
    check("剧本：片名不含时长噪声", "秒" not in script.title and "15" not in script.title, script.title)
    board = creative.make_storyboard(brief, script, 5.0)
    check("分镜：镜头数 ≥ 3", len(board.shots) >= 3, str(len(board.shots)))
    check("分镜：时长累加 = 简报时长", abs(board.total_duration - 15) < 0.01, str(board.total_duration))
    check("分镜：每镜都有英文图像提示词", all(s.prompt_image for s in board.shots))
    check("分镜：每镜都有验收标准且 ≤ 24 字", all(0 < len(s.acceptance) <= 24 for s in board.shots),
          str([s.acceptance for s in board.shots]))
    check("分镜：提示词包含质量词", all("masterpiece" in s.prompt_image for s in board.shots))
    check("分镜：运动提示词为英文且不含换场词",
          all(s.prompt_video and "scene change" not in s.prompt_video.replace("no scene change", "") for s in board.shots))
    # 渲染一帧
    genre = pick_genre(brief.theme)
    out = ROOT / "tests" / "_tmp_frame.png"
    render_still(board.shots[0], brief, genre, out, total_shots=len(board.shots))
    check("占位帧已渲染且体积正常", out.exists() and out.stat().st_size > 20_000,
          f"{out.stat().st_size if out.exists() else 0} bytes")
    if out.exists():
        out.unlink()
    check("占位帧可复现（同输入同哈希）", _frame_digest(brief, board, genre) == _frame_digest(brief, board, genre))


def _frame_digest(brief, board, genre) -> str:
    import hashlib

    from pipeline.image_render import render_still

    out = ROOT / "tests" / "_tmp_digest.png"
    render_still(board.shots[1], brief, genre, out, total_shots=len(board.shots))
    digest = hashlib.sha256(out.read_bytes()).hexdigest()[:16]
    out.unlink()
    return digest


def test_comfyui_workflow() -> None:
    """ComfyUI 工作流模板：加载合法性 + 管线参数改写 + provider 三态判定。"""
    print("\n[5] ComfyUI 工作流与 provider")
    from pipeline.images import ComfyUIProvider
    from pipeline.models import ProjectBrief

    wf_dir = ROOT / "workflows"
    p = ComfyUIProvider(base_url="http://127.0.0.1:1", workflow_dir=wf_dir)

    # 模板存在且为合法 JSON
    tpl = wf_dir / "txt2img.json"
    check("ComfyUI 工作流模板存在", tpl.exists(), str(tpl.name))
    if tpl.exists():
        try:
            graph = json.loads(tpl.read_text(encoding="utf-8"))
            check("模板为合法 JSON 且非空", isinstance(graph, dict) and len(graph) > 0, f"{len(graph)} 节点")
        except json.JSONDecodeError as exc:
            check("模板为合法 JSON 且非空", False, str(exc))
            return

    # 管线改写：分辨率按画幅、提示词与种子注入、负向不被覆盖
    brief = ProjectBrief(theme="烩面", total_duration=15, aspect_ratio="9:16")
    shot = Shot(id=1, act="起", duration=5, narration="测试", visual="测试画面",
                camera="推镜", prompt_image="steaming noodle bowl, close-up, golden hour")
    try:
        g = p._load_graph(shot, brief, seed=12345)
    except Exception as exc:  # noqa: BLE001
        check("管线可加载并改写模板", False, str(exc)[:80])
        return
    check("管线可加载并改写模板", isinstance(g, dict) and len(g) > 0, f"{len(g)} 节点")

    latent = g.get("4", {}).get("inputs", {})
    w, h = latent.get("width", 0), latent.get("height", 0)
    check("竖屏分辨率改写正确", h > w > 0, f"{w}x{h}")

    sampler = g.get("5", {}).get("inputs", {})
    check("种子注入正确", sampler.get("seed") == 12345, str(sampler.get("seed")))

    pos = g.get("2", {}).get("inputs", {}).get("text", "")
    check("正向提示词被镜头提示词覆盖", pos == shot.prompt_image, pos[:36])

    neg = g.get("3", {}).get("inputs", {}).get("text", "")
    check("负向提示词未被覆盖", "lowres" in neg or "worst" in neg, neg[:32])

    # 回归锁：模板里的说明字段（_meta/_note）必须在提交前剥离，
    # 否则 ComfyUI 会把它们当节点，报 missing_node_type 拒绝整个请求。
    check("顶层说明字段已剥离", "_meta" not in g, f"节点: {list(g.keys())}")
    check(
        "节点内说明字段已剥离",
        all(not any(str(k).startswith("_") for k in n) for n in g.values()),
        "存在节点仍带 _ 开头键",
    )
    check(
        "inputs 内说明字段已剥离",
        all(
            not any(str(k).startswith("_") for k in (n.get("inputs") or {}))
            for n in g.values()
        ),
        "存在 inputs 仍带 _ 开头键",
    )

    # provider 三态：服务不通时仍能分别报出模板状态
    ok, msg = p.available()
    check("服务未启动时不可用", ok is False)
    check("模板状态独立可见", "txt2img.json" in msg, msg[:60])

    # 模板缺失时也要明确报出
    with tempfile.TemporaryDirectory() as td:
        p2 = ComfyUIProvider(base_url="http://127.0.0.1:1", workflow_dir=Path(td))
        ok2, msg2 = p2.available()
        check("模板缺失时不可用", ok2 is False)
        check("缺模板原因明确", "缺少工作流模板" in msg2, msg2[:60])

    # 回归锁：出图尺寸必须与成片尺寸解耦。
    # 曾经直接把潜变量写成成片分辨率 1920x1080，SD1.5 在 8GB 显存上
    # 单张要跑 3-10 分钟甚至卡死在 VAE 解码——这是"出图极慢"的真正根因。
    for aspect in ("16:9", "9:16", "1:1"):
        a = ProjectBrief(theme="", aspect_ratio=aspect)
        try:
            gg = p._load_graph(shot, a, seed=1)
        except Exception as exc:  # noqa: BLE001
            check(f"出图尺寸 {aspect} 钳制生效", False, str(exc)[:60])
            continue
        lat = gg.get("4", {}).get("inputs", {})
        ww, hh = int(lat.get("width", 0)), int(lat.get("height", 0))
        check(
            f"出图尺寸 {aspect} 钳制生效",
            0 < max(ww, hh) <= 640 and ww % 8 == 0 and hh % 8 == 0,
            f"{ww}x{hh}",
        )

    # UI 导出格式必须被明确拒绝，而不是静默转成一张错的图
    with tempfile.TemporaryDirectory() as td:
        ui_tpl = Path(td) / "txt2img.json"
        ui_tpl.write_text(json.dumps({"nodes": [], "links": []}), encoding="utf-8")
        p3 = ComfyUIProvider(base_url="http://127.0.0.1:1", workflow_dir=Path(td))
        try:
            p3._load_graph(shot, brief, seed=1)
            check("UI 导出格式被明确拒绝", False, "没有报错，会静默产出错误图")
        except Exception as exc:  # noqa: BLE001
            check("UI 导出格式被明确拒绝", "API" in str(exc) or "UI" in str(exc), str(exc)[:60])


def test_settings_precedence() -> None:
    """配置优先级：.env 显式值（含显式空值）必须盖过系统环境变量。

    曾经是「.env 非空才认」，导致界面里把一个 Key 删掉保存后，
    只要系统里有同名环境变量，密钥照样还在用——用户以为删掉了。
    """
    print("\n[6] 配置优先级与停止信号")
    import os

    from pipeline.config import Settings

    os.environ["_YIYAN_TEST_KEY"] = "from-env"
    try:
        s_empty = Settings(values={"_YIYAN_TEST_KEY": ""})
        check("显式空值覆盖环境变量", s_empty.get("_YIYAN_TEST_KEY") == "",
              repr(s_empty.get("_YIYAN_TEST_KEY")))
        s_val = Settings(values={"_YIYAN_TEST_KEY": "from-dotenv"})
        check(".env 值优先于环境变量", s_val.get("_YIYAN_TEST_KEY") == "from-dotenv",
              repr(s_val.get("_YIYAN_TEST_KEY")))
        s_none = Settings(values={})
        check("未配置时回退环境变量", s_none.get("_YIYAN_TEST_KEY") == "from-env",
              repr(s_none.get("_YIYAN_TEST_KEY")))
    finally:
        os.environ.pop("_YIYAN_TEST_KEY", None)


def test_resource_dir() -> None:
    """随包资源定位：源码形态与 EXE 形态都要能找到 prompts / workflows。

    曾经到处写 `ROOT / "workflows"`：源码跑没问题，打包后随包数据在
    sys._MEIPASS 下而 ROOT 是 EXE 同级目录，于是永远报"缺少工作流模板"，
    EXE 形态接不上 ComfyUI。
    """
    print("\n[6b] 随包资源定位")
    from pipeline.config import resource_dir

    for name, expect_file in (("workflows", "txt2img.json"), ("prompts", "prompts.yaml")):
        d = resource_dir(name)
        check(f"{name} 目录可定位", d.exists(), str(d))
        check(f"{name} 内含 {expect_file}", (d / expect_file).exists(), str(d / expect_file))


def test_pipeline_controls() -> None:
    """管线侧的停止信号与真实进度（不跑完整流水线，只验协议）。"""
    from pipeline.pipeline import Pipeline

    # 用不触发网络与磁盘的方式构造：只验 _stop_requested 与 progress 的契约
    pipe = Pipeline.__new__(Pipeline)
    events: list[str] = []
    pipe.stopped = False
    pipe.progress = lambda stage, pct: events.append(stage)
    requested = {"v": False}
    pipe.should_stop = lambda: requested["v"]
    pipe.ctx = type("C", (), {"notes": []})()
    pipe.log = type("L", (), {"event": lambda self, kind, msg, **kw: events.append(msg)})()

    check("未请求停止时放行", pipe._stop_requested("镜 1") is False)
    check("放行时不写日志", not events and pipe.stopped is False)
    requested["v"] = True
    check("收到停止信号后返回 True", pipe._stop_requested("镜 2") is True)
    check("收到停止信号后置位 stopped", pipe.stopped is True)
    check("停止会写日志与人工关注项", len(pipe.ctx.notes) == 1 and len(events) == 1)
    check("再次调用直接短路", pipe._stop_requested("镜 3") is True)
    check("短路不重复写日志", len(events) == 1 and len(pipe.ctx.notes) == 1)

    # 进度回调值域：管线推的是 0-1 的比例，界面再乘 100
    seen: list[tuple[str, float]] = []
    p2 = Pipeline.__new__(Pipeline)
    p2.stopped = False
    p2.progress = lambda stage, pct: seen.append((stage, pct))
    p2.should_stop = lambda: False
    for stage, pct in [("环境体检", 0.05), ("创意层 · 简报", 0.10), ("完成", 1.0)]:
        p2.progress(stage, pct)
    check("进度取值落在 0-1", all(0.0 <= p <= 1.0 for _, p in seen), str(seen))
    check("进度阶段名非空", all(s for s, _ in seen))


def _mock_comfy_server() -> HTTPServer:
    """起一个只实现 /system_stats 的假ComfyUI，用于验证'服务通'分支。"""

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.startswith("/system_stats"):
                body = json.dumps({"system": {"comfyui_version": "0.3.40-test"}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _css_blocks(css: str, selector: str) -> list[str]:
    """取出某个选择器的**所有**声明块内容（大括号配对，不靠正则）。

    深色令牌在样式表里分成两段写（主令牌 + 开关控件），所以要取全部再合并；
    用正则抓 `{...}` 会在嵌套的 `@media` / 渐变里的括号上翻车。
    """
    out: list[str] = []
    pos = 0
    needle = selector + " {"
    while True:
        idx = css.find(needle, pos)
        if idx < 0:
            return out
        start = css.index("{", idx)
        depth = 0
        end = -1
        for i in range(start, len(css)):
            if css[i] == "{":
                depth += 1
            elif css[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end < 0:
            return out
        out.append(css[start + 1:end])
        pos = end + 1


def _css_decls(block: str) -> dict[str, str]:
    """把一块 CSS 里的自定义属性拆成字典。"""
    out: dict[str, str] = {}
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith("--") or ":" not in line:
            continue
        key, val = line.split(":", 1)
        out[key.strip()] = val.strip().rstrip(";").strip()
    return out


def _lum(color: str) -> float:
    """#rrggbb 的相对亮度，只用来判断"偏深还是偏亮"，不追求色度学精度。

    解析不出来返回 -1，调用处必须把它当成"没定义"而不是"很暗"。
    """
    c = (color or "").strip()
    if not c.startswith("#") or len(c) != 7:
        return -1.0
    try:
        r, g, b = (int(c[i:i + 2], 16) / 255 for i in (1, 3, 5))
    except ValueError:
        return -1.0
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def test_theme_tokens() -> None:
    """深浅两套令牌必须一一对应，否则换肤时必然漏配色。

    V4 把界面从 Tkinter 换成了 React，令牌表也跟着搬到 `web/src/index.css`：
    浅色写在 `:root`，深色写在 `[data-theme="dark"]`。这里守两条：

    1. 深色块必须把浅色块定义的**每一个颜色令牌**都覆盖一遍。少一个，
       切到深色时那个属性还留着浅色的值——表现是某块地方白得刺眼，
       而且往往只在某一个页面上出现，靠肉眼点页面很难发现。
    2. 正文色与底色的对比方向不能反：浅色主题里墨色要深，深色主题里墨色要亮。
       反过来写错了不会报错，只会整页看不清字。
    """
    print("\n[14] 前端主题令牌（深浅两套必须对齐）")

    css_path = ROOT / "web" / "src" / "index.css"
    check("前端样式表存在", css_path.exists(), str(css_path))
    if not css_path.exists():
        return
    css = css_path.read_text(encoding="utf-8")

    # 字体栈与缓动曲线不参与换肤：浅色定义一次，深色不必重复
    def _skin(d: dict[str, str]) -> dict[str, str]:
        return {k: v for k, v in d.items()
                if not k.startswith(("--font-", "--ease-"))}

    # 浅色令牌分两处：`@theme` 放 Tailwind 语义色（--color-*），`:root` 放玻璃质感
    # 那一套（--glass-* / --edge / --sunk…）。两处都算浅色，漏掉 @theme 的话
    # 「深色是否覆盖全部令牌」这条会失去一半意义。
    light = _skin(_css_decls("".join(_css_blocks(css, "@theme"))
                             + "".join(_css_blocks(css, ":root"))))
    dark: dict[str, str] = {}
    for blk in _css_blocks(css, '[data-theme="dark"]'):
        dark.update(_css_decls(blk))
    dark = _skin(dark)

    check("浅色主题有一整套颜色令牌", len(light) >= 15, f"只有 {len(light)} 个")
    missing = sorted(set(light) - set(dark))
    check("深色主题覆盖了浅色的每一个颜色令牌", not missing, f"深色缺 {missing}")

    l_ink, d_ink = light.get("--color-ink", ""), dark.get("--color-ink", "")
    check("浅色主题的正文是深墨色", 0 <= _lum(l_ink) < 0.35, f"--color-ink={l_ink}")
    check("深色主题的正文是亮色", _lum(d_ink) > 0.65, f"--color-ink={d_ink}")
    check("两套主题的凹陷区取值不同",
          light.get("--sunk") != dark.get("--sunk"),
          f"{light.get('--sunk')} / {dark.get('--sunk')}")
    check("主色与主色底衬都已定义",
          "--color-brand" in light and "--color-brand-soft" in dark, "")

    app = (ROOT / "web" / "src" / "App.tsx").read_text(encoding="utf-8")
    check("主题切换真的写到 html[data-theme]（样式表靠它匹配）",
          "documentElement.dataset.theme" in app, "")


def test_ffmpeg_env_guard() -> None:
    """环境体检的新增防护：版本探测通过 ≠ 能读写磁盘。"""
    from pipeline.video import (
        VideoError,
        ffmpeg_available,
        ffmpeg_filesystem_accessible,
        run_ffmpeg,
    )

    ok, detail = ffmpeg_available()
    check("FFmpeg 版本探测有明确结论", isinstance(ok, bool) and bool(detail), detail[:60])

    io_ok, io_detail = ffmpeg_filesystem_accessible()
    check("FFmpeg 写盘探针有明确结论", isinstance(io_ok, bool) and bool(io_detail), io_detail[:60])
    # 结论必须自洽：探针说不通过时，ready 不应声称环境就绪
    if not io_ok:
        check("写盘失败时给出可执行建议", "PowerShell" in io_detail or "CMD" in io_detail,
              io_detail[:80])

    # 错误包装必须区分「权限被拒」与「文件缺失」，否则误导排查方向
    with tempfile.TemporaryDirectory() as td:
        try:
            run_ffmpeg(["-f", "lavfi", "-i", "color=c=black:s=16x16:d=1",
                        "-frames:v", "1", str(Path(td) / "x.png")], timeout=90)
            wrote = (Path(td) / "x.png").exists()
        except VideoError as exc:
            wrote = False
            msg = str(exc)
            check("FFmpeg 失败报错带排查提示", "提示" in msg, msg[:100])
    check("写盘探针结论与实际一致（正常环境应产出文件）" if io_ok
          else "写盘探针结论与实际一致（受限环境应无文件）", wrote == io_ok)


def test_llm_config_robustness() -> None:
    """接入 API Key 的路径必须抗得住用户的不规范填法，并给出可执行报错。

    背景：用户反馈「接自己的 API key 总是报错」。根因有两类，都不能靠猜——
    1. 端点填法五花八门（漏 /v1、填全 chat/completions、带尾斜杠），
       硬拼 URL 必然 404；
    2. 报错只有 HTTP 401 这种天书，用户不知道自己该改哪。
    """
    from pipeline.llm import CreativeLayer, LLMError, OpenAIChatClient, _explain_llm_http
    from pipeline.prompts import PromptLibrary

    want = "https://api.deepseek.com/v1/chat/completions"
    variants = {
        "漏 /v1": "https://api.deepseek.com",
        "带 /v1": "https://api.deepseek.com/v1",
        "带尾斜杠": "https://api.deepseek.com/v1/",
        "填全路径": "https://api.deepseek.com/v1/chat/completions",
    }
    for name, raw in variants.items():
        got = OpenAIChatClient(base_url=raw, api_key="k", model="m")._endpoint()
        check(f"端点规整（{name}）", got == want, got)

    try:
        OpenAIChatClient(base_url="", api_key="k", model="m")._endpoint()
        check("空端点应报错", False)
    except LLMError:
        check("空端点应报错", True)
    try:
        OpenAIChatClient(base_url="api.deepseek.com", api_key="k", model="m")._endpoint()
        check("缺协议的端点应报错", False)
    except LLMError:
        check("缺协议的端点应报错", True)

    # 报错必须能指导行动，而不是把 JSON 天书甩给用户
    check("401 提示指向密钥", "密钥" in _explain_llm_http(401, "Authentication Fails", "u", "deepseek-chat"))
    check("404 提示指向端点与模型", "模型名" in _explain_llm_http(404, "not found", "u", "x")
          and "/v1" in _explain_llm_http(404, "not found", "u", "x"))
    check("402 提示余额", "余额" in _explain_llm_http(402, "insufficient", "u", "x"))
    check("429 提示限流", "限流" in _explain_llm_http(429, "rate", "u", "x"))
    check("5xx 提示服务端故障", "服务端" in _explain_llm_http(503, "bad gw", "u", "x"))

    # 解析失败必须被记录，不能静默降级（用户因此看不到模型到底用没用上）
    class BadClient:
        name = "bad"

        def chat(self, system, user, temperature=0.7):
            return "抱歉，我无法完成这个请求。"  # 完全不是 JSON

    lib = PromptLibrary.load()
    layer = CreativeLayer(lib, BadClient())
    brief = layer.make_brief("方城烩面", overrides={"total_duration": 15, "aspect_ratio": "16:9"})
    check("简报仍能产出（降级兜底）", bool(brief.theme))
    check("解析失败被记录", any("不是合法 JSON" in d for d in layer.degradations),
          str(layer.degradations)[:100])

    class TruncClient:
        name = "trunc"

        def chat(self, system, user, temperature=0.7):
            return '{"title":"测试"}'  # 合法 JSON 但缺 narration_full

    layer2 = CreativeLayer(lib, TruncClient())
    brief2 = layer2.make_brief("方城烩面", overrides={"total_duration": 15, "aspect_ratio": "16:9"})
    script2 = layer2.make_script(brief2)
    check("残缺 JSON 降级到离线剧本（不抛错）", bool(script2.narration_full))
    check("缺字段被记录", any("缺少必需字段 narration_full" in d for d in layer2.degradations),
          str(layer2.degradations)[:110])

    class ArrayClient:
        name = "arr"

        def chat(self, system, user, temperature=0.7):
            return "[1,2,3]"

    layer3 = CreativeLayer(lib, ArrayClient())
    layer3.make_brief("方城烩面", overrides={"total_duration": 15, "aspect_ratio": "16:9"})
    check("返回数组被识别为不合契约", any("而不是 JSON 对象" in d for d in layer3.degradations),
          str(layer3.degradations)[:100])


def test_startup_latency_and_quality() -> None:
    """启动延迟与画质档位（合并自 V1 的两个修复，防止回退）。

    这两项都是"用户能直接感觉到"的东西：
      1. 每次点「开始生成」不该先白等一次 ComfyUI 连接超时；
      2. 画质档位是唯一有效的提速手段，且换档必须让帧缓存失效。
    """
    print("\n[9] 启动延迟与水彩画质档位")
    import inspect as _insp
    import time as _t
    from pipeline import painted as _pb
    from pipeline import images as _img
    from pipeline.config import Settings as _S, load_env_file as _lef, resource_dir as _rd

    # ---- 探测超时：必须是亚秒级
    _isrc = _insp.getsource(_img)
    check("ComfyUI 探测超时 <1秒（否则每次白等整个超时）",
          "timeout=0.6" in _isrc, "")
    check("探测不再用 8 秒超时", "timeout=8" not in _isrc, "")

    # ---- TTL 缓存
    _s = _S(values=_lef())
    _wd = _rd("workflows")
    _t0 = _t.time(); _img.provider_status(_s, _wd, fresh=True); _t1 = _t.time()
    for _ in range(3):
        _img.provider_status(_s, _wd)
    _t2 = _t.time()
    check("首次探测在 1.5 秒内", (_t1 - _t0) < 1.5, f"{_t1-_t0:.2f}s")
    check("后续走缓存（3 次合计 <0.1 秒）", (_t2 - _t1) < 0.1, f"{_t2-_t1:.3f}s")
    check("缓存返回副本（改它不污染内部状态）",
          _img.provider_status(_s, _wd)[0] is not _img.provider_status(_s, _wd)[0], "")

    # ---- 出片主路径也复用缓存
    _t3 = _t.time(); _chain = _img.build_provider_chain(_s, _wd); _t4 = _t.time()
    check("build_provider_chain 复用缓存（<0.2秒）", (_t4 - _t3) < 0.2, f"{_t4-_t3:.3f}s")
    check("provider 链至少含 offline 兜底",
          any(p.name == "offline" for p in _chain), str([p.name for p in _chain]))

    # ---- 启动时体检不得同步阻塞（它含网络探测）
    import app.engine as _eng
    _wsrc = _insp.getsource(_eng.Engine._worker)
    check("启动时体检放到后台线程", "_refresh_env_bg" in _wsrc, "")
    check("启动时不再同步调 on_env(check_env(...))",
          "self.on_env(self.check_env(settings))" not in _wsrc, "")

    # ---- 画质档位
    check("三档预设齐全", set(_pb.QUALITY_PRESETS) == {"high", "medium", "fast"},
          str(sorted(_pb.QUALITY_PRESETS)))
    _fast = _pb.quality_js("fast")
    check("fast 档关闭纸纹与渗色",
          "texScale: 0.0" in _fast and "bleedScale: 0.0" in _fast, _fast)
    check("high 档保持出厂画质", "texScale: 1.0" in _pb.quality_js("high"), "")
    check("未知档位回落默认而非崩溃",
          "texScale" in _pb.quality_js("不存在的档"), "")

    # ---- quality.js 必须先于 core.js 加载（否则系数读不到 → 退化成全画质）
    _html = _pb.build_studio_html("src/scenes/yiyyan.js")
    check("studio 在 core.js 之前加载 quality.js",
          _html.index("src/quality.js") < _html.index("src/core.js"), "")

    # ---- 换档必须让帧缓存失效（否则复用旧质感的帧 = 静默串片）
    _src2 = _insp.getsource(_pb._render_shots_impl)
    check("画质纳入帧缓存签名", "--quality--" in _src2, "")
    check("quality.js 在写 studio.html 之前落盘",
          _src2.index('"quality.js"') < _src2.index('"studio.html"'), "")

    # ---- 引擎侧确实读了系数
    from pathlib import Path as _P
    _core = _P(_pb.template_dir()) / "src" / "core.js"
    if _core.exists():
        _c = _core.read_text(encoding="utf-8")
        check("core.js 读 window.QUALITY", "window.QUALITY" in _c, "")
        check("core.js 把系数作用到 fillTexture", "Q_TEX" in _c and "fillTexture" in _c, "")


def test_painted_backend() -> None:
    """代码绘制后端：场景编译的正确性与降级路径。

    这里只测"能不能正确编译出合法 JS"，不测实际渲染——渲染一帧要 1 秒以上，
    放进单测会拖垮整套测试。真实渲染由 tools 下的独立脚本验证。
    """
    from pipeline import painted as pb

    # 环境体检：无论 Node/浏览器是否齐备，都不能抛异常，且 detail 要说人话
    st = pb.check_env()
    check("体检返回布尔值", isinstance(st.ok, bool), str(type(st.ok)))
    check("体检带可读说明", bool(st.detail), st.detail)
    if not st.ok:
        # 不可用时 detail 必须包含"怎么办"，否则用户只看到一句"不可用"
        check("不可用时给出修复建议",
              any(k in st.detail for k in ("解决", "安装", "设置")), st.detail)

    shots = [
        {"duration": 5.0, "narration": "方城的烩面，先从一个细节说起。",
         "camera": "缓慢推镜，中景", "act": "起"},
        {"duration": 5.0, "narration": "门帘一掀，热气先冲出来。",
         "camera": "固定机位，特写", "act": "承"},
        {"duration": 5.0, "narration": "羊骨吊足八小时的高汤，是这里最不声张的讲究。",
         "camera": "缓慢拉镜，全景", "act": "转"},
    ]

    js = pb.build_scene_js(shots, genre_label="烟火美食")
    check("生成场景 JS 有实际内容", len(js) > 500, f"{len(js)} 字节")
    for i in range(3):
        check(f"镜头 {i + 1} 有绘制函数", f"function shot{i}(" in js, "")
    # 时间表断言用正则而不是字面量。注意拆分要按 "], " 而不是 "," ——
    # 项内部本身含逗号（[t0, fn]），按逗号拆会把 "0.0" 和 "shot0" 切开。
    import re as _re
    table = _re.search(r"shots\(\[(.*?)\]\)", js, _re.S)
    got = [p.strip() for p in table.group(1).split("],")] if table else []
    got = [(g + "]") if not g.endswith("]") else g for g in got]
    check("镜头时间表按累计时长排布",
          got == ["[0.0, shot0]", "[5.0, shot1]", "[10.0, shot2]"],
          str(got))
    # 纯函数约束：不得出现顶层 let/var 可变状态（引擎会乱序并行渲染帧）
    check("场景不含跨帧可变状态（每帧是 t 的纯函数）",
          "\nlet " not in js and "\nvar " not in js, "")

    # PROJECT 必须单独成文件，否则与 config.js 冲突
    cfg = pb.build_config_js(15.0)
    check("config.js 含 PROJECT", "const PROJECT = { duration: 15.0" in cfg, cfg[:80])
    check("场景 JS 不重复定义 PROJECT", "const PROJECT" not in js, "")

    # studio 页面不能加载 demo 场景，否则会把示例镜头混进成片
    html = pb.build_studio_html("src/scenes/yiyyan.js")
    check("studio 加载生成的场景", "yiyyan.js" in html, "")
    check("studio 不加载 demo 场景", "demo.js" not in html, "")

    # 打包形态回归：工作副本里没有 node_modules，p5 只能走绝对路径加载。
    # 曾踩坑：work_dir() 把 studio.html 改成绝对路径，但渲染时又整体覆盖
    # studio.html 把改写冲掉，EXE 下 p5 加载失败、控制台一片空白。
    abs_html = pb.build_studio_html("src/scenes/yiyyan.js",
                                    modules_prefix="C:/x/vendor/painted-template/node_modules")
    check("studio 支持自定义 p5 加载前缀（打包形态）",
          "C:/x/vendor/painted-template/node_modules/p5/lib/p5.min.js" in abs_html
          and 'src="node_modules/p5/lib/p5.min.js"' not in abs_html, "")
    check("模块前缀用正斜杠（file 路径要求）", "\\" not in abs_html, "")

    # render.mjs 用 ESM import puppeteer-core，Node 只从入口文件所在目录向上找
    # node_modules，NODE_PATH 对 ESM 无效——所以打包形态必须有联接/复制兜底。
    check("存在 node_modules 联接兜底函数", hasattr(pb, "_link_modules"), "")
    check("存在模块前缀解析函数", callable(getattr(pb, "modules_prefix", None)), "")

    # 回归：copy2 会连权限位一起复制，模板在 PyInstaller 解包目录里只读的话，
    # 工作副本也跟着只读，之后写 config.js 直接 Permission denied
    # （模拟打包形态实测踩到：渲染 0 帧就报 Permission denied）。
    check("工作副本会补写权限", callable(getattr(pb, "_ensure_writable", None)), "")
    import tempfile as _tf
    _ro = Path(_tf.mkdtemp(prefix="ro_")) / "sub"
    _ro.mkdir()
    _f = _ro / "a.js"
    _f.write_text("x", encoding="utf-8")
    try:
        os.chmod(_f, 0o444)
        pb._ensure_writable(_ro)
        writable = bool(os.stat(_f).st_mode & 0o200)
    except OSError:
        writable = True  # 权限位不被支持的环境（如部分 Windows FS）跳过断言
    check("只读文件被补上写权限", writable, "")
    check("模块前缀默认走相对路径（源码形态）",
          pb.modules_prefix() == "node_modules"
          or pb.modules_prefix().endswith("node_modules"),
          pb.modules_prefix())

    # 景别映射：中文复合机位名（「缓慢推镜，特写」）必须能识别出景别
    z_mid = pb._camera_for("缓慢推镜，中景", 0, 3)[0]
    z_close = pb._camera_for("固定机位，特写", 1, 3)[0]
    z_wide = pb._camera_for("缓慢拉镜，全景", 2, 3)[0]
    check("景别映射正确（特写 > 中景 > 全景）", z_close > z_mid > z_wide,
          f"中景{z_mid} 特写{z_close} 全景{z_wide}")
    check("未知机位有安全兜底", pb._camera_for("某种没见过的运镜", 0, 3)[0] > 0, "")

    # 字幕截断：长句必须在标点处断，不能砍成半句
    cap = pb._caption_for("羊骨吊足八小时的高汤，是这里最不声张的讲究")
    check("字幕被压到可显示长度", len(cap) <= 28, cap)
    check("字幕不以标点残尾结尾", not cap.endswith(("，", "、", "；", ",")), cap)
    check("空解说词返回空字幕", pb._caption_for("") == "", "")
    check("短句原样保留", pb._caption_for("短句。") == "短句。", pb._caption_for("短句。"))

    # 音轨接线回归：编码命令原先只有图像输入，于是「代码绘制」成片**没有音轨**、
    # 配音整体丢失，而界面/小结照样显示「配音：有」。这里断言音轨一定接上：
    # 有音频就用音频，没有就铺等长静音，绝不产出无声文件。
    vargs = pb.build_encode_args(Path("out/frames/f%05d.jpg"), Path("v.mp4"), 24, "ffmpeg")
    check("无配音时仍输出音轨（静音兜底）",
          "anullsrc=r=44100:cl=stereo" in vargs, str(vargs))
    check("无配音时音频流被映射", vargs.count("-map") == 2 and "1:a:0" in vargs, str(vargs))
    check("视频编码参数保持", "libx264" in vargs and "yuv420p" in vargs, "")
    check("成片时长以视频为准（-shortest）", "-shortest" in vargs, "")

    with tempfile.TemporaryDirectory() as _ad:
        a = Path(_ad) / "vo.m4a"
        a.write_bytes(b"\x00" * 32)
        aargs = pb.build_encode_args(Path("out/frames/f%05d.jpg"), Path("v.mp4"), 24,
                                     "ffmpeg", audio=a)
        check("有配音时音频作为第二路输入", str(a) in aargs, str(aargs))
        check("有配音时用 aac 编码音轨", "aac" in aargs, "")
        miss = pb.build_encode_args(Path("f%05d.jpg"), Path("v.mp4"), 24, "ffmpeg",
                                    audio=Path(_ad) / "nope.m4a")
        check("配音文件不存在时回落静音轨，而不是无声失败",
              "anullsrc=r=44100:cl=stereo" in miss, "")

    # 帧缓存回归：引擎（render.mjs:122）只按「文件存在且 >1000B」跳过渲染，所以
    # 上一次留下的帧会被静默复用（实测踩过：请求 15 秒却产出 18 秒、画面还是旧的）。
    # 但断点续渲是长片刚需，不能无脑清空——用渲染签名区分二者。
    with tempfile.TemporaryDirectory() as _fd:
        fdir = Path(_fd) / "frames"
        fdir.mkdir()
        for i in range(5):
            (fdir / f"f{i:05d}.jpg").write_bytes(b"x" * 2000)
        k1 = pb.frames_cache_key("scene-A", "config-A", 24)
        k2 = pb.frames_cache_key("scene-B", "config-B", 24)
        check("同一支片签名稳定", k1 == pb.frames_cache_key("scene-A", "config-A", 24), "")
        check("换分镜后签名改变", k1 != k2, "")
        check("换帧率后签名改变", k1 != pb.frames_cache_key("scene-A", "config-A", 12), "")

        n1 = pb.prepare_frames_dir(fdir, k1)
        check("无签名时清理旧帧（首次渲染不吃旧帧）",
              n1 == 5 and not list(fdir.glob("f*.jpg")), f"清掉 {n1} 张")
        for i in range(3):
            (fdir / f"f{i:05d}.jpg").write_bytes(b"x" * 2000)
        n2 = pb.prepare_frames_dir(fdir, k1)
        check("签名一致时保留帧（断点续渲不受影响）",
              n2 == 0 and len(list(fdir.glob("f*.jpg"))) == 3, f"清掉 {n2} 张")
        n3 = pb.prepare_frames_dir(fdir, k2)
        check("换了片子则清理旧帧（不再串片）",
              n3 == 3 and not list(fdir.glob("f*.jpg")), f"清掉 {n3} 张")
        check("签名文件名不会与 f*.jpg 混淆",
              not pb.FRAMES_KEY_FILE.startswith("f"), pb.FRAMES_KEY_FILE)

    # config.js 必须写入**真实**节拍：引擎用它驱动角色呼吸与舞步，
    # 此前是死参数（bpm 恒 96.0、offset 恒 0）
    real_cfg = pb.build_config_js(15.0, fps=24, beat_bpm=128.0, beat_offset=0.31)
    check("config.js 写入真实 bpm", "bpm: 128.0" in real_cfg, real_cfg[:110])
    check("config.js 写入真实 offset（此前恒为 0）", "offset: 0.31" in real_cfg, real_cfg[:110])
    default_cfg = pb.build_config_js(15.0)
    check("无节拍时回落默认 BPM（引擎 bpm=0 会算出 Infinity）",
          f"bpm: {pb.DEFAULT_BPM}" in default_cfg, default_cfg[:110])

    # 逐字卡拉OK（Step 9）：引擎自带 karaoke.js，但两份 studio.html 都没加载它。
    # 接线有三处硬要求，任何一处断了画面就退回整句字幕或干脆没有字幕。
    kara = pb.build_lyrics_js([{"start": 0.8, "end": 3.2, "text": "夜色落在窗台上"}])
    check("lyrics.js 注入 window.KARAOKE_LY", "window.KARAOKE_LY" in kara, kara[:80])
    check("lyrics.js 用引擎要的 [start, end, 词] 格式",
          "[0.8, 3.2," in kara and "夜色落在窗台上" in kara, kara)
    check("空歌词也生成合法 JS（不是 'undefined'）",
          "window.KARAOKE_LY = [" in pb.build_lyrics_js(None), "")
    check("坏行被跳过（不产生 NaN）",
          "NaN" not in pb.build_lyrics_js([{"start": None, "end": "x", "text": "词"}]), "")

    html_k = pb.build_studio_html("src/scenes/yiyyan.js", karaoke=True)
    check("karaoke 模式加载 karaoke.js", "src/karaoke.js" in html_k, "")
    check("karaoke 模式加载生成的 lyrics.js", "src/lyrics.js" in html_k, "")
    # 顺序是硬约束：karaoke.js 的 window.EXTRA_FONTS 在加载时按 LY 算字体预载集，
    # lyrics.js 必须在它之前定义 window.KARAOKE_LY
    check("lyrics.js 先于 karaoke.js 加载",
          html_k.index("src/lyrics.js") < html_k.index("src/karaoke.js"), "")
    # karaoke.js 用了 core.js 的 PAL/hash/ease 等全局
    check("karaoke.js 晚于 core.js 加载",
          html_k.index("src/core.js") < html_k.index("src/karaoke.js"), "")
    check("不开 karaoke 时页面与以前完全一致",
          "karaoke.js" not in pb.build_studio_html("src/scenes/yiyyan.js")
          and "lyrics.js" not in pb.build_studio_html("src/scenes/yiyyan.js"), "")

    # 开了卡拉OK 后场景自身的整句字幕必须关掉，否则两套字幕叠一起
    with_ly = pb.build_scene_js([{"duration": 3, "narration": "整句字幕", "camera": "固定机位",
                                  "act": "起", "visual": "x"}], captions=False)
    check("captions=False 时不画场景自带字幕", "整句字幕" not in with_ly, "")
    check("captions=True（默认）仍画场景字幕",
          "整句字幕" in pb.build_scene_js([{"duration": 3, "narration": "整句字幕",
                                          "camera": "固定机位", "act": "起", "visual": "x"}]), "")

    # 引擎自带的 karaoke.js 必须【读注入的歌词】，否则整条线白接
    kjs = (ROOT / "vendor" / "painted-template" / "src" / "karaoke.js").read_text(encoding="utf-8")
    check("karaoke.js 读 window.KARAOKE_LY（本项目对 vendor 的唯一改动）",
          "window.KARAOKE_LY" in kjs,
          "若上游同步把这行还原，逐字卡拉OK 会静默退回无歌词")

    # 帧缓存签名必须含歌词：开了卡拉OK 后场景 JS 里没有字幕，
    # 只换歌词时场景 JS 完全一样，不纳入签名就会复用旧帧、画面上还是上一版词
    k1 = pb.frames_cache_key("scene", "cfg", 24)
    k2 = pb.frames_cache_key("scene", "cfg", 24, extra='window.KARAOKE_LY=["a"];')
    check("只换歌词也会让帧缓存失效", k1 != k2, "")
    check("extra 不参与时签名与以前一致",
          pb.frames_cache_key("scene", "cfg", 24, extra="") == k1, "")

    # 渲染并发与重试：并发越多越容易崩（实测 ≥2 page 时 12% 概率导航超时），
    # 而且并不更快（1→8 workers 只 1.20×，瓶颈在集显 WebGL）。
    check("渲染并发不随核数放大", pb.default_workers() <= pb.MAX_WORKERS,
          f"{pb.default_workers()} > {pb.MAX_WORKERS}")
    check("渲染并发至少有 2（单 worker 浪费）", pb.default_workers() >= 2, "")

    _calls = {"n": 0}

    def _flaky() -> tuple[int, str]:
        _calls["n"] += 1
        return (1, "boom") if _calls["n"] < 3 else (0, "ok")

    _msgs: list[str] = []
    _code, _out = pb.render_with_retry(_flaky, attempts=3, say=_msgs.append)
    check("渲染失败会重试（帧缓存让重试等于续渲，不从头来）",
          _code == 0 and _calls["n"] == 3, f"code={_code} 调用 {_calls['n']} 次")
    check("重试有可读提示", len(_msgs) == 2 and "重试" in _msgs[0], str(_msgs))

    _always = {"n": 0}

    def _fail() -> tuple[int, str]:
        _always["n"] += 1
        return (1, "boom")

    _code2, _ = pb.render_with_retry(_fail, attempts=2)
    check("重试到上限后如实返回失败（不假装成功）",
          _code2 != 0 and _always["n"] == 2, f"code={_code2} 调用 {_always['n']} 次")

    # apply_painted：GUI 与 CLI 共用的一条通路
    _r0 = pb.apply_painted({"storyboard": {"shots": []}})
    check("apply_painted 无分镜时优雅跳过并写 notes",
          bool(_r0.get("notes")) and "已跳过" in _r0["notes"][0], str(_r0.get("notes")))
    _orig_render = pb.render_shots
    pb.render_shots = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("模拟渲染炸了"))
    try:
        _r1 = pb.apply_painted(
            {"storyboard": {"shots": [{"duration": 3, "narration": "x"}]},
             "compose": {"video": "old.mp4", "audio_source": "sapi", "audio": "vo.m4a"}},
            log=lambda level, msg: None)
    finally:
        pb.render_shots = _orig_render
    check("apply_painted 渲染失败时保留原成片（不抛异常、不改 compose）",
          _r1["compose"]["video"] == "old.mp4", str(_r1.get("compose")))
    check("apply_painted 失败写进 notes 供人工关注",
          any("代码绘制" in n for n in _r1.get("notes", [])), str(_r1.get("notes")))

    # 无镜头时必须优雅失败而不是抛异常
    r = pb.render_shots([], Path("work") / "_painted_empty")
    check("空镜头列表返回失败而不是异常", not r["ok"], str(r.get("error", "")))
    check("失败时给出原因", bool(r.get("error")), str(r.get("error", "")))
    check("失败时如实上报无音轨", r.get("has_audio") is False, str(r.get("has_audio")))


def test_packaging_exclusions() -> None:
    """打包规则：体积要瘦，但代码绘制的模板一件都不能少。

    这一条守的是一类**源码模式永远看不见**的缺陷：模板在项目里、可写、
    node_modules 就在旁边，怎么跑都对；一旦打进包，`sys._MEIPASS` 是只读的
    临时目录，`render.mjs` 又只按 ESM 规则从自己所在目录逐级向上找
    node_modules —— 少带一个文件，表现就是「代码绘制不可用：缺少渲染模板」，
    或者干脆渲 0 帧。

    上一版是 `build.py`（Tkinter）在守，它随旧前端一起删掉了，这里改为守
    `build_web.py`。
    """
    print("\n[8] 打包规则（分发包体积与可用性）")
    import importlib.util

    spec = importlib.util.spec_from_file_location("_build_under_test",
                                                  ROOT / "build_web.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)

    data = list(getattr(build, "DATA_DIRS", []))
    excl = list(getattr(build, "VENDOR_EXCLUDE", []))

    check("随包数据里有前端产物", "web/dist" in data, str(data))
    # 只带 src/ 与 studio.html 是不够的：painted.py 还要复制 render.mjs、
    # package.json、package-lock.json，并链接 node_modules。
    check("代码绘制模板是整套随包（不是只带 src）",
          "vendor/painted-template" in data, str(data))

    tpl = ROOT / "vendor" / "painted-template"
    for need in ("render.mjs", "studio.html", "package.json", "node_modules"):
        check(f"模板里确实有 {need}（打包后要能渲染）", (tpl / need).exists(),
              str(tpl / need))

    check("排除渲染产物 out/", "out" in excl, str(excl))
    check("排除上游仓库残留 .git/", ".git" in excl, str(excl))
    check("node_modules 不能排除（排掉就要用户自己 npm install）",
          "node_modules" not in excl, str(excl))

    src = Path(build.__file__).read_text(encoding="utf-8")
    check("排除发生在复制阶段（ignore_patterns 带上 VENDOR_EXCLUDE）",
          "VENDOR_EXCLUDE" in src and "ignore_patterns(*VENDOR_EXCLUDE)" in src, "")
    check("打包入口是 desktop.py（不是旧 Tk 入口）",
          str(getattr(build, "ENTRY", "")).endswith("desktop.py"),
          str(getattr(build, "ENTRY", "")))
    check("旧 Tk 前端已不在仓库里",
          not (ROOT / "yiyan_gui.py").exists()
          and not (ROOT / "app" / "gui.py").exists(), "")

    # multipart 是 4.4 加音轨上传时踩到的坑：FastAPI 在
    # `ensure_multipart_is_installed()` 里用 try/except 试导入它，静态分析把它
    # 当成可选依赖跳过。2026-10-21 实测：不加 hiddenimports 打出来的包，
    # `_internal/` 下既没有 python_multipart 也没有 multipart，上传接口必然 500。
    hidden = list(getattr(build, "HIDDEN", []))
    check("hiddenimports 带 python_multipart（否则打包版上传音轨 500）",
          "python_multipart" in hidden, str(hidden))
    check("hiddenimports 带 python_multipart.multipart（解析器本体）",
          "python_multipart.multipart" in hidden, str(hidden))

def test_replan_act() -> None:
    """C 期：按场局部重拆。只重拆一场，其余场次一字不动。

    这一期最容易出错的是**镜号顺移**：重拆后这一场的镜数会变，后面场的镜号
    跟着挪，而 `meta.edited.shots` / `stale_shots` 存的都是镜号——不映射就会
    把"你改过"的徽章标到别的镜头上。徽章错了比没有更糟，所以这里专门盯住它。
    """
    print("\n[9] 按场局部重拆（C 期）")
    import shutil

    from pipeline.config import RunContext, Settings
    from pipeline.llm import CreativeLayer
    from pipeline.pipeline import Pipeline
    from pipeline.runlog import RunLog

    root = Path(tempfile.mkdtemp(prefix="replan_"))
    try:
        plan_dir = root / "plan"
        plan_dir.mkdir(parents=True)
        brief = {
            "theme": "测试片", "total_duration": 16, "aspect_ratio": "16:9",
            "style": "纪实", "language": "zh",
            "must_include": [], "must_avoid": [], "reference": "",
        }
        script = {
            "title": "测试片", "logline": "", "tone": "纪实",
            "target_audience": "", "hook": "",
            "narration_full": "开场白。中段一。中段二。转折句。",
            "key_subjects": [],
            "acts": [
                {"name": "起", "time_range": "0-4s", "content": "开场的一段文字。"},
                {"name": "承", "time_range": "4-8s", "content": "中段的一段文字。"},
                {"name": "转", "time_range": "8-12s", "content": "转折的一段文字。"},
                {"name": "合", "time_range": "12-16s", "content": "收尾的一段文字。"},
            ],
        }

        def mk(i: int, act: str, start: float, visual: str, cam: str) -> dict:
            return {
                "id": i, "act": act, "duration": 4.0, "start": start,
                "narration": f"台词{i}", "visual": visual, "camera": cam,
                "prompt_image": "a", "prompt_video": "b", "subject_refs": [],
                "workflow_id": "wf", "acceptance": "清晰",
            }

        # 4 镜：起1 / 承2 / 转1，"合"没有镜头——真实拆解里很常见
        shots = [
            mk(1, "起", 0.0, "开场画面", "固定机位，全景"),
            mk(2, "承", 4.0, "中段画面一", "缓慢推镜，中景"),
            mk(3, "承", 8.0, "中段画面二", "固定机位，特写"),
            mk(4, "转", 12.0, "转折画面", "横向摇移，中景"),
        ]
        for name, payload in (
            ("brief.json", brief), ("script.json", script),
            ("storyboard.json", {"project": {"title": "测试片", "total_duration": 16},
                                 "subjects": [], "shots": shots}),
            ("meta.json", {"edited": {"script": False, "shots": [1, 4]}}),
        ):
            (plan_dir / name).write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )

        lib = PromptLibrary.load()
        creative = CreativeLayer(lib, None)
        ctx = RunContext(run_id="replan-test", root=root, project_dir=root,
                         settings=Settings(values={}), live=False,
                         dry_run=True, verbose=False)
        pipe = Pipeline(Settings(values={}), lib, ctx,
                        RunLog(root / "run.log", verbose=False),
                        creative, None, avg_shot_len=4.0)

        # "承"从 2 镜变 3 镜——这才是会触发顺移的情形
        def fake(brief_, script_, act_name, act_text, target_seconds, **kwargs):
            return [
                Shot(id=i + 1, act=act_name,
                     duration=round(target_seconds / 3, 3),
                     narration=f"新台词{i + 1}", visual=f"新画面{i + 1}",
                     camera="固定机位，特写")
                for i in range(3)
            ]

        real_make = creative.make_act_shots
        creative.make_act_shots = fake  # type: ignore[method-assign]
        result = pipe.replan_act(plan_dir, "承")
        after = result["storyboard"]["shots"]
        id_map = {int(k): int(v) for k, v in result["id_map"].items()}

        check("重拆后镜数 4 → 5", len(after) == 5, str(len(after)))
        check("重拆不改全片总时长", abs(result["total_duration"] - 16.0) < 0.02,
              str(result["total_duration"]))
        check("只有被点名的场变了", len([s for s in after if s["act"] == "承"]) == 3)
        check("后面的场被顺移到镜 5",
              next(s["id"] for s in after if s["act"] == "转") == 5,
              str([(s["id"], s["act"]) for s in after]))
        check("顺移的镜头内容没被改",
              next(s["visual"] for s in after if s["act"] == "转") == "转折画面")
        check("id_map 保住没动的镜 1", id_map.get(1) == 1, str(id_map))
        check("id_map 把镜 4 映射到 5", id_map.get(4) == 5, str(id_map))
        check("被换掉的镜 2/3 没有映射", 2 not in id_map and 3 not in id_map, str(id_map))
        check("新镜头不产生伪映射", len(id_map) == 2, str(id_map))

        starts = [round(s["start"], 3) for s in after]
        contiguous = all(
            abs(starts[i] - (starts[i - 1] + round(after[i - 1]["duration"], 3))) < 0.01
            for i in range(1, len(after))
        )
        check("起点首尾相接", contiguous, str(starts))
        check("末镜收在总时长上",
              abs(round(after[-1]["start"] + after[-1]["duration"], 3) - 16.0) < 0.02)

        # 0 镜的场：时长该从剧本的 time_range 读，而不是拍脑袋给个平均镜长
        check("time_range 能读出这一场的时长", abs(pipe._act_span(script["acts"][3]) - 4.0) < 1e-6,
              str(pipe._act_span(script["acts"][3])))
        check("读不出 time_range 时返回 0", pipe._act_span({"name": "x"}) == 0.0)
        check("场次名不存在要报错",
              _raises(lambda: pipe.replan_act(plan_dir, "不存在的场")))

        # 本场文字必须真的进到新镜头里，不能退回"画面还在走。"这类兜底句。
        # 用真的离线实现（上面那个 fake 只负责造顺移场景，已经替换掉了方法）。
        long_text = "面从滚水里捞起，浇上一勺熬了整夜的羊肉汤，辣油在汤面上散开。"
        offline = real_make(
            _brief_from(brief), _script_from(script),
            "承", long_text, 8.0, avg_shot_len=4.0,
        )
        joined = "".join(s.narration for s in offline)
        check("长句被切成小句后能装进镜头", "面从滚水里捞起" in joined, joined[:40])
        check("没有退化成兜底句", "画面还在走" not in joined, joined[:40])
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _raises(fn) -> bool:
    try:
        fn()
    except Exception:  # noqa: BLE001
        return True
    return False


def _brief_from(data: dict):
    from pipeline.models import brief_from_dict

    return brief_from_dict(data)


def _script_from(data: dict):
    from pipeline.models import script_from_dict

    return script_from_dict(data)


def test_mv_entry() -> None:
    """MV 入口：token 不可穿越、请求字段齐全、短音频"拆得出来就拍得出去"。

    这一条守的是删掉旧 Tkinter 界面之后补回来的那条通路。三个真踩过的坑：

    1. **token 不是路径。** 收客户端给的字符串当路径，`audio="C:\\Windows\\win.ini"`
       会被 ffprobe 当输入打开——既泄露文件是否存在，也把 ffmpeg 变成了任意文件
       解析器。所以上传只回一个内容哈希，解析时正则 + 前缀两道。
    2. **`music_role` 拼错一个字母就静默变成 MV。** `MusicInput.is_bgm` 只认
       "bgm"，用 str 收参数的话 "bg" 会被当成 MV 出片，没有任何报错。
    3. **短音频拆得出来、拍不出去。** 4 秒的音频拆成 3 镜，每镜 1.333 秒——
       `build_timeline` 认为这是最优（总时长装不下 3 个 2 秒镜头，它主动压低了
       floor），而两处校验都写死 2 秒下限，于是这份拆解永远拍不了。
    """
    print("\n[16] MV 入口（音轨上传与时长权威）")
    from fastapi import HTTPException

    from pipeline.models import Storyboard
    from pipeline.pipeline import Pipeline
    from server.media import TOKEN_RE, resolve_audio
    from server.plan import _resolve_music, _shot_bounds
    from server.schemas import RunRequest

    # ---- 1. token 格式
    check("合法 token 通过正则", bool(TOKEN_RE.match("3f2a9c1d8e4b7a06.mp3")), "")
    for bad in ("../yiyan.db", "..\\x.mp3", "C:\\Windows\\win.ini", "a/b.mp3",
                "3f2a9c1d8e4b7a06.mp3.exe", ""):
        check(f"token {bad!r} 被拒", not TOKEN_RE.match(bad), "正则放过了")

    # ---- 2. 解析：格式对但文件不在要 422（不是 404，也不是抛 OSError）
    for token, label in (("0000000000000000.mp3", "不存在的音轨"),
                         ("../yiyan.db", "穿越 token")):
        try:
            resolve_audio(token)
            check(f"{label} 被拒", False, "没有抛异常")
        except HTTPException as exc:
            check(f"{label} 被拒", exc.status_code == 422, f"status={exc.status_code}")

    # ---- 3. 请求模型
    req = RunRequest(theme="x")
    check("RunRequest 有全部音乐字段",
          all(hasattr(req, f) for f in ("audio", "lyrics", "music_role",
                                        "bpm", "beat_offset", "auto_beats")), "")
    check("默认没有音轨（解说模式）", req.audio == "" and req.music_role == "song", "")
    try:
        RunRequest(theme="x", music_role="mv")
        check("music_role 只认 song / bgm", False, "mv 被接受了")
    except Exception:  # noqa: BLE001
        check("music_role 只认 song / bgm", True, "")

    # ---- 4. 没有音轨时不动时长；有歌词没音轨要拒绝
    path, duration, music = _resolve_music(RunRequest(theme="x", duration=15))
    check("没有音轨时不改时长、不写 meta",
          path == "" and duration == 15 and music == {}, f"{path} / {duration} / {music}")
    try:
        _resolve_music(RunRequest(theme="x", lyrics="啊"))
        check("填了歌词却没音轨要拒绝", False, "被放过了")
    except HTTPException as exc:
        check("填了歌词却没音轨要拒绝", exc.status_code == 422, f"status={exc.status_code}")

    # ---- 5. 短音频：4 秒 3 镜，两边校验都必须放行
    short = {"shots": [{"id": i, "duration": d}
                       for i, d in ((1, 1.333), (2, 1.333), (3, 1.334))]}
    bad = _shot_bounds(short)
    check("服务端不把 4 秒 3 镜判成越界", bad == [], str(bad))

    def board_of(durations: list[float]) -> Storyboard:
        return Storyboard(
            project={},
            shots=[Shot(id=i, act="a", duration=d, narration="", visual="", camera="")
                   for i, d in enumerate(durations, 1)],
        )

    # `_check_shots` 不碰 self，用 None 当实例调它比造一整个 Pipeline 划算
    try:
        Pipeline._check_shots(None, board_of([1.333, 1.333, 1.334]))
        check("管线侧也不把 4 秒 3 镜判成越界", True, "")
    except Exception as exc:  # noqa: BLE001
        check("管线侧也不把 4 秒 3 镜判成越界", False, str(exc)[:100])

    # 但手改到下限以下仍要拦住——松的是"总时长装不下"，不是整条规则
    hand = {"shots": [{"id": 1, "duration": 1.0}, {"id": 2, "duration": 1.5},
                      {"id": 3, "duration": 1.5}]}
    check("手改到 1.0 秒仍被判越界", bool(_shot_bounds(hand)), "被放过了")
    over = {"shots": [{"id": 1, "duration": 5}, {"id": 2, "duration": 5},
                      {"id": 3, "duration": 12}]}
    check("12 秒仍被判越界", bool(_shot_bounds(over)), "被放过了")

    # ---- 6. 前端契约（这几处断了，界面就会静默退回解说模式）
    api = (ROOT / "web" / "src" / "lib" / "api.ts").read_text(encoding="utf-8")
    check("前端有上传接口", "uploadAudio" in api and "/api/media/audio" in api, "")
    check("前端把 mode 映射成 music_role", "music_role" in api, "")
    page = (ROOT / "web" / "src" / "pages" / "CreatePage.tsx").read_text(encoding="utf-8")
    check("创作台有模式切换", "MODE_OPTIONS" in page, "")
    check("MV 模式下时长是只读的", "以音频为准" in page, "")
    plan_page = (ROOT / "web" / "src" / "pages" / "PlanPage.tsx").read_text(encoding="utf-8")
    check("拆解页显示音轨徽章", "音轨" in plan_page and "plan?.music" in plan_page, "")


def test_single_shot() -> None:
    """单镜重拍：只重拍一镜、其余复用旧片段、**绝不重算镜长**。

    这条守的是三件容易做错的事：

    1. **镜长不能动。** 单镜重拍若走 `_rebuild_timeline`，它会为了让累加时长
       命中目标值微调镜长；而其余镜头的片段是按旧镜长渲出来的，镜长一动，
       已有片段与新时间轴就对不上，成片从被改的那一镜起整体音画错位，
       而且不报错。所以走 `_sync_starts`：只重排 start。
    2. **不对其余镜头重跑终检。** 它们的帧缓存可能已被 clean_frames 清掉，
       重跑终检会把它们判成未达标再按「宁缺毋滥」剔除——用一次单镜重拍
       把整条片毁掉，日志上看着还挺合理。
    3. **代码绘制模式要拒。** 它是按整段分镜编译的，跑下去等于整段重画。
    """
    print("\n[17] 单镜重拍（只拍一镜，其余复用）")
    from types import SimpleNamespace

    from fastapi import HTTPException

    from app.engine import RunOptions
    from pipeline.models import Shot
    from pipeline.pipeline import Pipeline
    from server.plan import render_shot
    from server.schemas import RenderRequest

    # ---- 1. 选项与签名
    opts = RunOptions(theme="x")
    check("RunOptions 有 only_shot，默认 0（整片拍摄）",
          getattr(opts, "only_shot", None) == 0, str(getattr(opts, "only_shot", "缺失")))
    import inspect
    sig = inspect.signature(Pipeline.render_from_plan)
    check("render_from_plan 收 only_shot", "only_shot" in sig.parameters, str(sig))

    # ---- 2. _sync_starts 只动 start，不动 duration
    shots = [Shot(id=1, act="A", duration=3.333, narration="", visual="", camera=""),
             Shot(id=2, act="A", duration=4.0, narration="", visual="", camera=""),
             Shot(id=3, act="A", duration=2.5, narration="", visual="", camera="")]
    board = SimpleNamespace(shots=shots, project={}, total_duration=0.0)
    before = [s.duration for s in shots]
    # 造个最小替身当 self：这两个方法只用 ctx.notes / log.event / only_shot。
    # 真造一个 Pipeline 要读 prompt 库、建 provider 链，为了测一个纯计算不值当。
    fake = SimpleNamespace(
        only_shot=1,
        ctx=SimpleNamespace(notes=[]),
        log=SimpleNamespace(event=lambda *a, **k: None),
        progress=lambda *a, **k: None,
        # _check_narration 在 MV 模式下才查字数节奏，这里按解说模式走
        music=SimpleNamespace(enabled=False, is_bgm=False),
    )
    # 替身得带上真实的方法，否则 `self._check_shots(...)` 找不到——这里要测的
    # 恰恰是"重排 start 之后那两道校验还能过"，不能把它们换成空壳。
    fake._check_shots = Pipeline._check_shots.__get__(fake)
    fake._check_narration = Pipeline._check_narration.__get__(fake)
    try:
        Pipeline._sync_starts(fake, board)
        ok = True
        err = ""
    except Exception as exc:  # noqa: BLE001
        ok, err = False, str(exc)[:120]
    check("_sync_starts 不抛异常", ok, err)
    check("镜长一个都没被改", [s.duration for s in shots] == before,
          f"{before} → {[s.duration for s in shots]}")
    check("start 按累加重排", [s.start for s in shots] == [0.0, 3.333, 7.333],
          str([s.start for s in shots]))
    check("总时长按累加回写", abs(board.project["total_duration"] - 9.833) < 1e-6,
          str(board.project.get("total_duration")))

    # ---- 3. 镜号不存在要明确报错（不是静默拍整片）
    ghost = SimpleNamespace(**{**fake.__dict__, "only_shot": 99})
    try:
        Pipeline._render_single_shot(ghost, None, None,
                                     SimpleNamespace(shots=shots, project={}))
        check("镜号不存在时报错", False, "没有抛异常")
    except RuntimeError as exc:
        check("镜号不存在时报错", "没有镜 99" in str(exc), str(exc)[:80])
    except Exception as exc:  # noqa: BLE001
        check("镜号不存在时报错", False, f"抛错了类型：{type(exc).__name__}: {exc}"[:100])

    # ---- 4. 代码绘制模式在起任务之前就被拒
    try:
        render_shot("20260101-000000-x", 1, RenderRequest(painted=True), None)
        check("代码绘制模式拒绝单镜重拍", False, "没有抛异常")
    except HTTPException as exc:
        check("代码绘制模式拒绝单镜重拍", exc.status_code == 422, f"status={exc.status_code}")

    # ---- 5. 路由与前端契约
    from server.app import app
    paths = app.openapi()["paths"]
    check("有单镜重拍接口", "/api/plan/{run_id}/shots/{shot_id}/render" in paths,
          str([p for p in paths if "shots" in p]))
    api = (ROOT / "web" / "src" / "lib" / "api.ts").read_text(encoding="utf-8")
    check("前端有 renderShot", "export async function renderShot" in api, "")
    hook = (ROOT / "web" / "src" / "hooks" / "useRun.ts").read_text(encoding="utf-8")
    check("useRun 有 startShot", "startShot" in hook, "")
    page = (ROOT / "web" / "src" / "pages" / "PlanPage.tsx").read_text(encoding="utf-8")
    check("拆解页有「重拍这一镜」按钮", "重拍这一镜" in page, "")
    app_tsx = (ROOT / "web" / "src" / "App.tsx").read_text(encoding="utf-8")
    check("App 把 onReshoot 接上了", "onReshoot" in app_tsx and "startShot" in app_tsx, "")



def test_timeline_edit() -> None:
    """时间轴编辑：改镜长、挪顺序之后只重合成，一个像素都不重画。

    这条路上有三个必须成立的点，任何一个破了都会"看着正常但成片是错的"：

    1. **对齐片段**。concat 只认文件本身的长度，不会照 `start/duration` 去裁。
       片段按旧镜长渲出来、时间轴改成新镜长却不 fit，成片从被改那一镜起
       音画整体错位——而且不报错。
    2. **按新顺序传片段**。`compose` 按给定顺序拼，重排就是把列表换个次序。
    3. **不重算镜长**。用户是明确改了某镜的秒数，再按目标总时长去微调，
       等于把他的改动改回去。
    """
    import json
    import shutil

    from fastapi import HTTPException

    from app.engine import RunOptions
    from pipeline.compose import fit_clip
    from pipeline.pipeline import Pipeline
    from server.plan import recompose_plan, reorder_shots
    from server.schemas import ReorderRequest, RenderRequest

    # ---- 1. fit_clip：能裁能补，且编码参数与 make_clip 一致 ----
    check("compose 有 fit_clip", callable(fit_clip), "")
    compose_src = (ROOT / "pipeline" / "compose.py").read_text(encoding="utf-8")
    fit_src = compose_src.split("def fit_clip", 1)[-1].split("\ndef ", 1)[0]
    check("fit_clip 短片段用 tpad 冻结末帧", "tpad=stop_mode=clone" in fit_src, "")
    check("fit_clip 长片段裁尾（-t 目标时长）", '"-t", f"{duration:.3f}"' in fit_src, "")
    # concat_clips 走 stream copy，参数不一致会直接拼不上——这不是洁癖
    for flag, why in [
        ('"-c:v", "libx264"', "编码器"),
        ('"-preset", "medium"', "preset"),
        ('"-crf", "20"', "crf"),
        ('"-pix_fmt", "yuv420p"', "像素格式"),
        ('"-r", str(FPS)', "帧率"),
    ]:
        check(f"fit_clip 与 make_clip 的{why}一致（{flag}）", flag in fit_src, "")
    check("fit_clip 长度差在一帧内时原样返回（不白重编码）",
          "<= 1.0 / FPS" in fit_src, "")

    # ---- 2. 管线：重合成不重算镜长 ----
    check("RunOptions 有 recompose 且默认关闭",
          getattr(RunOptions(theme="x"), "recompose", None) is False, "")
    check("Pipeline 有 recompose_from_plan", hasattr(Pipeline, "recompose_from_plan"), "")
    check("Pipeline 有 _recompose", hasattr(Pipeline, "_recompose"), "")
    pipe_src = (ROOT / "pipeline" / "pipeline.py").read_text(encoding="utf-8")
    recompose_src = pipe_src.split("def recompose_from_plan", 1)[-1].split("\n    def ", 1)[0]
    check("recompose_from_plan 走 _sync_starts（不重算镜长）",
          "_sync_starts(board)" in recompose_src, "")
    # 注释里提到 `_rebuild_timeline` 不算，要看有没有**调用**它
    check("recompose_from_plan 不调 _rebuild_timeline",
          "self._rebuild_timeline(" not in recompose_src, "")
    body_src = pipe_src.split("def _recompose", 1)[-1].split("\n    def ", 1)[0]
    check("_recompose 用 fit_clip 对齐片段", "fit_clip(" in body_src, "")
    check("_recompose 把对齐结果写到 clips_fit（不覆盖源片段）",
          'work_dir / "clips_fit"' in body_src, "")
    check("_recompose 按 board.shots 顺序收集片段（重排即改列表次序）",
          "for shot in board.shots:" in body_src, "")
    check("重合成复用配音（reuse_voice=True）", "reuse_voice=True" in body_src, "")
    vo_src = pipe_src.split("def _voiceover", 1)[-1].split("\n    def ", 1)[0]
    check("_voiceover 支持 reuse", "reuse: bool = False" in vo_src, "")
    check("复用配音仍重新 fit（镜长变了要重对齐）",
          "fit_audio(raw, fitted, shot.duration)" in vo_src, "")

    # ---- 3. 接口：重合成 / 重排 ----
    check("有 POST /plan/{run_id}/recompose", hasattr(recompose_plan, "__name__"), "")
    check("有 POST /plan/{run_id}/shots/reorder", hasattr(reorder_shots, "__name__"), "")
    try:
        recompose_plan("x", RenderRequest(painted=True), None)
        check("代码绘制模式拒绝只重合成", False, "没有抛 HTTPException")
    except HTTPException as exc:
        check("代码绘制模式拒绝只重合成（整段视频没有逐镜片段）",
              exc.status_code == 422 and "重拼" in str(exc.detail), str(exc.detail))
    try:
        ReorderRequest(order=[])
        check("ReorderRequest 拒绝空顺序", False, "空列表没被拦")
    except Exception:  # noqa: BLE001  pydantic ValidationError
        check("ReorderRequest 拒绝空顺序", True, "")

    # ---- 4. 真跑一次重排：非置换必须拒，置换要真的改顺序 ----
    runs = ROOT / "runs"
    probe = runs / "_selftest_timeline"
    try:
        (probe / "plan").mkdir(parents=True, exist_ok=True)
        (probe / "plan" / "storyboard.json").write_text(json.dumps({
            "project": {"total_duration": 9.0},
            "subjects": [],
            "shots": [
                {"id": 1, "act": "开场", "duration": 3.0, "narration": "一",
                 "visual": "a", "camera": "固定", "start": 0.0},
                {"id": 2, "act": "开场", "duration": 3.0, "narration": "二",
                 "visual": "b", "camera": "固定", "start": 3.0},
                {"id": 3, "act": "收束", "duration": 3.0, "narration": "三",
                 "visual": "c", "camera": "固定", "start": 6.0},
            ],
        }, ensure_ascii=False), encoding="utf-8")
        (probe / "plan" / "meta.json").write_text("{}", encoding="utf-8")

        try:
            reorder_shots("_selftest_timeline", ReorderRequest(order=[2, 1]), None)
            check("重排拒绝非完整置换（防悄悄少镜头）", False, "缺项没被拦")
        except HTTPException as exc:
            check("重排拒绝非完整置换（防悄悄少镜头）",
                  exc.status_code == 422 and "排列" in str(exc.detail), str(exc.detail))

        res = reorder_shots("_selftest_timeline", ReorderRequest(order=[3, 1, 2]), None)
        got = [int(s["id"]) for s in res["shots"]]
        starts = [float(s["start"]) for s in res["shots"]]
        check("重排后顺序真的是 3→1→2", got == [3, 1, 2], str(got))
        check("重排后 start 按新顺序重算", starts == [0.0, 3.0, 6.0], str(starts))
        check("重排不改镜号、不改总时长",
              sorted(got) == [1, 2, 3] and abs(res["total_duration"] - 9.0) < 1e-6,
              f"{got} / {res['total_duration']}")

        # 没拍过（没有 rendered 标记）时，重合成必须当场拒绝
        try:
            recompose_plan("_selftest_timeline", RenderRequest(), None)
            check("没拍过就拒绝只重合成", False, "没有抛 HTTPException")
        except HTTPException as exc:
            check("没拍过就拒绝只重合成（没有片段可重拼）",
                  exc.status_code == 422 and "还没拍过" in str(exc.detail), str(exc.detail))

        # 标成拍过但没有片段 → 必须点名缺哪几镜，而不是起一条任务再失败
        (probe / "plan" / "meta.json").write_text(
            json.dumps({"rendered": True}), encoding="utf-8")
        try:
            recompose_plan("_selftest_timeline", RenderRequest(), None)
            check("片段缺失时点名缺哪几镜", False, "没有抛 HTTPException")
        except HTTPException as exc:
            # 报的是**重排之后**的顺序（3→1→2）：缺片是按分镜表的次序点名，
            # 不是按镜号大小排序——用户看到的就是列表里那个先后。
            check("片段缺失时点名缺哪几镜（不是起任务后才失败）",
                  exc.status_code == 422 and "[3, 1, 2]" in str(exc.detail), str(exc.detail))
    finally:
        shutil.rmtree(probe, ignore_errors=True)

    # ---- 5. 前端契约 ----
    api = (ROOT / "web" / "src" / "lib" / "api.ts").read_text(encoding="utf-8")
    check("前端有 recomposePlan", "export async function recomposePlan" in api, "")
    check("前端有 reorderShots", "export async function reorderShots" in api, "")
    hook = (ROOT / "web" / "src" / "hooks" / "useRun.ts").read_text(encoding="utf-8")
    check("useRun 有 startRecompose", "startRecompose" in hook, "")
    page = (ROOT / "web" / "src" / "pages" / "PlanPage.tsx").read_text(encoding="utf-8")
    check("拆解页有「只重合成」按钮", "只重合成" in page, "")
    check("拆解页有上移/下移", "上移" in page and "下移" in page, "")
    check("拆解页把 onRecompose 传下去了", "onRecompose" in page, "")
    app_tsx = (ROOT / "web" / "src" / "App.tsx").read_text(encoding="utf-8")
    check("App 把 onRecompose 接上了",
          "onRecompose" in app_tsx and "startRecompose" in app_tsx, "")


def main() -> int:
    print("=== 一言片场 · 自检 ===")
    test_sanitize()
    test_net_proxy_bypass()
    test_launchers()
    test_narration_quality()
    test_prompts()
    test_decisions()
    test_timeline()
    test_music_input()
    test_music_mix()
    test_beats()
    test_lyrics()
    test_portrait()
    test_generated_js_syntax()
    test_render_timeout()
    test_narration_pacing()
    test_comfyui_workflow()
    test_settings_precedence()
    test_resource_dir()
    test_pipeline_controls()
    test_theme_tokens()
    test_ffmpeg_env_guard()
    test_llm_config_robustness()
    test_painted_backend()
    test_startup_latency_and_quality()
    test_packaging_exclusions()
    test_offline_end_to_end()
    test_replan_act()
    test_mv_entry()
    test_single_shot()
    test_timeline_edit()
    print(f"\n结果：{len(PASSED)} 项通过，{len(FAILED)} 项失败")
    if FAILED:
        print("失败项：")
        for name in FAILED:
            print("  - " + name)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
