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

    # I3-BE-02 字幕设置贯通绘制版：占位帧版认的模式/偏移/人工轨，
    # 水彩版必须一模一样，否则「不要字幕」在水彩版里照样出字。
    _ly = [{"start": 1.0, "end": 2.0, "text": "一句"}]
    _manual = [{"start": 5.0, "end": 6.5, "text": "人工"}]
    check("subtitle_plan：none 不烧字", pb.subtitle_plan("none", _ly, None) == (False, False, False, _ly),
          str(pb.subtitle_plan("none", _ly, None)))
    check("subtitle_plan：sidecar 画面也不烧字（字幕交给 SRT）",
          pb.subtitle_plan("sidecar", _ly, None) == (False, False, False, _ly), "")
    check("subtitle_plan：burn + 歌词 → 逐字卡拉OK，不再画整句字幕",
          pb.subtitle_plan("burn", _ly, None) == (True, False, True, _ly), "")
    check("subtitle_plan：burn 无歌词 → 整句字幕",
          pb.subtitle_plan("burn", None, None) == (True, True, False, None), "")
    check("subtitle_plan：人工轨优先于歌词",
          pb.subtitle_plan("burn", _ly, _manual) == (True, False, True, _manual), "")
    check("subtitle_plan：人工轨给空列表 = 明确不要字幕（连镜头字幕也不烧）",
          pb.subtitle_plan("burn", _ly, []) == (True, False, False, []), "")

    _seen: dict[str, Any] = {}

    def _capture(shots, out_dir, **kw):
        _seen.update(kw)
        return {"ok": False, "error": "测试用：只取参数不真渲", "has_audio": False}

    pb.render_shots = _capture
    try:
        pb.apply_painted(
            {"storyboard": {"shots": [{"duration": 3, "narration": "x"}]},
             "compose": {"video": "old.mp4"},
             "lyrics": _ly,
             "subtitle": {"mode": "sidecar", "offset": 0.4, "lines": _manual}},
            log=lambda level, msg: None)
    finally:
        pb.render_shots = _orig_render
    check("apply_painted 把字幕模式透传给绘制后端",
          _seen.get("subtitle_mode") == "sidecar", str(_seen.get("subtitle_mode")))
    check("apply_painted 把字幕偏移透传给绘制后端",
          _seen.get("subtitle_offset") == 0.4, str(_seen.get("subtitle_offset")))
    check("apply_painted 把人工字幕轨透传给绘制后端",
          _seen.get("subtitle_lines") == _manual, str(_seen.get("subtitle_lines")))

    _shots = [{"duration": 3.0, "narration": "台词", "visual": "画面"}]
    _scene_on = pb.build_scene_js(_shots, captions=True, subtitle_offset=0.5)
    _scene_off = pb.build_scene_js(_shots, captions=False)
    check("场景 JS 下发字幕偏移常量", "const SUB_OFFSET = 0.5;" in _scene_on, "")
    check("整句字幕按偏移后的本地时间判断", "lt - SUB_OFFSET" in _scene_on, "")
    check("关掉字幕时镜头不带字幕文案", "const _cap = null;" in _scene_off,
          "sidecar/none 模式下画面不该烧字")
    _ly_js = pb.build_lyrics_js(_ly, offset=0.5)
    check("逐字歌词同样按偏移平移（否则画面与 SRT 差一个固定量）",
          "[1.5, 2.5" in _ly_js, _ly_js)
    check("平移到片头之前的歌词行被丢掉",
          "window.KARAOKE_LY = [\n\n];" in pb.build_lyrics_js(_ly, offset=-3.0), "")
    check("字幕偏移变化会让帧缓存失效（不复用上一版画面）",
          pb.frames_cache_key(_scene_on, "cfg", 24)
          != pb.frames_cache_key(pb.build_scene_js(_shots, captions=True), "cfg", 24), "")
    check("字幕模式切换会让帧缓存失效",
          pb.frames_cache_key(_scene_on, "cfg", 24) != pb.frames_cache_key(_scene_off, "cfg", 24), "")
    check("人工字幕轨变化会让帧缓存失效",
          pb.frames_cache_key("scene", "cfg", 24, extra=pb.build_lyrics_js(_manual))
          != pb.frames_cache_key("scene", "cfg", 24, extra=pb.build_lyrics_js(_ly)), "")

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



def test_reuse_roundtrip() -> None:
    """真跑一遍闭环：整片拍摄 → 改时间轴只重合成 → 单镜重拍。

    迭代2 把选中图摘要纳入片段指纹，而从磁盘读回分镜会清空运行期字段。
    静态检查看不出这个矛盾——只有真拍一次、再读回来复用，才会暴露
    "每一镜都被判过期、时间轴编辑与单镜重拍全部失效"。这条用例补的就是那个闭环。
    """
    print("\n[17b] 复用闭环（拍摄 → 只重合成 → 单镜重拍）")
    import shutil

    from pipeline.config import Settings, existing_run_context
    from pipeline.decide import RuleDecider as _RuleDecider
    from pipeline.llm import CreativeLayer
    from pipeline.pipeline import Pipeline
    from pipeline.prompts import PromptLibrary
    from pipeline.runlog import RunLog

    lib = PromptLibrary.load()
    root = ROOT / "runs" / "reuse-roundtrip-smoke"
    shutil.rmtree(root, ignore_errors=True)
    plan = root / "plan"
    plan.mkdir(parents=True)
    (plan / "brief.json").write_text(json.dumps(
        {"theme": "复用闭环", "total_duration": 4, "aspect_ratio": "16:9", "style": "纪实"},
        ensure_ascii=False), encoding="utf-8")
    (plan / "script.json").write_text(json.dumps({
        "title": "复用闭环", "logline": "", "tone": "纪实",
        "target_audience": "", "hook": "",
        "narration_full": "第一句。第二句。", "key_subjects": [], "acts": [],
    }, ensure_ascii=False), encoding="utf-8")
    board_json = {
        "project": {"total_duration": 4.0, "title": "复用闭环"},
        "subjects": [],
        "shots": [
            {"id": 1, "act": "开场", "duration": 2.0, "start": 0.0,
             "visual": "海边日出", "camera": "固定", "narration": "第一句"},
            {"id": 2, "act": "收束", "duration": 2.0, "start": 2.0,
             "visual": "海浪", "camera": "固定", "narration": "第二句"},
        ],
    }
    sb_path = plan / "storyboard.json"
    sb_path.write_text(json.dumps(board_json, ensure_ascii=False), encoding="utf-8")

    def new_pipe() -> Pipeline:
        ctx = existing_run_context("reuse-roundtrip-smoke", Settings(values={}))
        ctx.prepare()
        return Pipeline(
            Settings(values={}), lib, ctx, RunLog(ctx.log_path),
            CreativeLayer(lib, None), _RuleDecider(lib), preview=True,
        )

    try:
        first = new_pipe().render_from_plan(plan)
        check("整片拍摄产出成片", Path(first["compose"]["video"]).is_file(),
              str(first.get("summary")))
        out_board = json.loads((root / "output" / "storyboard.json").read_text(encoding="utf-8"))
        check("拍摄后每镜都留下选中图摘要",
              all(s.get("image_sha256") for s in out_board["shots"]),
              str([s.get("image_sha256", "")[:8] for s in out_board["shots"]]))
        check("拍摄后每镜都留下片段指纹",
              all((root / "work" / "clips" / f"shot{s['id']:02d}.key.json").is_file()
                  for s in out_board["shots"]), "")

        # 时间轴编辑：镜1 由 2.0s 改成 2.5s，画面内容一个字都没动。
        edited = json.loads(sb_path.read_text(encoding="utf-8"))
        edited["shots"][0]["duration"] = 2.5
        edited["shots"][1]["start"] = 2.5
        edited["project"]["total_duration"] = 4.5
        sb_path.write_text(json.dumps(edited, ensure_ascii=False), encoding="utf-8")
        again = new_pipe().recompose_from_plan(plan)
        check("改时间轴后只重合成成功", Path(again["compose"]["video"]).is_file(),
              str(again.get("summary")))
        check("重合成按新时长出片", abs(float(again["compose"]["duration"]) - 4.5) < 0.4,
              str(again["compose"]["duration"]))
        recomposed_board = json.loads(
            (root / "output" / "storyboard.json").read_text(encoding="utf-8"))
        check("重合成不抹掉拍摄证据",
              all(s.get("image_sha256") for s in recomposed_board["shots"]), "")

        solo = new_pipe().render_from_plan(plan, only_shot=2)
        check("单镜重拍复用其余镜头片段", Path(solo["compose"]["video"]).is_file(),
              str(solo.get("summary")))

        # 画面真改过时必须拒绝：恢复证据不能把失效掩盖掉。
        changed = json.loads(sb_path.read_text(encoding="utf-8"))
        changed["shots"][0]["visual"] = "完全不同的画面"
        sb_path.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
        try:
            new_pipe().recompose_from_plan(plan)
            check("画面改过时拒绝只重合成", False, "被接受了")
        except RuntimeError as exc:
            check("画面改过时拒绝只重合成", "不一致" in str(exc), str(exc)[:90])
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_master_publish() -> None:
    """母版落盘：重渲期间旧成片必须一直可播，失败不能留半截文件。

    母版是历史详情与导出的唯一来源。直接往 ``output/final.mp4`` 上写，
    ffmpeg 会先把它截断——这段时间里所有读它的请求都拿到坏文件，中途崩溃
    还会让 manifest 把半截片当成成片。这条用例真跑 ffmpeg 验证原子替换。
    """
    print("\n[17c] 母版原子落盘与字幕模式收尾")
    import shutil
    import subprocess

    from pipeline import compose as cmod
    from pipeline.models import Shot, Storyboard
    from pipeline.video import FFMPEG, VideoError

    tmp = ROOT / "work" / "_master_publish"
    shutil.rmtree(tmp, ignore_errors=True)
    out_dir = tmp / "output"
    work_dir = tmp / "work"
    out_dir.mkdir(parents=True)
    work_dir.mkdir(parents=True)
    try:
        clips = []
        for i, color in enumerate(("red", "blue"), start=1):
            p = work_dir / f"clip{i}.mp4"
            subprocess.run([
                FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", f"color=c={color}:s=160x90:d=1:r=25",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(p),
            ], check=True, capture_output=True)
            clips.append(p)
        board = Storyboard(project={"total_duration": 2.0}, subjects=[], shots=[
            Shot(id=1, act="A", duration=1.0, narration="第一句", visual="红", camera="固定"),
            Shot(id=2, act="A", duration=1.0, narration="第二句", visual="蓝", camera="固定"),
        ])
        board.assign_timeline()

        first = cmod.compose(board, clips, out_dir, work_dir, subtitle_mode="burn")
        master = out_dir / "final.mp4"
        check("首次合成产出母版", master.is_file() and first.video == str(master), first.video)
        check("烧录模式产出 SRT", (out_dir / "subtitles.srt").is_file(), "")
        good_bytes = master.read_bytes()
        check("合成结束不留临时母版", not (out_dir / ".final.pending.mp4").exists(), "")
        check("分镜证据落盘且不留临时文件",
              (out_dir / "storyboard.json").is_file()
              and not (out_dir / ".storyboard.pending.json").exists(), "")

        # 第二次合成中途失败：mux 已经写了半截数据才炸。
        real_mux = cmod.mux_audio

        def broken_mux(video, audio, out_path, duration):
            Path(out_path).write_bytes(b"\x00\x01partial-not-a-video")
            raise VideoError("模拟合成中途崩溃")

        cmod.mux_audio = broken_mux
        try:
            cmod.compose(board, clips, out_dir, work_dir, subtitle_mode="burn")
            check("合成失败如实抛出", False, "被吞掉了")
        except VideoError:
            check("合成失败如实抛出", True, "")
        finally:
            cmod.mux_audio = real_mux
        check("失败后旧母版字节完全不变（历史详情与导出仍可播）",
              master.read_bytes() == good_bytes, f"{master.stat().st_size} bytes")
        check("失败后清掉半截临时文件（不会被当成成片）",
              not (out_dir / ".final.pending.mp4").exists(), "")

        # 成功重合成必须真的换掉母版，而不是留着旧片。
        second = cmod.compose(board, clips, out_dir, work_dir, subtitle_mode="burn")
        check("成功重合成替换母版", master.is_file() and second.video == str(master), "")
        check("重合成后不留临时母版", not (out_dir / ".final.pending.mp4").exists(), "")

        none_res = cmod.compose(board, clips, out_dir, work_dir, subtitle_mode="none")
        check("不要字幕时删掉上一版 SRT（历史页不再显示有字幕）",
              not (out_dir / "subtitles.srt").exists(), "")
        check("不要字幕时结果里字幕路径为空", none_res.subtitle == "", none_res.subtitle)
        side = cmod.compose(board, clips, out_dir, work_dir, subtitle_mode="sidecar")
        check("外挂字幕模式仍产出 SRT", Path(side.subtitle).is_file(), side.subtitle)

        # 绘制版走的是另一条编码通路，同样不能直接写母版。
        painted_src = (ROOT / "pipeline" / "painted.py").read_text(encoding="utf-8")
        check("绘制版编码先写临时文件再原子替换",
              '.painted.pending.mp4' in painted_src
              and "pending.replace(video)" in painted_src, "")
        check("绘制版编码失败时清掉临时文件",
              "pending.unlink(missing_ok=True)" in painted_src, "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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


def test_access_and_versions() -> None:
    """权限归属、故障拒绝、版本冲突与渲染配置继承。

    这一组守的是审计里最危险的几个方向：

    1. **归属查询失败不能变成"公开"**。以前 `_owner_of` 吞掉所有数据库异常并
       返回 None，`_can_view` 把 None 当成公共项目——数据库一抖，私有项目全公开。
    2. **并发编辑不能丢修改**。两个请求各自读整份 JSON 再覆盖，后写的赢；
       项目锁 + 版本号把"隐形覆盖"变成显式的 409。
    3. **先拆后拍不能换配置**。`RenderRequest` 以前默认 live=True，拆解页只传
       run_id，于是离线拆解被悄悄拍成真实模型。
    4. **同秒同主题不能撞 ID**。时间戳 + 主题前 20 字在并发下会共用目录。
    """
    print("\n[18] 权限归属、版本冲突与渲染继承")
    import inspect
    import shutil
    import tempfile
    from types import SimpleNamespace

    from fastapi import HTTPException

    from app.engine import RunOptions
    import pipeline.config as pconfig
    import server.app as sapp
    import server.auth as sauth
    import server.history as shist
    import server.jobs as sjobs
    import server.plan as splan
    from server.schemas import RenderRequest, ShotPatch

    tmp = Path(tempfile.mkdtemp(prefix="yiyan-sec-"))
    saved = {
        "auth_db": sauth.DB_PATH,
        "hist_runs": shist.RUNS,
        "hist_env": shist.ENV_PATH,
        "plan_root": splan.ROOT,
        "plan_registry": splan.REGISTRY,
        "app_registry": sapp.REGISTRY,
        "cfg_root": pconfig.ROOT,
    }
    try:
        sauth.DB_PATH = tmp / "work" / "yiyan.db"
        shist.RUNS = tmp / "runs"
        shist.RUNS.mkdir(parents=True, exist_ok=True)
        shist.ENV_PATH = tmp / ".env"
        splan.ROOT = tmp
        pconfig.ROOT = tmp
        sauth.init_db()
        import server.taskstore as stasks

        stasks.init_tasks()

        # ---- 1. 归属键与可见性 ----
        from server.auth import (
            OwnerConflict,
            OwnershipUnavailable,
            Principal,
            optional_principal,
            record_owner,
            run_owner_key,
        )

        owner = Principal("user", 1)
        record_owner("run-a", owner)
        check("归属键写成 u:<id>", run_owner_key("run-a") == "u:1", str(run_owner_key("run-a")))
        check("主人能看", shist._can_view("run-a", owner) is True, "")
        check("其他账号看不到", shist._can_view("run-a", Principal("user", 2)) is False, "")
        check("访客看不到", shist._can_view("run-a", Principal("guest", "a" * 32)) is False, "")
        check("匿名看不到", shist._can_view("run-a", None) is False, "")
        check("无归属旧项目仍按公共处理", shist._can_view("legacy-run", None) is True, "")
        try:
            record_owner("run-a", Principal("user", 2))
            check("归属不能被后来的创建覆盖", False, "没有抛 OwnerConflict")
        except OwnerConflict:
            check("归属不能被后来的创建覆盖", True, "")

        guest = optional_principal(None, "b" * 32)
        check("合法访客凭据解析成 g:<id>", guest is not None and guest.key == f"g:{'b' * 32}",
              str(guest))
        check("非法访客凭据被拒", optional_principal(None, "not-a-key") is None, "")

        # ---- 2. 数据库故障必须拒绝，而不是公开 ----
        sauth.DB_PATH = tmp / "missing-dir" / "yiyan.db"
        try:
            shist._can_view("run-a", None)
            check("归属查询故障时拒绝访问", False, "故障下仍返回了可见")
        except HTTPException as exc:
            check("归属查询故障时拒绝访问", exc.status_code == 503, f"status={exc.status_code}")
        try:
            record_owner("run-b", owner)
            check("归属写入故障可见", False, "没有抛异常")
        except OwnershipUnavailable:
            check("归属写入故障可见", True, "")
        sauth.DB_PATH = tmp / "work" / "yiyan.db"

        # ---- 3. 项目锁：并发修改都保留，版本号递增 ----
        rid = "20261115-120000-并发-abc123"
        plan_dir = tmp / "runs" / rid / "plan"
        plan_dir.mkdir(parents=True)
        board = {
            "project": {"total_duration": 6.0, "title": "并发"},
            "subjects": [],
            "shots": [
                {"id": 1, "act": "开场", "duration": 3.0, "start": 0.0,
                 "visual": "old-1", "camera": "固定", "narration": "一"},
                {"id": 2, "act": "开场", "duration": 3.0, "start": 3.0,
                 "visual": "old-2", "camera": "固定", "narration": "二"},
            ],
        }
        profile = {"live": False, "painted": True, "painted_fps": 12,
                   "painted_quality": "fast", "clean_frames": False}
        (plan_dir / "storyboard.json").write_text(
            json.dumps(board, ensure_ascii=False), encoding="utf-8")
        (plan_dir / "brief.json").write_text(
            json.dumps({"theme": "并发", "total_duration": 6, "aspect_ratio": "16:9"},
                       ensure_ascii=False), encoding="utf-8")
        (plan_dir / "script.json").write_text("{}", encoding="utf-8")
        (plan_dir / "meta.json").write_text(
            json.dumps({"revision": 0, "render_profile": profile}, ensure_ascii=False),
            encoding="utf-8")

        errors: list[str] = []

        def edit(shot_id: int, value: str) -> None:
            try:
                splan.patch_shot(rid, shot_id, ShotPatch(visual=value), None)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{shot_id}: {exc}")

        t1 = threading.Thread(target=edit, args=(1, "new-1"))
        t2 = threading.Thread(target=edit, args=(2, "new-2"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        saved_board = json.loads((plan_dir / "storyboard.json").read_text(encoding="utf-8"))
        visuals = {shot["id"]: shot["visual"] for shot in saved_board["shots"]}
        check("并发编辑不报错", errors == [], str(errors))
        check("两个镜头的修改都保留",
              visuals.get(1) == "new-1" and visuals.get(2) == "new-2", str(visuals))
        saved_meta = json.loads((plan_dir / "meta.json").read_text(encoding="utf-8"))
        check("版本号按修改次数递增", saved_meta.get("revision") == 2, str(saved_meta.get("revision")))

        try:
            splan.patch_shot(rid, 1, ShotPatch(visual="stale", expected_revision=0), None)
            check("过期版本提交被 409 挡住", False, "没有抛异常")
        except HTTPException as exc:
            check("过期版本提交被 409 挡住", exc.status_code == 409, f"status={exc.status_code}")

        # ---- 4. 拍摄继承项目配置，并冻结快照 ----
        captured: list[RunOptions] = []

        class FakeRegistry:
            def create(self, theme, opts, principal=None, kind="create"):
                captured.append(opts)
                return SimpleNamespace(job_id=f"job-{len(captured)}")

        splan.REGISTRY = FakeRegistry()
        result = splan.render_plan(rid, RenderRequest(), None)
        check("拍摄入口返回版本号", result.get("revision") == 2, str(result.get("revision")))
        check("空请求不被默认值改成真实模型",
              captured and captured[0].live is False, str(captured[0].live if captured else None))
        check("代码绘制配置被继承",
              captured and captured[0].painted is True and captured[0].painted_fps == 12,
              str((captured[0].painted, captured[0].painted_fps) if captured else None))
        check("拍的是冻结快照而不是活动目录",
              "snapshots" in captured[0].plan_dir, captured[0].plan_dir if captured else "")
        check("快照带 run_id，成片仍落在原项目",
              captured[0].run_id == rid, captured[0].run_id if captured else "")
        snap = Path(captured[0].plan_dir)
        check("快照内含分镜与配置",
              (snap / "storyboard.json").is_file() and (snap / "render_profile.json").is_file(), "")

        # 显式覆盖要生效并回写项目
        splan.render_plan(rid, RenderRequest(live=True), None)
        check("显式覆盖生效", captured[-1].live is True, str(captured[-1].live))
        meta_after = json.loads((plan_dir / "meta.json").read_text(encoding="utf-8"))
        check("显式覆盖回写项目配置",
              (meta_after.get("render_profile") or {}).get("live") is True,
              str(meta_after.get("render_profile")))

        # ---- 5. 配置写入：白名单、需要登录、显式清除、原子写 ----
        try:
            shist.save_env(shist.EnvUpdate(values={"AUDIT_UNLISTED_KEY": "x"}), user={"id": 1})
            check("未列入白名单的键被拒", False, "被接受了")
        except HTTPException as exc:
            check("未列入白名单的键被拒", exc.status_code == 422, f"status={exc.status_code}")
        try:
            shist.save_env(shist.EnvUpdate(values={"LLM_BASE_URL": "javascript:alert(1)"}),
                           user={"id": 1})
            check("配置值按类型校验", False, "非法 URL 被接受了")
        except HTTPException as exc:
            check("配置值按类型校验", exc.status_code == 422, f"status={exc.status_code}")
        out = shist.save_env(shist.EnvUpdate(values={"LLM_MODEL": "deepseek-chat"}), user={"id": 1})
        check("合法配置写入并返回状态", out.get("updated") == ["LLM_MODEL"]
              and out.get("configured", {}).get("LLM_MODEL") is True, str(out))
        check("写入没有遗留临时文件",
              not list(tmp.glob(".env-*.tmp")), str(list(tmp.glob(".env-*.tmp"))))

        shist.save_env(shist.EnvUpdate(values={"LLM_API_KEY": "sk-audit"}), user={"id": 1})
        removed = shist.save_env(shist.EnvUpdate(delete=["LLM_API_KEY"]), user={"id": 1})
        env_text = shist.ENV_PATH.read_text(encoding="utf-8")
        check("显式清除会移除整行",
              "LLM_API_KEY" not in env_text
              and removed.get("configured", {}).get("LLM_API_KEY") is False, env_text)
        check("清除不影响其他已保存项",
              "LLM_MODEL=deepseek-chat" in env_text, env_text)
        try:
            shist.save_env(shist.EnvUpdate(values={"LLM_API_KEY": "x"},
                                           delete=["LLM_API_KEY"]), user={"id": 1})
            check("同一键不能同时替换和清除", False, "被接受了")
        except HTTPException as exc:
            check("同一键不能同时替换和清除", exc.status_code == 422, f"status={exc.status_code}")
        dep = inspect.signature(shist.save_env).parameters["user"].default
        check("配置写入依赖登录", dep.dependency is shist.require_user, str(dep))

        # ---- 6. 任务接口按主体过滤 ----
        job = sjobs.Job("t", RunOptions(theme="t"), principal=Principal("guest", "c" * 32))
        job._on_start("run-job-owned")
        check("任务在生成前就登记归属（失败/取消也不会无主）",
              run_owner_key("run-job-owned") == f"g:{'c' * 32}",
              str(run_owner_key("run-job-owned")))
        check("任务对创建者可读", job.visible_to(Principal("guest", "c" * 32)) is True, "")
        check("任务对其他访客不可读", job.visible_to(Principal("guest", "d" * 32)) is False, "")
        check("任务对匿名不可读", job.visible_to(None) is False, "")
        check("任务快照带渲染配置且不泄露归属",
              "options" in job.snapshot() and "owner_key" not in job.snapshot(), "")
        sapp.REGISTRY = SimpleNamespace(get=lambda _jid: job)
        try:
            sapp.job_state(job.job_id, Principal("guest", "d" * 32))
            check("越权读取任务被 404 挡住", False, "没有抛异常")
        except HTTPException as exc:
            check("越权读取任务被 404 挡住", exc.status_code == 404, f"status={exc.status_code}")
        dep = inspect.signature(sapp.job_state).parameters["principal"].default
        check("任务详情依赖主体解析", dep.dependency is sauth.optional_principal, str(dep))

        # ---- 7. 同秒同主题的 ID 不再碰撞 ----
        id_a, dir_a = pconfig.allocate_run_id("同一主题")
        id_b, dir_b = pconfig.allocate_run_id("同一主题")
        check("同秒同主题得到不同 ID", id_a != id_b, f"{id_a} / {id_b}")
        check("两个目录都真实存在", dir_a.is_dir() and dir_b.is_dir(), "")

        # ---- 8. 前端契约（提示与队列） ----
        page = (ROOT / "web" / "src" / "pages" / "PlanPage.tsx").read_text(encoding="utf-8")
        check("拆解页有保存队列与版本", "chainRef" in page and "expected_revision" in page, "")
        check("拍摄前等待保存落地", "flushSaves" in page, "")
        check("拆解页显示实际拍摄配置", "profileLabel" in page, "")
        auth_ts = (ROOT / "web" / "src" / "lib" / "auth.ts").read_text(encoding="utf-8")
        check("前端生成访客凭据", "GUEST_KEY" in auth_ts and "X-Guest-Id" in auth_ts, "")
        sse_ts = (ROOT / "web" / "src" / "lib" / "sse.ts").read_text(encoding="utf-8")
        hook_ts = (ROOT / "web" / "src" / "hooks" / "useRun.ts").read_text(encoding="utf-8")
        check("事件流带认证头",
              "readSse(" in hook_ts and "authHeaders()" in hook_ts
              and "headers" in sse_ts, "")
    finally:
        sauth.DB_PATH = saved["auth_db"]
        shist.RUNS = saved["hist_runs"]
        shist.ENV_PATH = saved["hist_env"]
        splan.ROOT = saved["plan_root"]
        splan.REGISTRY = saved["plan_registry"]
        sapp.REGISTRY = saved["app_registry"]
        pconfig.ROOT = saved["cfg_root"]
        shutil.rmtree(tmp, ignore_errors=True)


def test_review_audio_sse() -> None:
    """质检依据、项目音轨资产与事件流恢复。

    三件事以前都"看起来正常"：
    1. 质检没读过图，两张不存在的图片得到同一段描述；
    2. 音轨只记 token，上传暂存清理后重拍必 422，尽管项目里已有副本；
    3. SSE 正常断流后直接 resolve，界面永远停在「生成中」。
    """
    print("\n[19] 质检依据、音轨资产与事件流恢复")
    import shutil
    import tempfile
    from types import SimpleNamespace

    from fastapi import HTTPException
    from PIL import Image, ImageDraw

    import server.plan as splan
    from app.engine import RunOptions
    from pipeline.decide import REVIEW_RULE_VERSION, RuleDecider
    from pipeline.images import inspect_image
    from pipeline.models import ProjectBrief, Shot
    from pipeline.pipeline import Pipeline
    from pipeline.config import Settings
    from server.schemas import RenderRequest

    tmp = Path(tempfile.mkdtemp(prefix="yiyan-review-"))
    old_root = splan.ROOT
    old_registry = splan.REGISTRY
    old_resolve = splan.resolve_audio
    try:
        splan.ROOT = tmp
        runs = tmp / "runs"
        runs.mkdir(parents=True)

        # ---- 1. 技术检查真的读图 ----
        missing = tmp / "no-such-frame.png"
        bad = inspect_image(missing)
        check("不存在的图片技术检查失败",
              bad.get("ok") is False and "读不到" in str(bad.get("reason")), str(bad))

        good_path = tmp / "good.png"
        image = Image.new("RGB", (160, 120), (30, 60, 90))
        draw = ImageDraw.Draw(image)
        for x in range(0, 160, 8):
            draw.line((x, 0, 160 - x, 119), fill=(220, 180, 90), width=2)
        image.save(good_path)
        good = inspect_image(good_path)
        check("正常图片通过技术检查", good.get("ok") is True, str(good))
        check("技术检查产出内容哈希", len(str(good.get("sha256") or "")) == 64, str(good.get("sha256")))

        black_path = tmp / "black.png"
        Image.new("RGB", (160, 120), (0, 0, 0)).save(black_path)
        check("纯黑图片被判技术检查失败", inspect_image(black_path).get("ok") is False, "")

        # ---- 2. 质检模式与硬失败 ----
        pipe = object.__new__(Pipeline)
        pipe.ctx = SimpleNamespace(live=False, notes=[])
        pipe.settings = Settings(values={})
        pipe.creative = SimpleNamespace(client=None)
        pipe.log = SimpleNamespace(error=lambda *a, **k: None)
        shot = Shot(id=1, act="a", duration=3, narration="", visual="v", camera="c")
        brief = ProjectBrief(theme="t")

        missing_desc = json.loads(pipe._describe(missing, shot, brief, 1))
        check("缺图时标注 technical_failed",
              missing_desc.get("review_mode") == "technical_failed"
              and missing_desc.get("technical", {}).get("ok") is False, str(missing_desc))

        sim_desc = json.loads(pipe._describe(good_path, shot, brief, 1))
        check("离线质检标注为模拟评审",
              sim_desc.get("review_mode") == "simulated"
              and sim_desc.get("visual_check") == "not_performed", str(sim_desc.get("review_mode")))
        check("描述里带真实技术指标",
              sim_desc.get("technical", {}).get("width") == 160
              and sim_desc.get("technical", {}).get("sha256") == good.get("sha256"), "")
        check("模拟评审会写入项目提示",
              any("模拟评审" in note for note in pipe.ctx.notes), str(pipe.ctx.notes))

        decider = RuleDecider()
        failed_review = decider.review_quality({
            "candidate_desc": json.dumps(missing_desc, ensure_ascii=False),
            "acceptance": "主体清晰",
            "attempt": 1,
        })
        check("技术失败直接判不合格",
              failed_review.get("verdict") == "reject"
              and failed_review.get("visual_check") == "failed", str(failed_review))
        sim_review = decider.review_quality({
            "candidate_desc": json.dumps({**sim_desc, "defects": []}, ensure_ascii=False),
            "acceptance": "主体清晰",
            "attempt": 1,
        })
        check("模拟评审结果带规则版本",
              sim_review.get("review_mode") == "simulated"
              and sim_review.get("rule_version") == REVIEW_RULE_VERSION, str(sim_review))
        check("镜头对象保存评审依据",
              all(hasattr(shot, f) for f in
                  ("review_mode", "visual_check", "review_rule", "image_sha256")), "")
        pipe_src = (ROOT / "pipeline" / "pipeline.py").read_text(encoding="utf-8")
        check("技术检查是决策层之外的硬闸门",
              'score, verdict, defect = 1, "reject", "image_unreadable"' in pipe_src, "")

        # ---- 3. 项目音轨资产优先于上传 token ----
        rid = "20261115-120000-音轨-abc123"
        plan_dir = runs / rid / "plan"
        plan_dir.mkdir(parents=True)
        (plan_dir / "storyboard.json").write_text(json.dumps({
            "project": {"total_duration": 6.0},
            "subjects": [],
            "shots": [
                {"id": 1, "act": "唱", "duration": 3.0, "start": 0.0,
                 "visual": "a", "camera": "固定", "narration": ""},
                {"id": 2, "act": "唱", "duration": 3.0, "start": 3.0,
                 "visual": "b", "camera": "固定", "narration": ""},
            ],
        }, ensure_ascii=False), encoding="utf-8")
        (plan_dir / "brief.json").write_text(json.dumps({
            "theme": "音轨", "total_duration": 6, "aspect_ratio": "16:9",
        }, ensure_ascii=False), encoding="utf-8")
        (plan_dir / "script.json").write_text("{}", encoding="utf-8")
        saved_meta = {
            "revision": 0,
            "rendered": True,
            "render_profile": {"live": False, "painted": False, "painted_fps": 24,
                               "painted_quality": "medium", "clean_frames": False},
            "music": {"token": "0123456789abcdef.wav", "role": "song",
                      "lyrics": "第一句\n第二句", "bpm": 90, "beat_offset": 0.0,
                      "auto_beats": True, "asset": "assets/music.wav"},
        }
        (plan_dir / "meta.json").write_text(json.dumps(saved_meta, ensure_ascii=False),
                                            encoding="utf-8")
        assets = runs / rid / "assets"
        assets.mkdir(parents=True)
        (assets / "music.wav").write_bytes(b"RIFF-audit-copy")

        resolve_calls: list[str] = []
        def fake_resolve(token: str):
            resolve_calls.append(token)
            raise HTTPException(status_code=422, detail="音轨不存在或已被清理，请重新上传")

        captured: list[RunOptions] = []
        class FakeRegistry:
            def create(self, theme, opts, principal=None, kind="create"):
                captured.append(opts)
                return SimpleNamespace(job_id=f"job-{len(captured)}")

        splan.resolve_audio = fake_resolve
        splan.REGISTRY = FakeRegistry()
        splan.render_plan(rid, RenderRequest(), None)
        check("上传已清理仍能拍摄（用项目内音轨）",
              captured and captured[0].audio.endswith("music.wav")
              and Path(captured[0].audio).is_file(), captured[0].audio if captured else "")
        check("没有回落去解析上传 token", resolve_calls == [], str(resolve_calls))
        check("歌词/节拍/角色完整保留",
              captured[0].lyrics.startswith("第一句") and captured[0].bpm == 90
              and captured[0].music_role == "song",
              str((captured[0].lyrics, captured[0].bpm, captured[0].music_role)))

        (assets / "music.wav").unlink()
        try:
            splan.render_plan(rid, RenderRequest(), None)
            check("项目音轨丢失时才要求重新上传", False, "没有报错")
        except HTTPException as exc:
            check("项目音轨丢失时才要求重新上传",
                  exc.status_code == 422 and resolve_calls == ["0123456789abcdef.wav"],
                  f"status={exc.status_code} calls={resolve_calls}")

        # ---- 4. 前端：SSE 恢复契约 ----
        sse = (ROOT / "web" / "src" / "lib" / "sse.ts").read_text(encoding="utf-8")
        check("SSE 归一 CRLF", "\\r\\n" in sse, "")
        check("SSE 返回终态标记", "terminal" in sse and "'done'" in sse and "'error'" in sse, "")
        check("SSE 收到终态主动释放连接", "reader.cancel" in sse, "")
        hook = (ROOT / "web" / "src" / "hooks" / "useRun.ts").read_text(encoding="utf-8")
        check("EOF 后查询任务快照", "applySnapshot" in hook and "getJob(id)" in hook, "")
        check("任务不存在时停止重连", "status === 404" in hook, "")
        check("恢复连接显示独立状态", "正在恢复事件流" in hook, "")
    finally:
        splan.ROOT = old_root
        splan.REGISTRY = old_registry
        splan.resolve_audio = old_resolve
        shutil.rmtree(tmp, ignore_errors=True)


def test_settings_route_upload() -> None:
    """配置三态、路由容错、上传探测线程化。

    FE-04：空输入框曾被当成"不修改"，用户清空后旧密钥照旧生效；
    FE-05：`#plan/%` 在 `useState(parseLocation)` 里抛 URIError，整页白屏；
    BE-08：async 上传接口里同步跑 ffprobe，会堵住事件循环。
    """
    print("\n[20] 配置清除、路由容错与上传线程化")
    import shutil
    import tempfile

    import server.media as smedia

    # ---- FE-04 前端三态 ----
    page = (ROOT / "web" / "src" / "pages" / "SettingsPage.tsx").read_text(encoding="utf-8")
    check("设置页有显式清除动作", "deletes" in page and "清除" in page, "")
    check("设置页区分保留与替换", "留空表示保留" in page, "")
    api = (ROOT / "web" / "src" / "lib" / "api.ts").read_text(encoding="utf-8")
    check("保存接口区分 values 与 delete",
          "delete?: string[]" in api and "values?: Record<string, string>" in api, "")

    # ---- FE-05 路由与错误边界 ----
    app_src = (ROOT / "web" / "src" / "App.tsx").read_text(encoding="utf-8")
    check("路由使用安全解码", "function safeDecode" in app_src and "catch" in app_src, "")
    check("非法链接有返回入口",
          "route: 'invalid'" in app_src and "链接无效" in app_src, "")
    check("页面级错误边界",
          "class ErrorBoundary" in app_src and "getDerivedStateFromError" in app_src, "")

    # ---- BE-08 上传收尾线程化 ----
    media_src = (ROOT / "server" / "media.py").read_text(encoding="utf-8")
    check("上传探测在线程池执行", "run_in_threadpool(" in media_src, "")
    check("并发探测有上限", "_PROBE_SLOTS" in media_src, "")
    check("失败仍清理 .part", "tmp.unlink(missing_ok=True)" in media_src, "")

    tmp = Path(tempfile.mkdtemp(prefix="yiyan-upload-"))
    old_dir = smedia.UPLOADS_DIR
    old_probe = smedia._probe_audio
    try:
        smedia.UPLOADS_DIR = tmp
        part = tmp / ".uploading-1.part"
        part.write_bytes(b"RIFF-audit")
        smedia._probe_audio = lambda _path: (1.5, "wav")
        token, duration, fmt = smedia._finalize_upload(part, "海阔天空.wav", 9, "a" * 64)
        check("收尾生成内容哈希 token",
              token == f"{'a' * 16}.wav" and (tmp / token).is_file(), token)
        note = json.loads((tmp / f"{token}.json").read_text(encoding="utf-8"))
        check("旁注保留原始文件名", note.get("name") == "海阔天空.wav", str(note))
        check("临时文件已改名", not part.exists(), "")
        check("时长/容器格式一并返回",
              duration == 1.5 and fmt == "wav", f"{duration} / {fmt}")
    finally:
        smedia.UPLOADS_DIR = old_dir
        smedia._probe_audio = old_probe
        shutil.rmtree(tmp, ignore_errors=True)


def test_projects_and_versions() -> None:
    """F1：草稿项目库、编辑版本与恢复、创作表单自动保存。

    草稿以前被成片墙排除（它没有成片，列出来会被判成失败），于是"只拆未拍"
    的项目只存在于 runs/ 里，用户找不回来。这里统一列出，并给出恢复入口。
    """
    print("\n[21] 草稿项目库与版本恢复")
    import shutil
    import tempfile

    import server.auth as sauth
    import server.history as shist
    import server.plan as splan
    from server.schemas import ShotPatch

    tmp = Path(tempfile.mkdtemp(prefix="yiyan-projects-"))
    saved = {
        "db": sauth.DB_PATH,
        "runs": shist.RUNS,
        "root": splan.ROOT,
    }
    try:
        sauth.DB_PATH = tmp / "work" / "yiyan.db"
        shist.RUNS = tmp / "runs"
        splan.ROOT = tmp
        sauth.init_db()

        def seed(run_id: str, theme: str, rendered: bool, video: bool) -> Path:
            project = shist.RUNS / run_id
            plan_dir = project / "plan"
            plan_dir.mkdir(parents=True)
            (plan_dir / "brief.json").write_text(
                json.dumps({"theme": theme, "total_duration": 6, "aspect_ratio": "16:9"},
                           ensure_ascii=False), encoding="utf-8")
            (plan_dir / "script.json").write_text("{}", encoding="utf-8")
            (plan_dir / "storyboard.json").write_text(json.dumps({
                "project": {"total_duration": 6.0, "title": theme},
                "subjects": [],
                "shots": [
                    {"id": 1, "act": "开场", "duration": 3.0, "start": 0.0,
                     "visual": "old-1", "camera": "固定", "narration": "一"},
                    {"id": 2, "act": "开场", "duration": 3.0, "start": 3.0,
                     "visual": "old-2", "camera": "固定", "narration": "二"},
                ],
            }, ensure_ascii=False), encoding="utf-8")
            (plan_dir / "meta.json").write_text(json.dumps({
                "revision": 0,
                "rendered": rendered,
                "render_profile": {"live": False, "painted": False, "painted_fps": 24,
                                   "painted_quality": "medium", "clean_frames": False},
                "music": {"token": "0123456789abcdef.wav", "asset": "assets/music.wav",
                          "lyrics": "第一句"},
            }, ensure_ascii=False), encoding="utf-8")
            if video:
                out = project / "output"
                out.mkdir(parents=True)
                (out / "final.mp4").write_bytes(b"audit-video")
            return plan_dir

        draft_id = "20261115-120000-草稿项目-abc123"
        rendered_id = "20261115-121000-已拍项目-def456"
        draft_dir = seed(draft_id, "草稿项目", rendered=False, video=False)
        seed(rendered_id, "已拍项目", rendered=True, video=True)

        all_items = shist.list_projects(state="all", limit=50, principal=None)["items"]
        by_id = {item["run_id"]: item for item in all_items}
        check("项目库列出草稿", by_id.get(draft_id, {}).get("state") == "draft",
              str(by_id.get(draft_id)))
        check("项目库列出已拍", by_id.get(rendered_id, {}).get("state") == "ok",
              str(by_id.get(rendered_id)))
        check("项目卡片带标题/镜数/版本",
              by_id[draft_id]["title"] == "草稿项目"
              and by_id[draft_id]["shots"] == 2
              and by_id[draft_id]["revision"] == 0, str(by_id[draft_id]))
        drafts = shist.list_projects(state="draft", limit=50, principal=None)["items"]
        check("草稿筛选只返回草稿",
              [item["run_id"] for item in drafts] == [draft_id], str(drafts))
        rendered = shist.list_projects(state="rendered", limit=50, principal=None)["items"]
        check("已拍筛选排除草稿",
              [item["run_id"] for item in rendered] == [rendered_id], str(rendered))

        splan._archive_version(draft_dir)
        first = splan.list_versions(draft_id, None)
        check("初始版本可列出",
              first["current"] == 0 and [v["revision"] for v in first["versions"]] == [0],
              str(first))
        splan.patch_shot(draft_id, 1, ShotPatch(visual="改过"), None)
        second = splan.list_versions(draft_id, None)
        check("编辑后新增版本",
              second["current"] == 1
              and [v["revision"] for v in second["versions"]] == [1, 0], str(second))

        restored = splan.restore_version(draft_id, 0, None)
        board = json.loads((draft_dir / "storyboard.json").read_text(encoding="utf-8"))
        meta = json.loads((draft_dir / "meta.json").read_text(encoding="utf-8"))
        check("恢复把分镜改回旧版本",
              board["shots"][0]["visual"] == "old-1", str(board["shots"][0]))
        check("恢复生成新版本号而不是回退计数", meta.get("revision") == 2, str(meta.get("revision")))
        check("恢复保留音轨与拍摄配置",
              (meta.get("music") or {}).get("asset") == "assets/music.wav"
              and (meta.get("render_profile") or {}).get("live") is False,
              str(meta.get("music")))
        check("恢复后标记为未拍摄", meta.get("rendered") is False, str(meta.get("rendered")))
        check("恢复接口返回完整拆解",
              restored.get("run_id") == draft_id and len(restored.get("shots") or []) == 2,
              str(restored.keys()))
        try:
            splan.restore_version(draft_id, 99, None)
            check("不存在的版本返回 404", False, "没有报错")
        except Exception as exc:  # noqa: BLE001
            status = getattr(exc, "status_code", None)
            check("不存在的版本返回 404", status == 404, str(status))

        from server.app import app
        paths = app.openapi()["paths"]
        check("有项目库接口", "/api/projects" in paths, str([p for p in paths if "project" in p]))
        check("有版本与恢复接口",
              "/api/plan/{run_id}/versions" in paths
              and "/api/plan/{run_id}/versions/{revision}/restore" in paths, "")

        projects_page = (ROOT / "web" / "src" / "pages" / "ProjectsPage.tsx").read_text(encoding="utf-8")
        check("前端有草稿项目库页面",
              "fetchProjects" in projects_page and "草稿" in projects_page, "")
        nav = (ROOT / "web" / "src" / "components" / "TopNav.tsx").read_text(encoding="utf-8")
        check("导航有项目入口", "key: 'projects'" in nav, "")
        plan_page = (ROOT / "web" / "src" / "pages" / "PlanPage.tsx").read_text(encoding="utf-8")
        check("拆解页有版本恢复入口",
              "fetchVersions" in plan_page and "restoreVersion" in plan_page, "")
        create_page = (ROOT / "web" / "src" / "pages" / "CreatePage.tsx").read_text(encoding="utf-8")
        check("创作表单自动保存草稿",
              "DRAFT_KEY" in create_page and "localStorage.setItem(DRAFT_KEY" in create_page, "")
    finally:
        sauth.DB_PATH = saved["db"]
        shist.RUNS = saved["runs"]
        splan.ROOT = saved["root"]
        shutil.rmtree(tmp, ignore_errors=True)


def test_task_center() -> None:
    """BE-09 + F6：任务状态落库、重启收尾与任务中心。

    以前任务只活在内存注册表里，进程一退就"任务不存在"——跑到一半的片子
    连"曾经跑到哪"都查不到。这里把状态与成片路径落库，并给用户一个任务中心。
    """
    print("\n[22] 任务中心与重启恢复")
    import asyncio
    import shutil
    import tempfile
    from types import SimpleNamespace

    from app.engine import RunOptions
    from fastapi import HTTPException

    import server.app as sapp
    import server.auth as sauth
    import server.jobs as sjobs
    import server.taskstore as stasks
    from server.auth import Principal

    tmp = Path(tempfile.mkdtemp(prefix="yiyan-tasks-"))
    saved_db = sauth.DB_PATH
    saved_registry = sapp.REGISTRY
    try:
        sauth.DB_PATH = tmp / "work" / "yiyan.db"
        sauth.init_db()
        stasks.init_tasks()

        owner = Principal("guest", "a" * 32)
        other = Principal("guest", "b" * 32)

        job = sjobs.Job("测试任务", RunOptions(theme="测试任务", live=False),
                        principal=owner, kind="create")
        created = stasks.get_task(job.job_id)
        check("任务创建即落库", bool(created) and created["status"] == "running", str(created))
        check("任务记录带渲染配置",
              (created.get("options") or {}).get("live") is False, str(created.get("options")))

        job._on_start("run-task-1")
        check("run_id 回填到任务记录",
              stasks.get_task(job.job_id)["run_id"] == "run-task-1", "")

        video = tmp / "runs" / "run-task-1" / "output" / "final.mp4"
        video.parent.mkdir(parents=True)
        video.write_bytes(b"audit-video")
        job._on_done({"run_id": "run-task-1", "stopped": False,
                      "compose": {"video": str(video)}})
        row = stasks.get_task(job.job_id)
        check("完成后状态与成片落库",
              row["status"] == "done" and row["video"] == str(video), str(row["status"]))
        check("结果体可回读",
              (row.get("result") or {}).get("run_id") == "run-task-1", "")
        check("任务按主体过滤",
              [t["job_id"] for t in stasks.list_tasks(owner.key)] == [job.job_id], "")
        check("其他主体看不到任务", stasks.list_tasks(other.key) == [], "")
        check("列表摘要不含结果体与本地路径",
              "video" not in stasks.list_tasks(owner.key)[0]
              and "result" not in stasks.list_tasks(owner.key)[0], "")

        snap = sapp._task_snapshot(row)
        check("重启后快照含状态与成片标记",
              snap["status"] == "done" and snap["has_video"] and snap["finished"], str(snap))
        check("快照不泄露归属", "owner_key" not in snap, "")

        # 内存注册表里已经没有它，接口必须走落库回落
        sapp.REGISTRY = SimpleNamespace(get=lambda _jid: None, recent=lambda _p: [])
        check("重启后按 job_id 仍可查",
              sapp.job_state(job.job_id, owner)["status"] == "done", "")
        try:
            sapp.job_state(job.job_id, other)
            check("其他主体按 id 查不到", False, "没有抛异常")
        except HTTPException as exc:
            check("其他主体按 id 查不到", exc.status_code == 404, f"status={exc.status_code}")
        check("任务中心列表按主体过滤",
              [t["job_id"] for t in sapp.recent_runs(60, owner)["jobs"]] == [job.job_id], "")
        check("匿名任务列表为空", sapp.recent_runs(60, None)["jobs"] == [], "")

        resp = sapp.run_events(job.job_id, 0, owner)

        async def collect(response):
            parts = []
            async for chunk in response.body_iterator:
                parts.append(chunk.encode("utf-8") if isinstance(chunk, str) else chunk)
            return b"".join(parts)

        body = asyncio.run(collect(resp)).decode("utf-8")
        check("重启后事件流直接给出终态", "event: done" in body, body[:80])

        job2 = sjobs.Job("跑到一半", RunOptions(theme="跑到一半"),
                         principal=owner, kind="plan")
        stasks.init_tasks()
        check("重启把运行中任务标为中断",
              stasks.get_task(job2.job_id)["status"] == "interrupted", "")
        snap2 = sapp._task_snapshot(stasks.get_task(job2.job_id))
        check("中断任务给出原因", snap2["failed"] and bool(snap2["error"]), str(snap2))

        app_src = (ROOT / "server" / "app.py").read_text(encoding="utf-8")
        check("启动即做任务收尾", "init_tasks()" in app_src, "")
        jobs_src = (ROOT / "server" / "jobs.py").read_text(encoding="utf-8")
        check("任务终态写入任务表", "taskstore.update_task" in jobs_src, "")
        plan_src = (ROOT / "server" / "plan.py").read_text(encoding="utf-8")
        check("拍摄任务记录种类",
              '"preview_shot" if preview and only_shot' in plan_src
              and '"recompose" if recompose' in plan_src, "")
        tasks_page = (ROOT / "web" / "src" / "pages" / "TasksPage.tsx").read_text(encoding="utf-8")
        check("前端有任务中心", "fetchTasks" in tasks_page and "接着看" in tasks_page, "")
        nav = (ROOT / "web" / "src" / "components" / "TopNav.tsx").read_text(encoding="utf-8")
        check("导航有任务入口", "key: 'tasks'" in nav, "")
        hook = (ROOT / "web" / "src" / "hooks" / "useRun.ts").read_text(encoding="utf-8")
        check("任务可接回进度", "const attach" in hook and "attach," in hook, "")
    finally:
        sauth.DB_PATH = saved_db
        sapp.REGISTRY = saved_registry
        shutil.rmtree(tmp, ignore_errors=True)


def test_visual_review() -> None:
    """F3：质检证据展示与人工复核。

    模拟评审曾经和真实视觉评审长得一模一样，分数被当成"画面通过了"。
    这里把技术证据（尺寸/亮度/对比度/哈希）原样展示，并让人工结论独立留档：
    通过/驳回只记录"人看过"，不改变已合成画面。
    """
    print("\n[23] 质检证据与人工复核")
    import shutil
    import tempfile

    import server.auth as sauth
    import server.history as shist
    from server.auth import Principal, record_owner

    tmp = Path(tempfile.mkdtemp(prefix="yiyan-review-panel-"))
    saved_db = sauth.DB_PATH
    saved_runs = shist.RUNS
    try:
        sauth.DB_PATH = tmp / "work" / "yiyan.db"
        shist.RUNS = tmp / "runs"
        sauth.init_db()

        run_id = "20261116-100000-质检面板-abc123"
        out = shist.RUNS / run_id / "output"
        out.mkdir(parents=True)
        (out / "final.mp4").write_bytes(b"audit-video")
        sha_a, sha_b = "a" * 64, "b" * 64
        (out / "storyboard.json").write_text(json.dumps({
            "project": {"title": "质检面板", "total_duration": 6.0,
                        "aspect_ratio": "16:9", "style": "纪实"},
            "subjects": [],
            "shots": [
                {"id": 1, "start": 0.0, "duration": 3.0, "status": "approved",
                 "quality": 4, "attempts": 1, "defect_type": "none",
                 "review_mode": "simulated", "visual_check": "not_performed",
                 "review_rule": "rule-2026.11", "image_sha256": sha_a,
                 "tech_ok": True, "tech_reason": "", "image_width": 1920,
                 "image_height": 1080, "image_mean": 112.4, "image_contrast": 30.1},
                {"id": 2, "start": 3.0, "duration": 3.0, "status": "rejected",
                 "quality": 1, "attempts": 2, "defect_type": "image_unreadable",
                 "review_mode": "technical_failed", "visual_check": "failed",
                 "review_rule": "rule-2026.11", "image_sha256": sha_b,
                 "tech_ok": False, "tech_reason": "unreadable", "image_width": 0,
                 "image_height": 0, "image_mean": 0.0, "image_contrast": 0.0},
            ],
        }, ensure_ascii=False), encoding="utf-8")

        panel = shist.run_review(run_id, None)
        first, second = panel["shots"]
        summary = panel["summary"]
        check("质检面板列出逐镜证据",
              first["technical"]["recorded"] and first["technical"]["width"] == 1920
              and first["technical"]["contrast"] == 30.1, str(first["technical"]))
        check("技术检查失败单独标出",
              second["technical"]["recorded"] and not second["technical"]["ok"]
              and second["technical"]["reason"] == "unreadable", str(second["technical"]))
        check("汇总区分模拟评审与技术失败",
              summary["modes"] == {"simulated": 1, "technical_failed": 1}
              and summary["technical_failed"] == 1, str(summary))
        check("初始全部待复核",
              summary["pending"] == 2 and not first["manual"], str(summary))

        guest_a = Principal("guest", "a" * 32)
        guest_b = Principal("guest", "b" * 32)
        saved = shist.review_shot(
            run_id, 1,
            shist.ReviewBody(verdict="approved", note="构图清楚", image_sha256=sha_a),
            guest_a,
        )
        check("人工通过写入并返回新汇总",
              saved["shot"]["manual"]["verdict"] == "approved"
              and saved["summary"]["manual_approved"] == 1, str(saved["summary"]))
        check("人工备注保留",
              shist.run_review(run_id, None)["shots"][0]["manual"]["note"] == "构图清楚", "")
        check("复核说明不改变成片",
              "不改变已合成画面" in saved["note"], saved["note"])

        try:
            shist.review_shot(
                run_id, 2,
                shist.ReviewBody(verdict="rejected", image_sha256=sha_a),
                guest_a,
            )
            check("画面哈希不符拒绝写入", False, "被接受了")
        except Exception as exc:  # noqa: BLE001
            check("画面哈希不符拒绝写入",
                  getattr(exc, "status_code", None) == 409, str(exc))
        try:
            shist.review_shot(run_id, 2, shist.ReviewBody(verdict="maybe"), guest_a)
            check("非法结论被拒", False, "被接受了")
        except Exception as exc:  # noqa: BLE001
            check("非法结论被拒", getattr(exc, "status_code", None) == 422, str(exc))
        try:
            shist.review_shot(run_id, 99, shist.ReviewBody(verdict="approved"), guest_a)
            check("不存在的镜头返回 404", False, "被接受了")
        except Exception as exc:  # noqa: BLE001
            check("不存在的镜头返回 404", getattr(exc, "status_code", None) == 404, str(exc))

        cleared = shist.review_shot(
            run_id, 1, shist.ReviewBody(verdict="clear"), guest_a
        )
        check("撤销人工结论",
              cleared["shot"]["manual"] is None
              and cleared["summary"]["pending"] == 2, str(cleared["summary"]))
        check("复核文件原子写无残留",
              not list(out.glob(".review-*.tmp")), str(list(out.glob(".review-*.tmp"))))

        # 归属：有主之后其他人既看不到也写不了
        record_owner(run_id, guest_a)
        check("有主运行仅主人可见", shist.run_review(run_id, guest_a)["run_id"] == run_id, "")
        try:
            shist.run_review(run_id, guest_b)
            check("他人看不到质检面板", False, "被接受了")
        except Exception as exc:  # noqa: BLE001
            check("他人看不到质检面板", getattr(exc, "status_code", None) == 404, str(exc))
        try:
            shist.review_shot(run_id, 2, shist.ReviewBody(verdict="approved"), guest_b)
            check("他人写不了人工结论", False, "被接受了")
        except Exception as exc:  # noqa: BLE001
            check("他人写不了人工结论", getattr(exc, "status_code", None) == 404, str(exc))

        from server.app import app
        paths = app.openapi()["paths"]
        check("有质检面板接口",
              "/api/history/{run_id}/review" in paths
              and "/api/history/{run_id}/review/{shot_id}" in paths, "")

        models_src = (ROOT / "pipeline" / "models.py").read_text(encoding="utf-8")
        check("分镜模型保存技术证据",
              "tech_ok: bool | None" in models_src and "image_contrast" in models_src, "")
        pipeline_src = (ROOT / "pipeline" / "pipeline.py").read_text(encoding="utf-8")
        check("渲染时写入技术证据",
              "shot.image_mean = round" in pipeline_src
              and "shot.tech_ok = bool" in pipeline_src, "")
        api = (ROOT / "web" / "src" / "lib" / "api.ts").read_text(encoding="utf-8")
        check("前端有质检接口类型",
              "ReviewShot" in api and "submitReview" in api, "")
        page = (ROOT / "web" / "src" / "pages" / "HistoryPage.tsx").read_text(encoding="utf-8")
        check("成片详情有质检面板",
              "fetchReview" in page and "人工通过" in page and "人工驳回" in page, "")
    finally:
        sauth.DB_PATH = saved_db
        shist.RUNS = saved_runs
        shutil.rmtree(tmp, ignore_errors=True)


def test_preview_render() -> None:
    """F2 第一切片：低清预览。

    预览必须同时做到三件事，缺一件就会坏事：
    1. 真的更便宜（半分辨率、运动画布不再放大、绘制帧率封顶）；
    2. 产物独立（覆盖正式成片是最不可接受的失败方向）；
    3. 不改项目状态（否则成片墙多了假记录，单镜重拍还会拿预览片段去拼）。
    """
    print("\n[24] 低清预览与独立产物")
    import shutil
    import tempfile
    from types import SimpleNamespace

    from fastapi import HTTPException

    from pipeline.config import Settings, existing_run_context
    from pipeline.image_render import _dims, half_size
    from pipeline.models import ProjectBrief
    from pipeline.pipeline import Pipeline
    from server.schemas import RenderRequest

    import pipeline.config as pconfig
    import server.auth as sauth
    import server.history as shist
    import server.plan as splan

    check("16:9 预览尺寸减半", half_size((1920, 1080)) == (960, 540),
          str(half_size((1920, 1080))))
    check("竖屏预览尺寸减半", half_size((1080, 1920)) == (540, 960),
          str(half_size((1080, 1920))))
    check("预览尺寸保持偶数", all(v % 2 == 0 for v in half_size((1080, 1080))),
          str(half_size((1080, 1080))))

    brief = ProjectBrief(theme="预览", total_duration=6, aspect_ratio="16:9")
    check("占位帧可按半尺寸绘制", _dims(brief, 0.5) == (960, 540), str(_dims(brief, 0.5)))
    check("整数倍率行为不变", _dims(brief, 2) == (3840, 2160), str(_dims(brief, 2)))

    tmp = Path(tempfile.mkdtemp(prefix="yiyan-preview-"))
    smoke_root: Path | None = None
    saved = {
        "db": sauth.DB_PATH,
        "runs": shist.RUNS,
        "root": splan.ROOT,
        "registry": splan.REGISTRY,
    }
    try:
        sauth.DB_PATH = tmp / "work" / "yiyan.db"
        shist.RUNS = tmp / "runs"
        splan.ROOT = tmp
        sauth.init_db()

        ctx = existing_run_context("r1", Settings(values={}), namespace="preview")
        base = pconfig.ROOT / "runs" / "r1"
        check("预览成片写独立目录",
              ctx.out_dir == base / "preview" / "output", str(ctx.out_dir))
        check("预览工作目录独立",
              ctx.work_dir == base / "preview" / "work", str(ctx.work_dir))

        # 构造真实 Pipeline（只验证尺寸推导，不跑渲染）：
        # 预览的倍率与分辨率在 __init__ 与 _setup_for_brief 两处一起决定。
        from pipeline.llm import CreativeLayer
        from pipeline.decide import RuleDecider as _RuleDecider
        from pipeline.prompts import PromptLibrary
        from pipeline.runlog import RunLog

        lib = PromptLibrary.load()
        preview_settings = Settings(values={})
        pipe = Pipeline(
            preview_settings, lib, ctx, RunLog(tmp / "preview-run.jsonl"),
            CreativeLayer(lib, None), _RuleDecider(lib), preview=True,
        )
        pipe._setup_for_brief(brief, "")
        widths = [getattr(item, "width", None) for item in pipe.video_chain]
        check("预览成片尺寸减半", pipe._video_size == (960, 540), str(pipe._video_size))
        check("运动后端也按预览尺寸", 960 in widths, str(widths))
        check("预览不再放大工作画布",
              pipe.work_scale == 1 and pipe.still_scale == 0.5
              and pipe.fixture_scale == 0.5,
              f"{pipe.work_scale}/{pipe.still_scale}/{pipe.fixture_scale}")

        run_id = "20261116-120000-预览项目-abc123"
        plan_dir = shist.RUNS / run_id / "plan"
        plan_dir.mkdir(parents=True)
        (plan_dir / "brief.json").write_text(json.dumps(
            {"theme": "预览项目", "total_duration": 6, "aspect_ratio": "16:9"},
            ensure_ascii=False), encoding="utf-8")
        (plan_dir / "script.json").write_text("{}", encoding="utf-8")
        (plan_dir / "storyboard.json").write_text(json.dumps({
            "project": {"total_duration": 6.0, "title": "预览项目"},
            "subjects": [],
            "shots": [
                {"id": 1, "act": "开场", "duration": 3.0, "start": 0.0,
                 "visual": "一", "camera": "固定", "narration": "一"},
                {"id": 2, "act": "开场", "duration": 3.0, "start": 3.0,
                 "visual": "二", "camera": "固定", "narration": "二"},
            ],
        }, ensure_ascii=False), encoding="utf-8")
        (plan_dir / "meta.json").write_text(json.dumps({
            "revision": 0,
            "rendered": False,
            "render_profile": {"live": False, "painted": False, "painted_fps": 24,
                               "painted_quality": "medium", "clean_frames": False},
        }, ensure_ascii=False), encoding="utf-8")

        captured: list[tuple[object, str]] = []

        class FakeRegistry:
            def create(self, theme, opts, principal=None, kind="create"):
                captured.append((opts, kind))
                return SimpleNamespace(job_id=f"job-{len(captured)}")

        splan.REGISTRY = FakeRegistry()
        splan._start_render(run_id, RenderRequest(), None, preview=True)
        opts, kind = captured[-1]
        check("预览任务带 preview 标志", opts.preview is True and kind == "preview",
              f"{opts.preview}/{kind}")
        meta = json.loads((plan_dir / "meta.json").read_text(encoding="utf-8"))
        check("预览不把项目标记为已拍", not meta.get("rendered"), str(meta.get("rendered")))
        check("预览记录时间与版本",
              bool(meta.get("last_preview_at")) and meta.get("last_preview_revision") == 0,
              str(meta.get("last_preview_at")))
        check("预览不写正式拍摄配置", "last_render_profile" not in meta, "")

        # 单镜预览：允许 preview + only_shot，但仍然是"预览"，不标已拍。
        splan._start_render(run_id, RenderRequest(), None, only_shot=2, preview=True)
        opts_shot, kind_shot = captured[-1]
        meta_shot = json.loads((plan_dir / "meta.json").read_text(encoding="utf-8"))
        check("单镜预览任务可启动",
              opts_shot.preview is True and opts_shot.only_shot == 2
              and kind_shot == "preview_shot",
              f"{opts_shot.preview}/{opts_shot.only_shot}/{kind_shot}")
        check("单镜预览仍不标已拍", not meta_shot.get("rendered"), str(meta_shot.get("rendered")))
        check("记录最近单镜预览",
              (meta_shot.get("last_shot_preview") or {}).get("shot") == 2,
              str(meta_shot.get("last_shot_preview")))

        # 已有正式片段要让界面看得见（只重合成会复用这些片段）。
        clips_dir = plan_dir.parent / "work" / "clips"
        clips_dir.mkdir(parents=True, exist_ok=True)
        (clips_dir / "shot01.mp4").write_bytes(b"x")
        plan_payload = splan.read_plan(run_id, None)
        check("拆解接口返回已有片段",
              plan_payload.get("clips", {}).get("available") == [1],
              str(plan_payload.get("clips")))

        splan._start_render(run_id, RenderRequest(), None)
        opts2, kind2 = captured[-1]
        meta2 = json.loads((plan_dir / "meta.json").read_text(encoding="utf-8"))
        check("正式拍摄仍走原路径",
              opts2.preview is False and kind2 == "plan" and meta2.get("rendered") is True,
              f"{opts2.preview}/{kind2}/{meta2.get('rendered')}")

        from server.app import app
        paths = app.openapi()["paths"]
        check("有整片与单镜预览接口",
              "/api/plan/{run_id}/preview" in paths
              and "/api/plan/{run_id}/shots/{shot_id}/preview" in paths, "")

        render_mjs = (ROOT / "vendor" / "painted-template" / "render.mjs").read_text(encoding="utf-8")
        check("绘制引擎支持独立帧目录", "frames-dir" in render_mjs, "")
        painted_src = (ROOT / "pipeline" / "painted.py").read_text(encoding="utf-8")
        check("绘制帧目录可传入",
              "frames_dir: Path | None" in painted_src and "--frames-dir=" in painted_src, "")
        api = (ROOT / "web" / "src" / "lib" / "api.ts").read_text(encoding="utf-8")
        check("前端有整片与单镜预览接口",
              "previewPlan" in api and "previewShot" in api
              and "preview?: boolean" in api and "shot_preview?: boolean" in api, "")
        plan_page = (ROOT / "web" / "src" / "pages" / "PlanPage.tsx").read_text(encoding="utf-8")
        check("拆解页有整片预览与单镜预览入口",
              "低清预览" in plan_page and "onPreview" in plan_page
              and "预览这一镜" in plan_page and "onPreviewShot" in plan_page, "")
        check("拆解页标记片段缓存",
              "片段已缓存" in plan_page and "clips?.available" in plan_page,
              "")
        hook = (ROOT / "web" / "src" / "hooks" / "useRun.ts").read_text(encoding="utf-8")
        check("预览复用事件流",
              "const startPreview" in hook and "startPreview," in hook
              and "startShotPreview" in hook, "")
        create_page = (ROOT / "web" / "src" / "pages" / "CreatePage.tsx").read_text(encoding="utf-8")
        check("结果页区分整片与单镜预览",
              "result.preview" in create_page and "低清预览" in create_page
              and "单镜预览" in create_page and "result.shot_preview" in create_page, "")

        # ---- 真跑一次单镜预览：只出这一镜，不产出正式成片
        smoke_root = pconfig.ROOT / "runs" / "preview-shot-smoke"
        smoke_plan = smoke_root / "plan"
        smoke_plan.mkdir(parents=True)
        (smoke_plan / "brief.json").write_text(json.dumps(
            {"theme": "单镜预览", "total_duration": 4, "aspect_ratio": "16:9", "style": "纪实"},
            ensure_ascii=False), encoding="utf-8")
        (smoke_plan / "script.json").write_text(json.dumps({
            "title": "单镜预览", "logline": "", "tone": "纪实",
            "target_audience": "", "hook": "",
            "narration_full": "第一句。第二句。", "key_subjects": [], "acts": [],
        }, ensure_ascii=False), encoding="utf-8")
        (smoke_plan / "storyboard.json").write_text(json.dumps({
            "project": {"total_duration": 4.0, "title": "单镜预览"},
            "subjects": [],
            "shots": [
                {"id": 1, "act": "开场", "duration": 2.0, "start": 0.0,
                 "visual": "海边日出", "camera": "固定", "narration": "第一句"},
                {"id": 2, "act": "收束", "duration": 2.0, "start": 2.0,
                 "visual": "海浪", "camera": "固定", "narration": "第二句"},
            ],
        }, ensure_ascii=False), encoding="utf-8")
        ctx2 = existing_run_context("preview-shot-smoke", Settings(values={}), namespace="preview")
        ctx2.prepare()
        pipe2 = Pipeline(
            Settings(values={}), lib, ctx2, RunLog(ctx2.log_path),
            CreativeLayer(lib, None), _RuleDecider(lib), preview=True,
        )
        smoke = pipe2.render_from_plan(smoke_plan, only_shot=1)
        smoke_video = Path(smoke["compose"]["video"])
        check("单镜预览产出这一镜片段",
              smoke.get("shot_preview") is True and smoke.get("shot_id") == 1
              and smoke_video.is_file(), str(smoke.get("summary")))
        check("单镜预览片段为半分辨率",
              smoke["compose"]["resolution"] == "960x540", smoke["compose"]["resolution"])
        check("单镜预览不产出正式成片",
              not (smoke_root / "output" / "final.mp4").exists(), "")
        check("单镜预览不写正式片段缓存",
              not (smoke_root / "work" / "clips" / "shot01.mp4").exists(), "")
        check("单镜预览片段落在 preview 命名空间",
              "preview" in smoke_video.parts, str(smoke_video))
    finally:
        sauth.DB_PATH = saved["db"]
        shist.RUNS = saved["runs"]
        splan.ROOT = saved["root"]
        splan.REGISTRY = saved["registry"]
        if smoke_root is not None:
            shutil.rmtree(smoke_root, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)


def test_audio_subtitle_edit() -> None:
    """F5 第一切片：混音电平与字幕整体校准。

    这两个设置改变的是"成片的声音与字幕轨"，不需要重画任何画面。所以它们
    必须能随项目保存，并且由「只重合成」直接生效；字幕偏移必须夹在有效时长内，
    提前/延后过量时不能产出负时间或超出成片的坏字幕。
    """
    print("\n[25] 混音电平与字幕校准")
    import shutil
    import tempfile
    from types import SimpleNamespace

    from pydantic import ValidationError

    from pipeline.lyrics import LyricLine, lyric_srt
    from pipeline.models import Shot, Storyboard
    from pipeline.tts import write_srt
    from server.schemas import RenderRequest

    import server.auth as sauth
    import server.history as shist
    import server.plan as splan

    tmp = Path(tempfile.mkdtemp(prefix="yiyan-mix-"))
    saved = {
        "db": sauth.DB_PATH,
        "runs": shist.RUNS,
        "root": splan.ROOT,
        "registry": splan.REGISTRY,
    }
    try:
        board = Storyboard(
            project={"total_duration": 4.0},
            shots=[
                Shot(id=1, act="开场", duration=2.0, narration="第一句", visual="画面", camera="固定"),
                Shot(id=2, act="收束", duration=2.0, narration="第二句", visual="画面", camera="固定"),
            ],
        )
        board.assign_timeline()
        srt_path = tmp / "offset.srt"

        write_srt(board, srt_path, offset=1.0)
        delayed = srt_path.read_text(encoding="utf-8-sig")
        check("字幕整体延后",
              "00:00:01,000 --> 00:00:03,000" in delayed
              and "00:00:03,000 --> 00:00:04,000" in delayed, delayed[:120])

        write_srt(board, srt_path, offset=-1.5)
        early = srt_path.read_text(encoding="utf-8-sig")
        check("提前时起点夹到 0",
              "00:00:00,000 --> 00:00:00,500" in early, early[:120])
        check("提前后不产生负时间", "-00:" not in early, early[:120])

        write_srt(board, srt_path, offset=10.0)
        check("整体移出成片后不写坏字幕",
              "-->" not in srt_path.read_text(encoding="utf-8-sig"), "")

        lyric_path = tmp / "lyric.srt"
        line = LyricLine(start=3.0, end=4.0, text="啊")
        lyric_srt([LyricLine(start=1.0, end=2.0, text="哦"), line], lyric_path,
                  offset=-2.5, max_end=4.0)
        lyric_text = lyric_path.read_text(encoding="utf-8-sig")
        check("歌词提前时丢弃已移出片头的整句",
              "哦" not in lyric_text and "00:00:00,500 --> 00:00:01,500" in lyric_text,
              lyric_text[:120])
        lyric_srt([line], lyric_path, offset=3.0, max_end=4.0)
        check("歌词延后超出成片时不写坏字幕",
              "-->" not in lyric_path.read_text(encoding="utf-8-sig"), "")

        sauth.DB_PATH = tmp / "work" / "yiyan.db"
        shist.RUNS = tmp / "runs"
        splan.ROOT = tmp
        sauth.init_db()

        run_id = "20261116-140000-混音项目-abc123"
        plan_dir = shist.RUNS / run_id / "plan"
        plan_dir.mkdir(parents=True)
        (plan_dir / "brief.json").write_text(json.dumps(
            {"theme": "混音项目", "total_duration": 4, "aspect_ratio": "16:9"},
            ensure_ascii=False), encoding="utf-8")
        (plan_dir / "script.json").write_text("{}", encoding="utf-8")
        (plan_dir / "storyboard.json").write_text(json.dumps({
            "project": {"total_duration": 4.0, "title": "混音项目"},
            "subjects": [],
            "shots": [
                {"id": 1, "act": "开场", "duration": 2.0, "start": 0.0,
                 "visual": "一", "camera": "固定", "narration": "一"},
                {"id": 2, "act": "收束", "duration": 2.0, "start": 2.0,
                 "visual": "二", "camera": "固定", "narration": "二"},
            ],
        }, ensure_ascii=False), encoding="utf-8")
        (plan_dir / "meta.json").write_text(json.dumps({
            "revision": 0,
            "rendered": True,
            "render_profile": {"live": False, "painted": False, "painted_fps": 24,
                               "painted_quality": "medium", "clean_frames": False},
            "audio_edit": {"voice_gain": 0.5, "music_gain": 0.2, "subtitle_offset": -0.4},
        }, ensure_ascii=False), encoding="utf-8")

        captured: list[object] = []

        class FakeRegistry:
            def create(self, theme, opts, principal=None, kind="create"):
                captured.append(opts)
                return SimpleNamespace(job_id=f"job-{len(captured)}")

        splan.REGISTRY = FakeRegistry()
        splan._start_render(run_id, RenderRequest(), None)
        opts = captured[-1]
        check("拍摄继承项目保存的混音设置",
              opts.voice_gain == 0.5 and opts.music_gain == 0.2
              and opts.subtitle_offset == -0.4,
              f"{opts.voice_gain}/{opts.music_gain}/{opts.subtitle_offset}")

        splan._start_render(
            run_id,
            RenderRequest(voice_gain=0.8, subtitle_offset=0.6),
            None,
        )
        opts = captured[-1]
        meta = json.loads((plan_dir / "meta.json").read_text(encoding="utf-8"))
        check("显式设置覆盖并保存",
              opts.voice_gain == 0.8 and opts.subtitle_offset == 0.6
              and meta.get("audio_edit", {}).get("voice_gain") == 0.8,
              str(meta.get("audio_edit")))
        check("未覆盖的字段沿用保存值",
              meta.get("audio_edit", {}).get("music_gain") == 0.2,
              str(meta.get("audio_edit")))
        snap_dirs = sorted(p for p in (plan_dir / "snapshots").iterdir() if p.is_dir())
        snap_audio = snap_dirs[0] / "audio_edit.json"
        check("快照冻结混音设置",
              snap_audio.is_file()
              and json.loads(snap_audio.read_text(encoding="utf-8"))["music_gain"] == 0.2,
              str(snap_audio))

        splan._start_render(run_id, RenderRequest(music_gain=0.9), None, preview=True)
        opts = captured[-1]
        meta_after = json.loads((plan_dir / "meta.json").read_text(encoding="utf-8"))
        check("预览可以试听电平但不改项目设置",
              opts.music_gain == 0.9
              and meta_after.get("audio_edit", {}).get("music_gain") == 0.2,
              str(meta_after.get("audio_edit")))

        try:
            RenderRequest(voice_gain=3.0)
            check("混音电平越界被拒", False, "被接受了")
        except ValidationError:
            check("混音电平越界被拒", True, "")

        compose_src = (ROOT / "pipeline" / "compose.py").read_text(encoding="utf-8")
        check("合成使用项目电平",
              "voice_gain: float = 1.0" in compose_src
              and "music_gain: float = BGM_GAIN" in compose_src, "")
        pipeline_src = (ROOT / "pipeline" / "pipeline.py").read_text(encoding="utf-8")
        check("重合成把设置传给合成",
              "subtitle_offset=self.subtitle_offset" in pipeline_src, "")
        tts_src = (ROOT / "pipeline" / "tts.py").read_text(encoding="utf-8")
        check("字幕写入支持整体偏移", "def write_srt(board: Storyboard, out_path: Path, offset" in tts_src, "")
        api = (ROOT / "web" / "src" / "lib" / "api.ts").read_text(encoding="utf-8")
        check("前端有混音设置类型",
              "interface AudioEdit" in api and "subtitle_offset?: number" in api, "")
        plan_page = (ROOT / "web" / "src" / "pages" / "PlanPage.tsx").read_text(encoding="utf-8")
        check("拆解页有音轨字幕面板",
              "音轨与字幕" in plan_page and "人声电平" in plan_page and "mixOpts()" in plan_page, "")
    finally:
        sauth.DB_PATH = saved["db"]
        shist.RUNS = saved["runs"]
        splan.ROOT = saved["root"]
        splan.REGISTRY = saved["registry"]
        shutil.rmtree(tmp, ignore_errors=True)


def test_subtitle_track() -> None:
    """F5：字幕轨拆分/合并与 LRC 导出。

    字幕一旦能被编辑，就必须独立于镜头/歌词保存：否则重合成会再用自动规则
    生成一遍，用户的断句与时间全被冲掉。同时要能和项目整体偏移共存——
    读成片字幕时扣掉偏移，避免保存后再叠一次。
    """
    print("\n[26] 字幕轨编辑与导出")
    import shutil
    import tempfile
    from types import SimpleNamespace

    from fastapi import HTTPException

    from pipeline.tts import parse_srt, srt_to_lrc, write_srt_lines
    from server.schemas import RenderRequest, SubtitleLine, SubtitlesBody

    import server.auth as sauth
    import server.history as shist
    import server.plan as splan

    tmp = Path(tempfile.mkdtemp(prefix="yiyan-subtitle-"))
    saved = {
        "db": sauth.DB_PATH,
        "runs": shist.RUNS,
        "root": splan.ROOT,
        "registry": splan.REGISTRY,
    }
    try:
        sauth.DB_PATH = tmp / "work" / "yiyan.db"
        shist.RUNS = tmp / "runs"
        splan.ROOT = tmp
        sauth.init_db()

        raw_srt = (
            "1\n00:00:01,500 --> 00:00:02,500\n第一句\n\n"
            "2\n00:00:02,500 --> 00:00:03,500\n第二句\n"
        )
        check("SRT 解析保留时间与文本",
              parse_srt(raw_srt)[0]["start"] == 1.5
              and parse_srt(raw_srt)[0]["text"] == "第一句", str(parse_srt(raw_srt)))
        check("带 BOM 的 SRT 不把序号当正文",
              parse_srt("\ufeff" + raw_srt) == parse_srt(raw_srt),
              str(parse_srt("\ufeff" + raw_srt)))
        check("LRC 导出按启动时间标记",
              srt_to_lrc(raw_srt).startswith("[00:01.50]第一句"), srt_to_lrc(raw_srt))

        lines_path = tmp / "edited.srt"
        write_srt_lines(
            [{"start": 0.5, "end": 1.5, "text": "一"},
             {"start": 1.5, "end": 5.0, "text": "二"}],
            lines_path, offset=0.5, limit=4.0,
        )
        edited = lines_path.read_text(encoding="utf-8-sig")
        check("人工字幕应用偏移并夹到片尾",
              "00:00:01,000 --> 00:00:02,000" in edited
              and "00:00:02,000 --> 00:00:04,000" in edited, edited[:140])
        # 写盘用 utf-8-sig（带 BOM），读回若按普通 utf-8 解析，第一句会混进序号。
        roundtrip = parse_srt(lines_path.read_text(encoding="utf-8", errors="replace"))
        check("字幕写盘再读回不带序号与 BOM",
              [row["text"] for row in roundtrip] == ["一", "二"], str(roundtrip))

        run_id = "20261116-150000-字幕项目-abc123"
        plan_dir = shist.RUNS / run_id / "plan"
        plan_dir.mkdir(parents=True)
        out = plan_dir.parent / "output"
        out.mkdir(parents=True)
        (out / "subtitles.srt").write_text(raw_srt, encoding="utf-8")
        (plan_dir / "brief.json").write_text(json.dumps(
            {"theme": "字幕项目", "total_duration": 4, "aspect_ratio": "16:9"},
            ensure_ascii=False), encoding="utf-8")
        (plan_dir / "script.json").write_text("{}", encoding="utf-8")
        (plan_dir / "storyboard.json").write_text(json.dumps({
            "project": {"total_duration": 4.0, "title": "字幕项目"},
            "subjects": [],
            "shots": [
                {"id": 1, "act": "开场", "duration": 2.0, "start": 0.0,
                 "visual": "一", "camera": "固定", "narration": "第一句"},
                {"id": 2, "act": "收束", "duration": 2.0, "start": 2.0,
                 "visual": "二", "camera": "固定", "narration": "第二句"},
            ],
        }, ensure_ascii=False), encoding="utf-8")
        (plan_dir / "meta.json").write_text(json.dumps({
            "revision": 0,
            "rendered": True,
            "render_profile": {"live": False, "painted": False, "painted_fps": 24,
                               "painted_quality": "medium", "clean_frames": False},
            "audio_edit": {"voice_gain": 1.0, "music_gain": 0.28, "subtitle_offset": 0.5},
        }, ensure_ascii=False), encoding="utf-8")

        derived = splan.list_subtitles(run_id, None)
        check("默认读上次成片字幕",
              derived["source"] == "rendered" and len(derived["lines"]) == 2, str(derived))
        check("读取时扣除整体偏移",
              derived["lines"][0]["start"] == 1.0 and derived["lines"][0]["end"] == 2.0,
              str(derived["lines"][0]))

        saved_lines = [
            SubtitleLine(start=0.5, end=1.5, text="改过一"),
            SubtitleLine(start=1.5, end=3.0, text="改过二"),
        ]
        saved_out = splan.save_subtitles(run_id, SubtitlesBody(lines=saved_lines), None)
        meta = json.loads((plan_dir / "meta.json").read_text(encoding="utf-8"))
        check("保存字幕轨并递增版本",
              saved_out["revision"] == 1
              and len(meta.get("subtitle_lines") or []) == 2, str(saved_out))
        check("来源标记为人工保存",
              splan.list_subtitles(run_id, None)["source"] == "edited", "")
        try:
            splan.save_subtitles(run_id, SubtitlesBody(lines=saved_lines, expected_revision=0), None)
            check("旧版本号保存被 409 挡住", False, "被接受了")
        except HTTPException as exc:
            check("旧版本号保存被 409 挡住", exc.status_code == 409, f"status={exc.status_code}")

        try:
            splan.save_subtitles(run_id, SubtitlesBody(lines=[
                SubtitleLine(start=2.0, end=2.5, text="甲"),
                SubtitleLine(start=2.2, end=3.0, text="乙"),
            ]), None)
            check("重叠字幕被拒", False, "被接受了")
        except HTTPException as exc:
            check("重叠字幕被拒", exc.status_code == 422, f"status={exc.status_code}")
        try:
            splan.save_subtitles(run_id, SubtitlesBody(lines=[
                SubtitleLine(start=3.0, end=5.0, text="超时"),
            ]), None)
            check("超出成片时长被拒", False, "被接受了")
        except HTTPException as exc:
            check("超出成片时长被拒", exc.status_code == 422, f"status={exc.status_code}")
        try:
            splan.save_subtitles(run_id, SubtitlesBody(lines=[
                SubtitleLine(start=1.0, end=1.0, text="零长"),
            ]), None)
            check("零长度字幕被拒", False, "被接受了")
        except Exception as exc:  # noqa: BLE001 — pydantic 也会在这里拦
            check("零长度字幕被拒", getattr(exc, "status_code", None) == 422
                  or exc.__class__.__name__ == "ValidationError", str(exc))

        captured: list[object] = []

        class FakeRegistry:
            def create(self, theme, opts, principal=None, kind="create"):
                captured.append(opts)
                return SimpleNamespace(job_id=f"job-{len(captured)}")

        splan.REGISTRY = FakeRegistry()
        splan._start_render(run_id, RenderRequest(), None)
        opts = captured[-1]
        check("拍摄带上人工字幕轨",
              opts.subtitle_lines == [
                  {"start": 0.5, "end": 1.5, "text": "改过一"},
                  {"start": 1.5, "end": 3.0, "text": "改过二"},
              ], str(opts.subtitle_lines))
        snap_dirs = sorted(p for p in (plan_dir / "snapshots").iterdir() if p.is_dir())
        snap_meta = json.loads((snap_dirs[-1] / "meta.json").read_text(encoding="utf-8"))
        check("快照冻结人工字幕",
              len(snap_meta.get("subtitle_lines") or []) == 2, str(snap_meta.keys()))

        splan.save_subtitles(run_id, SubtitlesBody(lines=[]), None)
        splan._start_render(run_id, RenderRequest(), None)
        check("空字幕轨表达为明确不要字幕",
              captured[-1].subtitle_lines == [], str(captured[-1].subtitle_lines))

        splan.save_subtitles(run_id, SubtitlesBody(lines=None), None)
        meta = json.loads((plan_dir / "meta.json").read_text(encoding="utf-8"))
        check("恢复自动生成移除人工字幕",
              "subtitle_lines" not in meta
              and splan.list_subtitles(run_id, None)["source"] == "rendered", "")

        from server.app import app
        paths = app.openapi()["paths"]
        check("有字幕轨接口",
              "/api/plan/{run_id}/subtitles" in paths, "")

        api = (ROOT / "web" / "src" / "lib" / "api.ts").read_text(encoding="utf-8")
        check("前端有字幕轨接口与 LRC 导出",
              "fetchSubtitles" in api and "saveSubtitles" in api and "historyLrcUrl" in api, "")
        plan_page = (ROOT / "web" / "src" / "pages" / "PlanPage.tsx").read_text(encoding="utf-8")
        check("拆解页有字幕轨编辑",
              "字幕轨 ·" in plan_page and "拆分" in plan_page and "合并" in plan_page
              and "persistSubtitles" in plan_page, "")
        history_page = (ROOT / "web" / "src" / "pages" / "HistoryPage.tsx").read_text(encoding="utf-8")
        check("成片详情可下载 SRT/LRC",
              "下载 SRT" in history_page and "下载 LRC" in history_page, "")
    finally:
        sauth.DB_PATH = saved["db"]
        shist.RUNS = saved["runs"]
        splan.ROOT = saved["root"]
        splan.REGISTRY = saved["registry"]
        shutil.rmtree(tmp, ignore_errors=True)


def test_subject_assets() -> None:
    """F4 第一切片：参考素材存档 + 角色卡 + 能力声明。

    关键不是"能传图"，而是**不撒谎**：当前出图后端都不吃参考图，界面与
    接口必须明说；能真正跨镜一致的是 visual_lock 文本，它要在出图时被
    拼进提示词，且不能改坏用户写的原稿。参考图按内容哈希存档，重拆与
    版本恢复不能把用户资产冲掉。
    """
    print("\n[27] 角色参考图与视觉锁定")
    import io
    import shutil
    import tempfile
    from types import SimpleNamespace

    from fastapi import HTTPException
    from PIL import Image

    from pipeline.images import OfflineFrameProvider, provider_status
    from pipeline.models import Shot, Storyboard, Subject
    from pipeline.pipeline import Pipeline
    from server.schemas import ReplanRequest, SubjectPatch

    import server.auth as sauth
    import server.history as shist
    import server.plan as splan

    tmp = Path(tempfile.mkdtemp(prefix="yiyan-subject-"))
    saved = {
        "db": sauth.DB_PATH,
        "runs": shist.RUNS,
        "root": splan.ROOT,
        "engine": splan.Engine,
    }

    def png_bytes(color: tuple[int, int, int] = (120, 80, 200)) -> bytes:
        buf = io.BytesIO()
        Image.new("RGB", (128, 96), color).save(buf, format="PNG")
        return buf.getvalue()

    class FakeUpload:
        def __init__(self, data: bytes, name: str = "hero.png"):
            self.file = io.BytesIO(data)
            self.filename = name

    try:
        # ---- 纯逻辑：视觉锁定注入出图提示词，且不改原稿
        shot = Shot(id=1, act="开场", duration=2.0, narration="", visual="镜头",
                    camera="固定", prompt_image="一只猫", subject_refs=["hero", "scene"])
        board = Storyboard(project={}, subjects=[
            Subject(id="hero", name="主角", visual_lock="红围巾，圆眼镜，全片同款"),
            Subject(id="scene", name="场景", visual_lock="暖色黄昏"),
        ], shots=[shot])
        gen, locks = Pipeline._locked_prompt(None, shot, board)
        check("出图提示词拼上引用的视觉锁定",
              "红围巾" in gen.prompt_image and "暖色黄昏" in gen.prompt_image
              and gen.prompt_image.startswith("一只猫"), gen.prompt_image)
        check("注入不改写原稿", shot.prompt_image == "一只猫", shot.prompt_image)
        check("锁定去重且记录引用",
              locks == ["红围巾，圆眼镜，全片同款", "暖色黄昏"], str(locks))

        plain = Shot(id=2, act="", duration=2.0, narration="", visual="", camera="",
                     prompt_image="风景", subject_refs=[])
        same, none = Pipeline._locked_prompt(None, plain, board)
        check("没引用任何角色时原样返回", same is plain and none == [], "")

        # ---- 能力声明：能出图 ≠ 能消费参考图
        ok, why = OfflineFrameProvider().reference_capability()
        check("离线后端声明不支持参考图", ok is False and "参考图" in why, why)

        class FakeSettings:
            def get(self, key, default=None):
                return {"COMFYUI_BASE_URL": "", "VIDEO_API_BASE_URL": "",
                        "VIDEO_API_KEY": ""}.get(key, default)

        wf = tmp / "workflows"
        wf.mkdir(parents=True)
        status = provider_status(FakeSettings(), wf, fresh=True)
        check("provider 状态带参考图能力字段",
              status and all("reference_images" in item and "reference_detail" in item
                             for item in status), str(status))
        check("当前所有后端都不支持参考图",
              all(item["reference_images"] == "否" for item in status), str(status))

        engine_src = (ROOT / "app" / "engine.py").read_text(encoding="utf-8")
        check("环境体检明确显示参考图能力",
              "参考图：" in engine_src and "reference_images" in engine_src, "")
        pipe_src = (ROOT / "pipeline" / "pipeline.py").read_text(encoding="utf-8")
        check("出图时应用角色锁定",
              "_locked_prompt" in pipe_src and "subject_locks" in pipe_src, "")

        # ---- HTTP 侧：项目、角色卡、上传/读取/移除
        sauth.DB_PATH = tmp / "work" / "yiyan.db"
        shist.RUNS = tmp / "runs"
        splan.ROOT = tmp
        sauth.init_db()

        run_id = "20261116-160000-角色项目-abc123"
        plan_dir = shist.RUNS / run_id / "plan"
        plan_dir.mkdir(parents=True)
        (plan_dir / "brief.json").write_text(json.dumps(
            {"theme": "角色项目", "total_duration": 4, "aspect_ratio": "16:9"},
            ensure_ascii=False), encoding="utf-8")
        (plan_dir / "script.json").write_text(json.dumps({
            "title": "角色项目",
            "acts": [{"name": "开场", "time_range": "0-4s", "content": "开场"}],
        }, ensure_ascii=False), encoding="utf-8")
        (plan_dir / "storyboard.json").write_text(json.dumps({
            "project": {"total_duration": 4.0, "title": "角色项目"},
            "subjects": [
                {"id": "hero", "name": "主角", "visual_lock": "红围巾"},
                {"id": "scene", "name": "场景", "visual_lock": "暖色黄昏"},
            ],
            "shots": [
                {"id": 1, "act": "开场", "duration": 2.0, "start": 0.0,
                 "visual": "一", "camera": "固定", "narration": "一",
                 "subject_refs": ["hero"]},
                {"id": 2, "act": "开场", "duration": 2.0, "start": 2.0,
                 "visual": "二", "camera": "固定", "narration": "二",
                 "subject_refs": ["hero", "scene"]},
            ],
        }, ensure_ascii=False), encoding="utf-8")
        (plan_dir / "meta.json").write_text(json.dumps(
            {"revision": 0, "edited": {"script": False, "shots": []}},
            ensure_ascii=False), encoding="utf-8")

        up = splan.upload_subject_reference(
            run_id, "hero", FakeUpload(png_bytes()), expected_revision=0, principal=None)
        ref = up["subject"]["reference"]
        meta = json.loads((plan_dir / "meta.json").read_text(encoding="utf-8"))
        check("参考图存档并递增版本",
              ref["asset"].startswith("assets/subjects/") and meta["revision"] == 1,
              str(ref))
        asset_path = plan_dir / ref["asset"]
        check("参考图按内容哈希落盘", asset_path.is_file(), str(asset_path))
        check("角色卡记录被标记为用户改过",
              "hero" in (meta.get("edited", {}).get("subjects") or []), str(meta.get("edited")))

        served = splan.read_subject_reference(run_id, "hero", None)
        check("参考图接口返回文件",
              Path(getattr(served, "path", "")).is_file(), str(getattr(served, "path", "")))

        plan_payload = splan.read_plan(run_id, None)
        hero = next(s for s in plan_payload["subjects"] if s["id"] == "hero")
        check("拆解接口带上参考图信息",
              hero.get("reference", {}).get("sha256") == ref["sha256"], str(hero))

        try:
            splan.upload_subject_reference(
                run_id, "ghost", FakeUpload(png_bytes()), expected_revision=None,
                principal=None)
            check("不存在的角色被拒", False, "被接受了")
        except HTTPException as exc:
            check("不存在的角色被拒", exc.status_code == 404, f"status={exc.status_code}")

        try:
            splan.upload_subject_reference(
                run_id, "hero", FakeUpload(b"not an image"), expected_revision=None,
                principal=None)
            check("非图片文件被拒", False, "被接受了")
        except HTTPException as exc:
            check("非图片文件被拒", exc.status_code == 422, f"status={exc.status_code}")

        try:
            splan.upload_subject_reference(
                run_id, "hero",
                FakeUpload(b"x" * (splan.MAX_REFERENCE_BYTES + 1)),
                expected_revision=None, principal=None)
            check("超大参考图被 413 挡住", False, "被接受了")
        except HTTPException as exc:
            check("超大参考图被 413 挡住", exc.status_code == 413, f"status={exc.status_code}")

        try:
            splan.upload_subject_reference(
                run_id, "hero", FakeUpload(png_bytes()), expected_revision=0,
                principal=None)
            check("旧版本号上传被 409 挡住", False, "被接受了")
        except HTTPException as exc:
            check("旧版本号上传被 409 挡住", exc.status_code == 409, f"status={exc.status_code}")

        patched = splan.patch_subject(
            run_id, "hero", SubjectPatch(visual_lock="红围巾，圆眼镜", expected_revision=1),
            None)
        check("角色卡保存视觉锁定",
              patched["subject"]["visual_lock"] == "红围巾，圆眼镜"
              and patched["affected_shots"] == [1, 2], str(patched.get("affected_shots")))
        check("改锁定会列出引用镜头并说明不自动更新",
              "重拍" in patched["note"] and patched["capability"], patched["note"])

        # 重拆时保留参考图与用户手改过的锁定
        class FakeEngine:
            def replan_act(self, opts, act_name, force=False):
                return {
                    "storyboard": {
                        "project": {"total_duration": 4.0},
                        "subjects": [
                            {"id": "hero", "name": "模型改名", "visual_lock": "模型新锁定"},
                            {"id": "scene", "name": "场景", "visual_lock": "模型场景"},
                        ],
                        "shots": [
                            {"id": 1, "act": act_name, "duration": 4.0, "start": 0.0,
                             "visual": "一", "camera": "固定", "narration": "一",
                             "subject_refs": ["hero"]},
                        ],
                    },
                    "shots": [],
                    "total_duration": 4.0,
                    "id_map": {"1": "1", "2": "1"},
                    "target": 4.0,
                }

        splan.Engine = lambda: FakeEngine()
        replanned = splan.replan_act(run_id, "开场", ReplanRequest(force=True), None)
        by_id = {s["id"]: s for s in replanned["storyboard"]["subjects"]}
        check("重拆保留参考图",
              by_id["hero"].get("reference", {}).get("sha256") == ref["sha256"], str(by_id["hero"]))
        check("重拆保留用户手改的锁定",
              by_id["hero"]["visual_lock"] == "红围巾，圆眼镜", by_id["hero"]["visual_lock"])
        check("未手改的角色跟随模型更新",
              by_id["scene"]["visual_lock"] == "模型场景", by_id["scene"]["visual_lock"])

        removed = splan.delete_subject_reference(run_id, "hero", None, None)
        check("移除参考图后角色卡清空",
              not removed["subject"].get("reference"), str(removed["subject"]))
        check("旧版本引用的图片不被立即删除", asset_path.is_file(), str(asset_path))
        try:
            splan.read_subject_reference(run_id, "hero", None)
            check("没有参考图时读取返回 404", False, "被接受了")
        except HTTPException as exc:
            check("没有参考图时读取返回 404", exc.status_code == 404, f"status={exc.status_code}")

        from server.app import app
        paths = app.openapi()["paths"]
        check("有角色卡与参考图接口",
              "/api/plan/{run_id}/subjects/{subject_id}" in paths
              and "/api/plan/{run_id}/subjects/{subject_id}/reference" in paths, "")

        api = (ROOT / "web" / "src" / "lib" / "api.ts").read_text(encoding="utf-8")
        check("前端有角色卡接口与参考图地址",
              "patchSubject" in api and "uploadSubjectReference" in api
              and "subjectReferenceUrl" in api, "")
        plan_page = (ROOT / "web" / "src" / "pages" / "PlanPage.tsx").read_text(encoding="utf-8")
        check("拆解页有角色卡与引用标记",
              "角色卡 ·" in plan_page and "视觉锁定" in plan_page
              and "上传参考图" in plan_page and "subject_refs" in plan_page, "")
    finally:
        sauth.DB_PATH = saved["db"]
        shist.RUNS = saved["runs"]
        splan.ROOT = saved["root"]
        splan.Engine = saved["engine"]
        shutil.rmtree(tmp, ignore_errors=True)


def test_waveform() -> None:
    """F5：音轨峰值缓存与波形接口。

    波形是"用眼睛对齐字幕"的唯一依据。两个前提必须成立：
    1. 峰值算得对（静音段接近 0，有声段接近 1）；
    2. 结果能缓存且会在音轨变化时失效——否则换歌之后还在画旧波形，
       用户会照着错误的波峰调字幕。
    """
    print("\n[28] 音轨波形与峰值缓存")
    import math
    import os
    import shutil
    import tempfile
    import time
    import wave

    from fastapi import HTTPException

    from pipeline.waveform import clamp_buckets, peaks as wave_peaks

    import server.auth as sauth
    import server.history as shist
    import server.plan as splan

    tmp = Path(tempfile.mkdtemp(prefix="yiyan-wave-"))
    saved = {"db": sauth.DB_PATH, "runs": shist.RUNS, "root": splan.ROOT}
    try:
        sample_rate = 8000
        frames = bytearray()
        for i in range(sample_rate * 2):
            value = 0 if i < sample_rate else int(12000 * math.sin(2 * math.pi * 440 * i / sample_rate))
            frames += int(value).to_bytes(2, "little", signed=True)
        audio = tmp / "tone.wav"
        with wave.open(str(audio), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(sample_rate)
            handle.writeframes(bytes(frames))

        data = wave_peaks(audio, tmp / "cache", 300)
        check("峰值桶数正确", len(data["peaks"]) == 300, str(len(data.get("peaks") or [])))
        check("静音段接近 0、有声段接近 1",
              data["peaks"][0] < 0.05 and data["peaks"][-1] > 0.9,
              f"{data['peaks'][0]} → {data['peaks'][-1]}")
        check("时长按采样推算", abs(data["duration"] - 2.0) < 0.05, str(data["duration"]))
        again = wave_peaks(audio, tmp / "cache", 300)
        check("第二次读缓存",
              again["cached"] is True and (tmp / "cache" / "wave-300.json").is_file(),
              str(again["cached"]))
        check("桶数夹在上下限",
              clamp_buckets(5) == 200 and clamp_buckets(99999) == 4000,
              f"{clamp_buckets(5)}/{clamp_buckets(99999)}")

        sauth.DB_PATH = tmp / "work" / "yiyan.db"
        shist.RUNS = tmp / "runs"
        splan.ROOT = tmp
        sauth.init_db()

        run_id = "20261116-170000-波形项目-abc123"
        run_dir = shist.RUNS / run_id
        plan_dir = run_dir / "plan"
        plan_dir.mkdir(parents=True)
        (run_dir / "assets").mkdir()
        shutil.copyfile(audio, run_dir / "assets" / "music.wav")
        frames = run_dir / "work" / "frames"
        frames.mkdir(parents=True)
        from PIL import Image as _Image

        _Image.new("RGB", (96, 54), (90, 120, 160)).save(frames / "shot01_a1.png")
        (plan_dir / "storyboard.json").write_text(json.dumps({
            "project": {"total_duration": 2.0},
            "subjects": [],
            "shots": [{"id": 1, "act": "开场", "duration": 2.0, "start": 0.0,
                       "visual": "一", "camera": "固定", "narration": "一"}],
        }, ensure_ascii=False), encoding="utf-8")
        (plan_dir / "meta.json").write_text(json.dumps({
            "revision": 0,
            "music": {"asset": "assets/music.wav", "name": "测试音轨.wav",
                      "role": "song", "duration": 2.0},
        }, ensure_ascii=False), encoding="utf-8")

        out = splan.plan_waveform(run_id, 300, None)
        check("波形接口返回峰值与音轨信息",
              len(out["peaks"]) == 300 and out["name"] == "测试音轨.wav"
              and out["role"] == "song", str({k: out.get(k) for k in ("name", "role", "cached")}))
        check("波形缓存落在项目 work 目录",
              (run_dir / "work" / "waveform" / "wave-300.json").is_file(),
              str(run_dir / "work" / "waveform"))
        check("首次接口调用未命中缓存", out["cached"] is False, str(out["cached"]))
        check("再次调用命中缓存",
              splan.plan_waveform(run_id, 300, None)["cached"] is True, "")

        future = time.time() + 30
        os.utime(run_dir / "assets" / "music.wav", (future, future))
        check("音轨变化后缓存失效",
              splan.plan_waveform(run_id, 300, None)["cached"] is False, "")

        meta = json.loads((plan_dir / "meta.json").read_text(encoding="utf-8"))
        meta.pop("music")
        (plan_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        try:
            splan.plan_waveform(run_id, 300, None)
            check("没有音轨时明确 404", False, "被接受了")
        except HTTPException as exc:
            check("没有音轨时明确 404", exc.status_code == 404, f"status={exc.status_code}")

        still = shist.run_shot_still(run_id, 1, None)
        check("单镜画面接口返回留存帧",
              Path(getattr(still, "path", "")).is_file(), str(getattr(still, "path", "")))
        try:
            shist.run_shot_still(run_id, 9, None)
            check("没有留存画面时 404", False, "被接受了")
        except HTTPException as exc:
            check("没有留存画面时 404", exc.status_code == 404, f"status={exc.status_code}")

        from server.app import app
        paths = app.openapi()["paths"]
        check("有波形接口与单镜画面接口",
              "/api/plan/{run_id}/waveform" in paths
              and "/api/history/{run_id}/shots/{shot_id}/still" in paths, "")

        api = (ROOT / "web" / "src" / "lib" / "api.ts").read_text(encoding="utf-8")
        check("前端有波形与单镜画面地址",
              "fetchWaveform" in api and "interface Waveform" in api
              and "historyStillUrl" in api, "")
        plan_page = (ROOT / "web" / "src" / "pages" / "PlanPage.tsx").read_text(encoding="utf-8")
        check("拆解页绘制可拖动波形与字幕块",
              "WaveformStrip" in plan_page and "波形与时间轴" in plan_page
              and "wave.peaks" in plan_page and "onPointerDown" in plan_page
              and "setPointerCapture" in plan_page and "cursor-ew-resize" in plan_page
              and "onAdjust" in plan_page, "")
        history_page = (ROOT / "web" / "src" / "pages" / "HistoryPage.tsx").read_text(encoding="utf-8")
        check("成片详情可叠安全区",
              "画面安全区 90/80%" in history_page and "inset-[5%]" in history_page
              and "inset-[10%]" in history_page and "historyStillUrl" in history_page, "")
    finally:
        sauth.DB_PATH = saved["db"]
        shist.RUNS = saved["runs"]
        splan.ROOT = saved["root"]
        shutil.rmtree(tmp, ignore_errors=True)


def test_export_profiles() -> None:
    """F7：成片多规格导出。

    导出只允许从**已拍成片**派生，并且必须做到：
    1. 目标分辨率正确、时长不变；
    2. 重复导出命中缓存，原片重拍后缓存失效；
    3. 原片已经是目标画幅时明确拒绝，不做无意义的重编码。
    """
    print("\n[29] 成片多规格导出")
    import os
    import shutil
    import tempfile
    import time

    from fastapi import HTTPException
    from pydantic import ValidationError
    from starlette.requests import Request

    from pipeline.compose import probe_resolution
    from pipeline.video import probe_duration, run_ffmpeg
    from server.schemas import ExportRequest

    import server.auth as sauth
    import server.exports as sexports
    import server.history as shist

    tmp = Path(tempfile.mkdtemp(prefix="yiyan-export-"))
    saved = {"db": sauth.DB_PATH, "runs": shist.RUNS}
    try:
        sauth.DB_PATH = tmp / "work" / "yiyan.db"
        shist.RUNS = tmp / "runs"
        sauth.init_db()

        def make_run(run_id: str, size: str = "320x180") -> tuple[Path, Path]:
            run_dir = shist.RUNS / run_id
            out = run_dir / "output"
            out.mkdir(parents=True)
            final = out / "final.mp4"
            run_ffmpeg([
                "-f", "lavfi", "-i", f"color=c=blue:s={size}:d=1:r=30",
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                "-t", "1", "-shortest",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                str(final),
            ])
            return run_dir, final

        def wait_done(run_id: str, export_id: str, timeout: float = 180.0) -> dict:
            deadline = time.time() + timeout
            status = sexports.export_status(run_id, export_id, None)
            while time.time() < deadline:
                status = sexports.export_status(run_id, export_id, None)
                if status["status"] != "running":
                    return status
                time.sleep(0.2)
            return status

        run_id = "20261116-180000-导出项目-abc123"
        run_dir, final = make_run(run_id)

        started = sexports.start_export(
            run_id, ExportRequest(aspect="9:16", mode="fit"), None)
        check("导出任务已启动",
              started["status"] in ("running", "done"), str(started))

        status = wait_done(run_id, "9-16-fit")
        check("竖屏适配导出完成",
              status["status"] == "done" and status["width"] == 1080
              and status["height"] == 1920, str(status))
        video = run_dir / "output" / "exports" / "9-16-fit.mp4"
        check("导出文件是 1080x1920",
              video.is_file() and probe_resolution(video) == "1080x1920", str(video))
        check("导出保留原片时长",
              abs(probe_duration(video) - 1.0) < 0.25, str(probe_duration(video)))
        check("导出元数据落盘",
              (run_dir / "output" / "exports" / "9-16-fit.json").is_file(), "")

        again = sexports.start_export(
            run_id, ExportRequest(aspect="9:16", mode="fit"), None)
        check("重复导出命中缓存",
              again["status"] == "done" and again["size_mb"] > 0, str(again))

        listing = sexports.list_exports(run_id, None)
        check("导出列表包含缓存",
              any(item["export_id"] == "9-16-fit" for item in listing["items"]),
              str(listing["items"]))

        future = time.time() + 30
        os.utime(final, (future, future))
        stale = sexports.start_export(
            run_id, ExportRequest(aspect="9:16", mode="fit"), None)
        check("原片变化后缓存失效并重导",
              stale["status"] in ("running", "done"), str(stale))
        stale_done = wait_done(run_id, "9-16-fit")
        check("失效后重新导出完成", stale_done["status"] == "done", str(stale_done))

        sexports.start_export(run_id, ExportRequest(aspect="1:1", mode="crop"), None)
        crop = wait_done(run_id, "1-1-crop")
        crop_video = run_dir / "output" / "exports" / "1-1-crop.mp4"
        check("方形裁切导出 1080x1080",
              crop["status"] == "done" and probe_resolution(crop_video) == "1080x1080",
              str(crop))

        portrait_id = "20261116-181000-竖屏项目-abc123"
        make_run(portrait_id, "180x320")
        try:
            sexports.start_export(
                portrait_id, ExportRequest(aspect="9:16", mode="fit"), None)
            check("原片已是目标画幅时拒绝", False, "被接受了")
        except HTTPException as exc:
            check("原片已是目标画幅时拒绝", exc.status_code == 422, f"status={exc.status_code}")

        empty_id = "20261116-182000-无片项目-abc123"
        (shist.RUNS / empty_id).mkdir(parents=True)
        try:
            sexports.start_export(
                empty_id, ExportRequest(aspect="9:16", mode="fit"), None)
            check("没有成片时拒绝导出", False, "被接受了")
        except HTTPException as exc:
            check("没有成片时拒绝导出", exc.status_code == 404, f"status={exc.status_code}")

        try:
            ExportRequest(aspect="4:3", mode="fit")
            check("非法画幅被拒", False, "被接受了")
        except ValidationError:
            check("非法画幅被拒", True, "")

        request = Request({
            "type": "http", "method": "GET", "headers": [], "query_string": b"",
            "path": "/", "server": ("test", 80), "client": ("test", 1),
            "scheme": "http",
        })
        response = sexports.download_run_export(run_id, "9-16-fit", request, None)
        check("导出文件可下载",
              Path(getattr(response, "path", "")).is_file(),
              str(getattr(response, "path", "")))

        from server.app import app
        paths = app.openapi()["paths"]
        check("有导出接口",
              "/api/history/{run_id}/exports" in paths
              and "/api/history/{run_id}/exports/{export_id}/file" in paths, "")

        api = (ROOT / "web" / "src" / "lib" / "api.ts").read_text(encoding="utf-8")
        check("前端有导出接口",
              "fetchExports" in api and "startExport" in api
              and "exportFileUrl" in api and "interface ExportItem" in api, "")
        history_page = (ROOT / "web" / "src" / "pages" / "HistoryPage.tsx").read_text(encoding="utf-8")
        check("成片详情有导出面板",
              "多规格导出" in history_page and "适配" in history_page
              and "裁切" in history_page and "launchExport" in history_page
              and "exportFileUrl" in history_page, "")
    finally:
        sauth.DB_PATH = saved["db"]
        shist.RUNS = saved["runs"]
        with sexports._LOCK:
            sexports._TASKS.clear()
        shutil.rmtree(tmp, ignore_errors=True)


def test_retry_cancel() -> None:
    """F6：失败任务重试 + 取消释放子进程。

    重试不是"重放旧快照"：画面内容由项目当前拆解决定，重试要按最新版本冻结。
    取消也不是只改个标志位：正在编码的 FFmpeg / node 绘制必须真的结束，
    否则用户点了停止，风扇还会转几分钟。
    """
    print("\n[30] 任务重试与取消释放子进程")
    import shutil
    import subprocess
    import sys
    import tempfile
    import threading
    import time
    from types import SimpleNamespace

    from fastapi import HTTPException

    from pipeline import procguard
    from pipeline import video as pvideo
    from server.auth import Principal

    import server.app as sapp
    import server.auth as sauth
    import server.history as shist
    import server.plan as splan
    import server.taskstore as stask

    tmp = Path(tempfile.mkdtemp(prefix="yiyan-retry-"))
    saved = {
        "db": sauth.DB_PATH,
        "runs": shist.RUNS,
        "root": splan.ROOT,
        "registry": splan.REGISTRY,
    }
    try:
        sauth.DB_PATH = tmp / "work" / "yiyan.db"
        shist.RUNS = tmp / "runs"
        splan.ROOT = tmp
        sauth.init_db()
        stask.init_tasks()

        run_id = "20261116-190000-重试项目-abc123"
        plan_dir = shist.RUNS / run_id / "plan"
        plan_dir.mkdir(parents=True)
        (plan_dir / "brief.json").write_text(json.dumps(
            {"theme": "重试项目", "total_duration": 4, "aspect_ratio": "16:9"},
            ensure_ascii=False), encoding="utf-8")
        (plan_dir / "script.json").write_text("{}", encoding="utf-8")
        (plan_dir / "storyboard.json").write_text(json.dumps({
            "project": {"total_duration": 4.0, "title": "重试项目"},
            "subjects": [],
            "shots": [
                {"id": 1, "act": "开场", "duration": 2.0, "start": 0.0,
                 "visual": "一", "camera": "固定", "narration": "一"},
                {"id": 2, "act": "收束", "duration": 2.0, "start": 2.0,
                 "visual": "二", "camera": "固定", "narration": "二"},
            ],
        }, ensure_ascii=False), encoding="utf-8")
        (plan_dir / "meta.json").write_text(json.dumps({
            "revision": 0,
            "rendered": {"revision": 0},
            "render_profile": {"live": False, "painted": False, "painted_fps": 24,
                               "painted_quality": "medium", "clean_frames": False},
        }, ensure_ascii=False), encoding="utf-8")
        clips_dir = shist.RUNS / run_id / "work" / "clips"
        clips_dir.mkdir(parents=True)
        for shot_id in (1, 2):
            (clips_dir / f"shot{shot_id:02d}.mp4").write_bytes(b"fake")

        captured: list[tuple[str, object]] = []

        class FakeRegistry:
            def create(self, theme, opts, principal=None, kind="create"):
                captured.append((kind, opts))
                return SimpleNamespace(job_id=f"retry-{len(captured)}")

        splan.REGISTRY = FakeRegistry()
        owner = Principal(kind="guest", id="a" * 32)
        base = {"live": False, "painted": False, "painted_fps": 24,
                "painted_quality": "medium", "clean_frames": False,
                "only_shot": 0, "recompose": False}

        def failed_task(job_id: str, kind: str, options: dict | None = None,
                        task_run_id: str = run_id) -> str:
            stask.create_task(job_id, owner.key, "测试任务", kind,
                              run_id=task_run_id, options=options or dict(base))
            stask.update_task(job_id, status="failed", error="模拟失败")
            return job_id

        out = sapp.retry_run(failed_task("job-plan", "plan"), owner)
        kind, opts = captured[-1]
        check("重试按项目重建整片拍摄",
              kind == "plan" and opts.preview is False and opts.only_shot == 0
              and out.get("retried_from") == "job-plan", f"{kind}/{out}")

        out = sapp.retry_run(failed_task("job-shot", "shot", {**base, "only_shot": 2}), owner)
        kind, opts = captured[-1]
        check("重试恢复单镜重拍的镜号",
              kind == "shot" and opts.only_shot == 2,
              f"{kind}/{getattr(opts, 'only_shot', None)}")

        out = sapp.retry_run(
            failed_task("job-preview-shot", "preview_shot", {**base, "only_shot": 1}), owner)
        kind, opts = captured[-1]
        check("重试恢复单镜预览",
              kind == "preview_shot" and opts.preview is True and opts.only_shot == 1,
              f"{kind}/{getattr(opts, 'preview', None)}/{getattr(opts, 'only_shot', None)}")

        sapp.retry_run(failed_task("job-recompose", "recompose", dict(base)), owner)
        kind, opts = captured[-1]
        check("重试恢复只重合成", kind == "recompose" and opts.recompose is True,
              f"{kind}/{getattr(opts, 'recompose', None)}")

        # 显式拍摄配置要跟着任务记录走，而不是被项目旧配置覆盖。
        sapp.retry_run(failed_task(
            "job-live", "plan",
            {**base, "live": True, "painted_quality": "high"}), owner)
        kind, opts = captured[-1]
        check("重试沿用任务记录里的拍摄配置",
              opts.live is True and opts.painted_quality == "high",
              f"{opts.live}/{opts.painted_quality}")

        try:
            sapp.retry_run(failed_task("job-create", "create"), owner)
            check("一句话成片明确拒绝重试", False, "被接受了")
        except HTTPException as exc:
            check("一句话成片明确拒绝重试", exc.status_code == 422, f"status={exc.status_code}")

        stask.create_task("job-done", owner.key, "已完成", "plan",
                          run_id=run_id, options=dict(base))
        stask.update_task("job-done", status="done")
        try:
            sapp.retry_run("job-done", owner)
            check("已完成任务不允许重试", False, "被接受了")
        except HTTPException as exc:
            check("已完成任务不允许重试", exc.status_code == 409, f"status={exc.status_code}")

        other = Principal(kind="guest", id="b" * 32)
        try:
            sapp.retry_run("job-plan", other)
            check("别人的任务不能重试", False, "被接受了")
        except HTTPException as exc:
            check("别人的任务不能重试", exc.status_code == 404, f"status={exc.status_code}")

        try:
            sapp.retry_run(failed_task("job-missing", "plan", task_run_id="no-such-run"), owner)
            check("项目不存在时明确报错", False, "被接受了")
        except HTTPException as exc:
            check("项目不存在时明确报错", exc.status_code == 404, f"status={exc.status_code}")

        from server.app import app
        check("有任务重试接口", "/api/runs/{job_id}/retry" in app.openapi()["paths"], "")

        # ---- 取消真的结束子进程
        procguard.begin_run()
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        check("子进程可登记", procguard.register(child) is True, "")
        check("登记计数正确", procguard.active_count() == 1, str(procguard.active_count()))
        ended = procguard.request_cancel()
        check("取消结束已登记进程", ended >= 1 and procguard.cancelled(), str(ended))
        try:
            child.wait(timeout=15)
            released = True
        except subprocess.TimeoutExpired:
            released = False
            child.kill()
        check("被取消的进程确实退出", released, "")
        procguard.unregister(child)

        # 取消之后才登记的新进程也必须立刻结束，不能漏网。
        procguard.begin_run()
        procguard.request_cancel()
        late = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        accepted = procguard.register(late)
        try:
            late.wait(timeout=15)
            late_gone = True
        except subprocess.TimeoutExpired:
            late_gone = False
            late.kill()
        check("取消后登记的进程立即结束", accepted is False and late_gone, str(accepted))

        # 正在编码的 FFmpeg 要能被取消打断，而不是等它自然跑完。
        procguard.begin_run()
        outcome: dict[str, object] = {}

        def encode() -> None:
            try:
                pvideo.run_ffmpeg([
                    "-re", "-f", "lavfi", "-i",
                    "testsrc=duration=30:size=320x180:rate=30",
                    "-f", "null", "-",
                ], timeout=60)
                outcome["ok"] = True
            except Exception as exc:  # noqa: BLE001
                outcome["error"] = exc

        worker = threading.Thread(target=encode, daemon=True)
        worker.start()
        deadline = time.time() + 10
        while time.time() < deadline and procguard.active_count() == 0:
            time.sleep(0.05)
        started = procguard.active_count() > 0
        cancel_started = time.time()
        procguard.request_cancel()
        worker.join(20)
        elapsed = time.time() - cancel_started
        check("取消能中断正在编码的 FFmpeg",
              started and not worker.is_alive() and "error" in outcome and elapsed < 15,
              f"started={started} alive={worker.is_alive()} err={str(outcome.get('error'))[:80]}")
        procguard.begin_run()

        engine_src = (ROOT / "app" / "engine.py").read_text(encoding="utf-8")
        check("取消入口会释放子进程",
              "procguard.request_cancel()" in engine_src and "_stopped_result" in engine_src, "")
        video_src = (ROOT / "pipeline" / "video.py").read_text(encoding="utf-8")
        check("FFmpeg 执行会登记进程",
              "procguard.register(proc)" in video_src and "cancellable" in video_src, "")
        exports_src = (ROOT / "server" / "exports.py").read_text(encoding="utf-8")
        check("导出不参与任务取消", "cancellable=False" in exports_src, "")
        jobs_src = (ROOT / "server" / "jobs.py").read_text(encoding="utf-8")
        check("停止提示说明会释放子进程", "子进程会被释放" in jobs_src, "")

        api = (ROOT / "web" / "src" / "lib" / "api.ts").read_text(encoding="utf-8")
        check("前端有重试接口", "retryTask" in api and "/retry" in api, "")
        tasks_page = (ROOT / "web" / "src" / "pages" / "TasksPage.tsx").read_text(encoding="utf-8")
        check("任务中心有重试入口",
              "retryTask(jobId)" in tasks_page and "重试" in tasks_page, "")
        create_page = (ROOT / "web" / "src" / "pages" / "CreatePage.tsx").read_text(encoding="utf-8")
        check("结果页说明停止后没有成片",
              "已停止，没有可交付的成片" in create_page and "子进程已释放" in create_page, "")
    finally:
        procguard.begin_run()
        sauth.DB_PATH = saved["db"]
        shist.RUNS = saved["runs"]
        splan.ROOT = saved["root"]
        splan.REGISTRY = saved["registry"]
        shutil.rmtree(tmp, ignore_errors=True)


def test_brand_batch() -> None:
    """F7：品牌模板（可撤销）与批量导出。

    品牌只在导出层生效：不改原片、不改时长。导出缓存必须把品牌指纹算进去，
    否则换了 logo/字卡之后还会把旧产物当成"已完成"。
    """
    print("\n[31] 品牌模板与批量导出")
    import asyncio
    import io
    import shutil
    import subprocess
    import tempfile
    import time
    from pathlib import Path

    from fastapi import HTTPException
    from fastapi import UploadFile
    from PIL import Image

    from pipeline import branding as pbrand
    from pipeline.compose import probe_resolution
    from pipeline.video import FFMPEG
    from server.auth import Principal
    from server.schemas import BatchExportRequest, BrandRequest, ExportRequest

    import server.auth as sauth
    import server.branding as sbrand
    import server.exports as sexp
    import server.history as shist
    import server.plan as splan

    tmp = Path(tempfile.mkdtemp(prefix="yiyan-brand-"))
    saved = {"root": splan.ROOT, "runs": shist.RUNS, "db": sauth.DB_PATH}
    try:
        splan.ROOT = tmp
        shist.RUNS = tmp / "runs"
        sauth.DB_PATH = tmp / "work" / "yiyan.db"
        sauth.init_db()
        sexp._TASKS.clear()

        owner = Principal(kind="guest", id="c" * 32)
        other = Principal(kind="guest", id="d" * 32)
        run_id = "20261116-200000-品牌批量-test"
        run = shist.RUNS / run_id
        (run / "plan").mkdir(parents=True)
        (run / "output").mkdir()
        (run / "plan" / "meta.json").write_text(json.dumps({"revision": 1}), encoding="utf-8")
        sauth.record_owner(run_id, owner)
        source = run / "output" / "final.mp4"
        subprocess.run([
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=3:size=640x360:rate=25",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
            "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", str(source),
        ], check=True, capture_output=True)

        state = sbrand.save_brand(run_id, BrandRequest(
            name="一言片场", position="top-left", opacity=0.9), owner)
        check("品牌模板保存后启用",
              state["applied"] and state["brand"]["version"] == 1
              and state["brand"]["position"] == "top-left", str(state["brand"]))

        # 旧项目没有 plan/ 目录：读品牌应返回"未配置"，而不是 404 让抽屉报错。
        legacy_id = "20261018-121940-旧项目无拆解"
        legacy = shist.RUNS / legacy_id
        (legacy / "output").mkdir(parents=True)
        sauth.record_owner(legacy_id, owner)
        legacy_state = sbrand.read_brand(legacy_id, owner)
        check("无拆解的旧项目读品牌不报 404",
              not legacy_state["applied"] and legacy_state["brand"]["name"] == ""
              and not legacy_state["can_undo"], str(legacy_state["brand"]))
        try:
            sbrand.read_brand("20261018-000000-根本不存在的运行", owner)
            check("不存在的运行读品牌仍 404", False, "被接受了")
        except HTTPException as exc:
            check("不存在的运行读品牌仍 404", exc.status_code == 404, f"status={exc.status_code}")
        try:
            sbrand.save_brand(legacy_id, BrandRequest(name="写不进"), owner)
            check("无拆解的旧项目写品牌仍被拒", False, "被接受了")
        except HTTPException as exc:
            check("无拆解的旧项目写品牌仍被拒", exc.status_code == 404, f"status={exc.status_code}")

        state = sbrand.save_brand(run_id, BrandRequest(
            name="第二版", intro_text="片头", outro_text="片尾"), owner)
        check("再次保存留出撤销档",
              state["brand"]["version"] == 2 and state["can_undo"] and state["history"] == 1,
              f"v{state['brand']['version']}/undo={state['can_undo']}")

        try:
            sbrand.save_brand(run_id, BrandRequest(name="抢写", expected_version=1), owner)
            check("品牌版本冲突被拒绝", False, "被接受了")
        except HTTPException as exc:
            check("品牌版本冲突被拒绝", exc.status_code == 409, f"status={exc.status_code}")

        state = sbrand.undo_brand(run_id, owner)
        check("撤销回到上一版内容",
              state["brand"]["name"] == "一言片场"
              and state["brand"]["intro_text"] == ""
              and state["brand"]["version"] == 3,
              f"{state['brand']['name']}/v{state['brand']['version']}")

        buf = io.BytesIO()
        Image.new("RGBA", (64, 64), (220, 60, 60, 255)).save(buf, "PNG")
        png = buf.getvalue()
        state = asyncio.run(sbrand.upload_brand_logo(
            run_id, UploadFile(file=io.BytesIO(png), filename="logo.png"), None, owner))
        asset_rel = str(state["brand"]["logo"]["asset"])
        logo_file = run / "plan" / asset_rel
        check("logo 按内容寻址落盘",
              bool(asset_rel) and logo_file.is_file() and state["logo_url"].endswith("/brand/logo"),
              asset_rel)

        state = sbrand.delete_brand_logo(run_id, None, owner)
        check("移除 logo 后配置清空", not state["brand"]["logo"]["asset"], "")
        state = sbrand.undo_brand(run_id, owner)
        check("撤销能找回 logo 且文件还在",
              state["brand"]["logo"]["asset"] == asset_rel and logo_file.is_file(),
              state["brand"]["logo"]["asset"])

        state = sbrand.clear_brand(run_id, owner)
        check("清除模板后不再启用", state["applied"] is False and state["can_undo"], "")

        try:
            sbrand.save_brand(run_id, BrandRequest(name="别人"), other)
            check("别人的项目不能改品牌", False, "被接受了")
        except HTTPException as exc:
            check("别人的项目不能改品牌", exc.status_code == 404, f"status={exc.status_code}")

        brand = pbrand.normalize({"name": "指纹", "intro_text": "片头"})
        fp1 = pbrand.fingerprint(brand, run / "plan")
        fp2 = pbrand.fingerprint(pbrand.normalize({"name": "指纹", "intro_text": "片头"}), run / "plan")
        fp3 = pbrand.fingerprint(pbrand.normalize({"name": "指纹", "intro_text": "片尾"}), run / "plan")
        check("品牌指纹对等价配置稳定、内容不同则变化",
              fp1 and fp1 == fp2 and fp1 != fp3, f"{fp1}/{fp3}")

        # 实际渲染：品牌层要真的出现在目标规格上，不是只写进 meta。
        sbrand.save_brand(run_id, BrandRequest(
            name="导出品牌", intro_text="出品 · 一言片场", outro_text="感谢观看",
            intro_seconds=1, outro_seconds=1), owner)
        out = sexp.start_batch(run_id, BatchExportRequest(targets=[
            ExportRequest(aspect="9:16", mode="fit"),
            ExportRequest(aspect="1:1", mode="crop"),
            ExportRequest(aspect="9:16", mode="fit"),
        ]), owner)
        check("批量导出自动去重",
              len(out["items"]) == 2 and out["branded"]
              and all(item["status"] == "running" for item in out["items"]),
              str([(i["export_id"], i["status"]) for i in out["items"]]))

        deadline = time.time() + 180
        items: list[dict] = []
        while time.time() < deadline:
            items = sexp.list_exports(run_id, owner)["items"]
            if len(items) >= 2 and all(item["status"] != "running" for item in items):
                break
            time.sleep(0.5)
        check("批量导出全部完成",
              len(items) == 2 and all(item["status"] == "done" for item in items),
              str([(i["export_id"], i["status"], i.get("error")) for i in items]))
        dims = {
            item["export_id"]: probe_resolution(run / "output" / "exports" / f"{item['export_id']}.mp4")
            for item in items
        }
        check("品牌版分辨率正确",
              dims.get("9-16-fit") == "1080x1920" and dims.get("1-1-crop") == "1080x1080",
              str(dims))
        meta = json.loads((run / "output" / "exports" / "9-16-fit.json").read_text(encoding="utf-8"))
        check("导出记录品牌版本与指纹",
              meta.get("branded") and meta.get("brand_fp") and meta.get("brand_version"),
              str({k: meta.get(k) for k in ("branded", "brand_fp", "brand_version")}))

        cached = sexp.start_batch(run_id, BatchExportRequest(targets=[
            ExportRequest(aspect="9:16", mode="fit"),
            ExportRequest(aspect="1:1", mode="crop"),
        ]), owner)
        check("同品牌重复批量命中缓存",
              all(item["status"] == "done" for item in cached["items"]),
              str([(i["export_id"], i["status"]) for i in cached["items"]]))

        sbrand.save_brand(run_id, BrandRequest(name="改名后"), owner)
        stale = sexp.list_exports(run_id, owner)
        check("品牌变更让旧导出失效",
              stale["items"] == [] and stale.get("branded") is True, str(stale["items"]))

        original_dims = sexp._source_dims
        original_render = sexp._render
        sexp._source_dims = lambda _path: (1080, 1080)

        def fake_render(task, _run_path, _source):
            task.status = "done"

        sexp._render = fake_render
        try:
            same = sexp.start_batch(run_id, BatchExportRequest(targets=[
                ExportRequest(aspect="1:1", mode="fit"),
            ]), owner)
            check("同画幅但有品牌变换仍可导出",
                  bool(same.get("items")) and same["items"][0]["aspect"] == "1:1",
                  str(same.get("items")))
        finally:
            sexp._source_dims = original_dims
            sexp._render = original_render

        # ---- I3-B3：16:9 原片 + 品牌也必须能导出 ----
        check("导出目标包含 16:9", "16:9" in sexp.TARGETS, str(sexp.TARGETS))
        original_dims = sexp._source_dims
        original_render = sexp._render

        def fake_render2(task, _run_path, _source):
            task.status = "done"

        sexp._source_dims = lambda _path: (1920, 1080)
        sexp._render = fake_render2
        try:
            wide = sexp.start_batch(run_id, BatchExportRequest(targets=[
                ExportRequest(aspect="16:9", mode="fit"),
            ]), owner)
            check("16:9 原片叠品牌可导出",
                  bool(wide.get("items")) and wide["items"][0]["aspect"] == "16:9"
                  and wide["items"][0]["mode"] == "fit", str(wide.get("items")))
        finally:
            sexp._source_dims = original_dims
            sexp._render = original_render

        # 同画幅且没有品牌 → 仍是空操作，批量整批跳过并 422。
        original_brand = sexp._brand_context
        sexp._brand_context = lambda _path: ({}, "", 0)
        sexp._source_dims = lambda _path: (1920, 1080)
        try:
            try:
                sexp.start_batch(run_id, BatchExportRequest(targets=[
                    ExportRequest(aspect="16:9", mode="fit"),
                ]), owner)
                check("16:9 无品牌时整批跳过", False, "被接受了")
            except HTTPException as exc:
                check("16:9 无品牌时整批跳过", exc.status_code == 422, f"status={exc.status_code}")
        finally:
            sexp._brand_context = original_brand
            sexp._source_dims = original_dims

        # ---- C3：导出规格预设 ----
        check("预设 id 不污染标准缓存",
              sexp.export_id_of("9:16", "fit") == "9-16-fit"
              and sexp.export_id_of("9:16", "fit", "douyin") == "9-16-fit-douyin", "")
        check("预设清单包含平台档位",
              {"standard", "douyin", "shipinhao", "bilibili", "high", "small"} <= set(sexp.PRESETS), "")
        try:
            sexp.preset_of("not-a-preset")
            check("未知预设被拒绝", False, "被接受了")
        except HTTPException as exc:
            check("未知预设被拒绝", exc.status_code == 422, f"status={exc.status_code}")
        try:
            sexp.start_batch(run_id, BatchExportRequest(targets=[
                ExportRequest(aspect="1:1", mode="fit", preset="douyin"),
            ]), owner)
            check("预设固定画幅不匹配时拒绝", False, "被接受了")
        except HTTPException as exc:
            check("预设固定画幅不匹配时拒绝", exc.status_code == 422, f"status={exc.status_code}")
        check("预设产物缓存键区分预设",
              sexp.export_id_of("9:16", "fit", "douyin")
              != sexp.export_id_of("9:16", "fit", "shipinhao"), "")

        from server.app import app
        paths = app.openapi()["paths"]
        check("品牌与批量接口已注册",
              "/api/history/{run_id}/brand" in paths
              and "/api/history/{run_id}/brand/undo" in paths
              and "/api/history/{run_id}/exports/batch" in paths, "")

        api = (ROOT / "web" / "src" / "lib" / "api.ts").read_text(encoding="utf-8")
        check("前端有品牌与批量接口",
              "startBatchExports" in api and "/brand/undo" in api
              and "brandLogoUrl" in api and "/exports/batch" in api, "")
        page = (ROOT / "web" / "src" / "pages" / "HistoryPage.tsx").read_text(encoding="utf-8")
        check("成片页有模板编辑与批量入口",
              "品牌模板" in page and "撤销上一步" in page
              and "批量导出选中" in page and "startBatchExports" in page, "")
    finally:
        sexp._TASKS.clear()
        splan.ROOT = saved["root"]
        shist.RUNS = saved["runs"]
        sauth.DB_PATH = saved["db"]
        shutil.rmtree(tmp, ignore_errors=True)


def test_provider_budget() -> None:
    """F8：费用台账与预算闸，以及"未接入不冒充就绪"的能力目录。"""
    print("\n[32] 生成器目录与费用预算")
    import shutil
    import tempfile
    import time
    from pathlib import Path

    from fastapi import HTTPException

    from pipeline import costs
    from pipeline.config import Settings
    from pipeline.video import CloudVideoProvider
    from server.auth import Principal

    import server.auth as sauth
    import server.history as shist
    import server.usage as susage

    tmp = Path(tempfile.mkdtemp(prefix="yiyan-usage-"))
    saved = {
        "runs": shist.RUNS,
        "db": sauth.DB_PATH,
        "settings": susage._settings,
    }
    try:
        shist.RUNS = tmp / "runs"
        sauth.DB_PATH = tmp / "work" / "yiyan.db"
        sauth.init_db()

        run_a = shist.RUNS / "run-a"
        run_b = shist.RUNS / "run-b"
        run_legacy = shist.RUNS / "run-legacy"
        for path, theme in ((run_a, "甲项目"), (run_b, "乙项目"), (run_legacy, "旧项目")):
            (path / "plan").mkdir(parents=True)
            (path / "plan" / "brief.json").write_text(
                json.dumps({"theme": theme}, ensure_ascii=False), encoding="utf-8")
            time.sleep(0.01)

        owner = Principal(kind="guest", id="e" * 32)
        other = Principal(kind="guest", id="f" * 32)
        sauth.record_owner(run_a.name, owner)
        sauth.record_owner(run_b.name, other)

        e1 = costs.record(run_a, kind="video", provider="cloud-video", amount_cny=0.5,
                          units=1, unit="s", rate_cny=0.5)
        costs.record(run_a, kind="video", provider="cloud-video", amount_cny=1.0,
                     units=2, unit="s", rate_cny=0.5)
        costs.record(run_a, kind="video", provider="cloud-video", amount_cny=0.0)
        costs.record(run_b, kind="video", provider="cloud-video", amount_cny=2.0)
        costs.record(run_legacy, kind="video", provider="cloud-video", amount_cny=9.0)
        ledger = run_a / costs.LEDGER
        ledger.write_text(
            ledger.read_text(encoding="utf-8")
            + json.dumps(e1, ensure_ascii=False) + "\n"
            + "not-json\n",
            encoding="utf-8",
        )
        events = costs.read_events(run_a)
        summary = costs.summarize(events)
        check("台账去重、跳过坏行且不记零金额",
              len(events) == 2 and summary["total_cny"] == 1.5
              and summary["by_provider"]["cloud-video"] == 1.5,
              f"{len(events)}/{summary}")

        plain = Settings(values={"BUDGET_CNY": "3"})
        est = costs.estimate_video(10, plain)
        check("云端视频未接入时估价为 0、不拦本地出片",
              est["available"] is False and est["estimate_cny"] == 0.0
              and est["exceeded"] is False, str(est))

        configured = Settings(values={
            "VIDEO_API_BASE_URL": "http://example.invalid",
            "VIDEO_API_KEY": "test-key",
            "BUDGET_CNY": "3",
        })
        est = costs.estimate_video(10, configured)
        check("配置了端点但适配器未接入仍不算可用",
              est["configured"] is True and est["available"] is False
              and est["exceeded"] is False, str(est))

        original = CloudVideoProvider.available
        CloudVideoProvider.available = lambda self: (True, "测试：模拟云端可用")
        try:
            est = costs.estimate_video(10, configured)
            check("适配器可用后按单价估算",
                  est["available"] is True and est["estimate_cny"] == 5.0
                  and est["exceeded"] is True, str(est))
            try:
                susage.guard_budget(10, configured)
                check("超预算任务在提交前拒绝", False, "被接受了")
            except HTTPException as exc:
                check("超预算任务在提交前拒绝",
                      exc.status_code == 422 and "估价" in str(exc.detail)
                      and "拒绝提交" in str(exc.detail), f"status={exc.status_code}")
            for kwargs, label in (
                ({"painted": True}, "代码绘制"),
                ({"preview": True}, "低清预览"),
                ({"recompose": True}, "只重合成"),
            ):
                susage.guard_budget(60, configured, **kwargs)
                check(f"{label}不误伤预算闸", True, "")
            susage.guard_budget(2, configured)
            check("预算内的任务正常放行", True, "")
        finally:
            CloudVideoProvider.available = original

        susage._settings = lambda: Settings(values={
            "BUDGET_CNY": "4",
            "VIDEO_API_BASE_URL": "http://example.invalid",
            "VIDEO_API_KEY": "test-key",
        })
        catalog = susage.provider_catalog(fresh=True)
        by_id = {item["id"]: item for item in catalog["items"]}
        check("能力目录覆盖绘制/出图/视频/配音/文本",
              {"painted", "offline", "comfy", "cloud-image", "cloud-video",
               "tts-sapi", "tts-cloud-tts", "tts-silent", "llm"} <= set(by_id),
              str(sorted(by_id)))
        video = by_id["cloud-video"]
        check("未接入的云端视频不显示就绪",
              video["available"] is False and video["configured"] is True
              and "待接入" in video["detail"] and video["cost"] == "paid",
              f"{video['available']}/{video['detail']}")
        check("能力测试写明只探测不生成收费样片",
              all("仅探测" in item["test"] for item in catalog["items"]),
              "")
        check("本地离线出图在目录中真实可用",
              by_id["offline"]["available"] is True and "不计费" in by_id["offline"]["price_hint"],
              by_id["offline"]["detail"])

        mine = susage.usage(principal=owner)
        check("费用台账只统计自己的运行",
              len(mine["items"]) == 1 and mine["items"][0]["run_id"] == run_a.name
              and mine["items"][0]["total_cny"] == 1.5
              and mine["items"][0]["theme"] == "甲项目",
              str([(i["run_id"], i["total_cny"]) for i in mine["items"]]))
        theirs = susage.usage(principal=other)
        check("别人的费用不会串到当前账号",
              [item["run_id"] for item in theirs["items"]] == [run_b.name]
              and theirs["total_cny"] == 2.0,
              str([(i["run_id"], i["total_cny"]) for i in theirs["items"]]))
        nobody = susage.usage(principal=None)
        check("没有主体时不查台账",
              nobody["items"] == [] and "未查询" in nobody["note"], nobody["note"])

        from server.app import app
        paths = app.openapi()["paths"]
        check("生成器与费用接口已注册",
              "/api/providers" in paths and "/api/usage" in paths, "")

        app_src = (ROOT / "server" / "app.py").read_text(encoding="utf-8")
        plan_src = (ROOT / "server" / "plan.py").read_text(encoding="utf-8")
        pipe_src = (ROOT / "pipeline" / "pipeline.py").read_text(encoding="utf-8")
        check("创建与拍摄入口都接预算闸",
              "guard_budget(req.duration" in app_src and "guard_budget(" in plan_src, "")
        check("云端调用成功后才记费用",
              "record_cost(" in pipe_src and 'provider.name == "cloud"' in pipe_src, "")

        api = (ROOT / "web" / "src" / "lib" / "api.ts").read_text(encoding="utf-8")
        page = (ROOT / "web" / "src" / "pages" / "SettingsPage.tsx").read_text(encoding="utf-8")
        check("设置页有生成器与费用面板",
              "fetchProviders" in api and "fetchUsage" in api
              and 'title="生成器"' in page and 'title="费用台账"' in page
              and "重新探测" in page
              and "还没有付费调用记录" in page, "")
    finally:
        susage._settings = saved["settings"]
        shist.RUNS = saved["runs"]
        sauth.DB_PATH = saved["db"]
        shutil.rmtree(tmp, ignore_errors=True)


def test_clip_cache_palette() -> None:
    """F2 片段指纹 + F4 项目色板。

    这两件事必须一起验：色板改变配色，片段缓存必须因此失效；而只改镜长
    属于「只重合成」支持的编辑，不能被指纹误判成过期。
    """
    print("\n[33] 片段指纹与项目色板")
    import shutil
    import tempfile
    from pathlib import Path

    from fastapi import HTTPException
    from PIL import Image

    from pipeline import clipcache
    from pipeline import palette as palettes
    from pipeline.genre import pick_genre
    from pipeline.image_render import render_still
    from pipeline.models import ProjectBrief, Shot, Storyboard
    from server.plan import _render_profile
    from server.schemas import PaletteBody, RenderRequest

    import server.plan as splan

    tmp = Path(tempfile.mkdtemp(prefix="yiyan-cache-palette-"))
    saved = splan.ROOT
    try:
        brief = ProjectBrief(theme="一碗面", style="暖色纪实", aspect_ratio="16:9")
        shot = Shot(id=1, act="开场", duration=3.0, narration="热气", visual="面汤", camera="特写")
        board = Storyboard(project={}, shots=[shot])
        kw = dict(
            genre="food", palette="auto", preview=False,
            work_scale=2, still_scale=2.0, guides=False,
            providers=["kenburns:"], video_size=(1920, 1080),
        )
        key = clipcache.clip_key(shot, brief, board, **kw)
        check("同一输入的片段指纹稳定",
              key and key == clipcache.clip_key(shot, brief, board, **kw), key)

        shot.narration = "换了一句文案"
        changed = clipcache.clip_key(shot, brief, board, **kw)
        check("文案变化让片段指纹失效", changed != key, f"{key} → {changed}")

        shot.narration = "热气"
        shot.duration = 5.0
        shot.start = 10.0
        check("只改镜长/时间轴不变更片段指纹",
              clipcache.clip_key(shot, brief, board, **kw) == key, "")
        check("换色板让片段指纹失效",
              clipcache.clip_key(shot, brief, board, **{**kw, "palette": "cool"}) != key, "")
        check("换预览档位/安全区让指纹失效",
              clipcache.clip_key(shot, brief, board, **{**kw, "preview": True}) != key
              and clipcache.clip_key(shot, brief, board, **{**kw, "guides": True}) != key, "")

        clip = tmp / "shot01.mp4"
        check("没有片段时状态是 missing",
              clipcache.state(clip, key) == "missing", "")
        clip.write_bytes(b"fake")
        check("旧片段没有指纹时状态是 unknown",
              clipcache.state(clip, key) == "unknown", "")
        clipcache.write_key(clip, key, provider="kenburns")
        check("写入指纹后状态是 current",
              clipcache.state(clip, key) == "current"
              and clipcache.read_key(clip) == key, clipcache.read_key(clip))
        check("输入变化后状态是 stale",
              clipcache.state(clip, "another-key") == "stale", "")

        base = pick_genre("一碗热汤面 烟火气")
        film = palettes.apply(base, "film")
        cool = palettes.apply(base, "cool")
        check("色板只换配色，不换题材",
              film.key == base.key and film.label == base.label
              and film.palette != base.palette and cool.palette != film.palette, film.key)
        shot.duration = 3.0
        shot.start = 0.0
        p1 = render_still(shot, brief, film, tmp / "warm.png", with_text=False, scale=0.15)
        p2 = render_still(shot, brief, cool, tmp / "cool.png", with_text=False, scale=0.15)
        with Image.open(p1) as a, Image.open(p2) as b:
            mean_a = a.convert("RGB").resize((1, 1)).getpixel((0, 0))
            mean_b = b.convert("RGB").resize((1, 1)).getpixel((0, 0))
        check("不同色板真的产出不同画面",
              p1.read_bytes() != p2.read_bytes() and mean_a != mean_b,
              f"{mean_a} vs {mean_b}")

        run_id = "20261116-210000-色板-test"
        plan_dir = tmp / "runs" / run_id / "plan"
        plan_dir.mkdir(parents=True)
        (plan_dir / "meta.json").write_text(
            json.dumps({"revision": 1, "render_profile": {"palette": "auto"}}),
            encoding="utf-8",
        )
        splan.ROOT = tmp
        out = splan.set_palette(run_id, PaletteBody(palette="cool", expected_revision=1), None)
        meta = json.loads((plan_dir / "meta.json").read_text(encoding="utf-8"))
        check("色板保存递增版本并写入渲染配置",
              out["revision"] == 2 and meta["palette"] == "cool"
              and meta["render_profile"]["palette"] == "cool", str(out))
        check("拍摄时默认继承项目色板",
              _render_profile(meta, RenderRequest())[0]["palette"] == "cool", "")
        check("拍摄请求可显式覆盖色板",
              _render_profile(meta, RenderRequest(palette="ink"))[0]["palette"] == "ink", "")
        try:
            splan.set_palette(run_id, PaletteBody(palette="warm", expected_revision=1), None)
            check("色板保存遵循版本冲突", False, "被接受了")
        except HTTPException as exc:
            check("色板保存遵循版本冲突", exc.status_code == 409, f"status={exc.status_code}")

        from server.app import app
        paths = app.openapi()["paths"]
        check("色板接口已注册",
              "/api/palettes" in paths and "/api/plan/{run_id}/palette" in paths, "")

        api = (ROOT / "web" / "src" / "lib" / "api.ts").read_text(encoding="utf-8")
        create_page = (ROOT / "web" / "src" / "pages" / "CreatePage.tsx").read_text(encoding="utf-8")
        plan_page = (ROOT / "web" / "src" / "pages" / "PlanPage.tsx").read_text(encoding="utf-8")
        check("前后端都有色板入口",
              "savePalette" in api and "PALETTE_OPTIONS" in create_page
              and "视觉色板" in plan_page, "")
        pipe_src = (ROOT / "pipeline" / "pipeline.py").read_text(encoding="utf-8")
        check("片段复用与过期拦截都在管线里",
              "clip_state(clip_path, key) == \"current\"" in pipe_src
              and "缺少缓存指纹" in pipe_src and "片段还是旧的" in pipe_src, "")
    finally:
        splan.ROOT = saved
        shutil.rmtree(tmp, ignore_errors=True)


def test_vision_review() -> None:
    """F3：真实视觉评审必须真的把图发给 VLM，并用 VLM_MODEL。"""
    print("\n[34] 真实视觉评审链路")
    import base64
    import io
    import shutil
    import tempfile
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from pathlib import Path
    from types import SimpleNamespace

    from PIL import Image

    from pipeline.config import Settings
    from pipeline.llm import OpenAIChatClient
    from pipeline.models import ProjectBrief, Shot
    from pipeline.pipeline import Pipeline
    from pipeline.prompts import PromptLibrary

    state: dict[str, object] = {"payload": None, "fail": False}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            state["payload"] = json.loads(self.rfile.read(length).decode("utf-8"))
            if state["fail"]:
                body = b'{"error":{"message":"no vision support"}}'
                self.send_response(400)
            else:
                body = json.dumps({
                    "choices": [{"message": {"content": '{"quality": 4, "verdict": "approved", "note": "画面清楚"}'}}],
                }).encode("utf-8")
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
    tmp = Path(tempfile.mkdtemp(prefix="yiyan-vlm-"))
    try:
        image = tmp / "still.png"
        photo = Image.new("RGB", (96, 64))
        pixels = photo.load()
        for x in range(96):
            for y in range(64):
                pixels[x, y] = (x * 2, y * 3, (x + y) * 2)
        photo.save(image, "PNG")

        client = OpenAIChatClient(
            base_url=f"http://127.0.0.1:{port}/v1", api_key="test", model="test-vl")
        text = client.chat_vision("系统提示", "看看这张图", image, temperature=0.1)
        payload = state["payload"] or {}
        content = ((payload.get("messages") or [{}])[-1].get("content") or [])
        image_part = next((part for part in content if part.get("type") == "image_url"), {})
        encoded = base64.b64encode(image.read_bytes()).decode("ascii")
        check("视觉请求使用 VLM 模型名",
              payload.get("model") == "test-vl", str(payload.get("model")))
        check("视觉请求真的带上图片数据",
              image_part.get("image_url", {}).get("url", "").endswith(encoded)
              and image_part.get("image_url", {}).get("url", "").startswith("data:image/png;base64,"),
              str(image_part)[:80])
        check("视觉响应能取回内容", "approved" in text, text[:60])

        settings = Settings(values={
            "VLM_MODEL": "test-vl",
            "VLM_BASE_URL": f"http://127.0.0.1:{port}/v1",
            "VLM_API_KEY": "test",
        })
        fake = SimpleNamespace(
            settings=settings,
            ctx=SimpleNamespace(live=True, notes=[]),
            lib=PromptLibrary.load(),
            log=SimpleNamespace(error=lambda *a, **k: None),
            _vlm_client=None,
        )
        fake._vision_client = Pipeline._vision_client.__get__(fake)
        describe = Pipeline._describe.__get__(fake)
        shot = Shot(id=1, act="开场", duration=3, narration="", visual="面汤",
                    camera="特写", acceptance="主体清楚")
        brief = ProjectBrief(theme="一碗面", style="暖色纪实")
        data = json.loads(describe(image, shot, brief, 1))
        check("VLM 成功时标记为真实视觉评审",
              data.get("review_mode") == "vlm" and data.get("visual_check") == "performed"
              and data.get("technical", {}).get("ok") is True,
              str({k: data.get(k) for k in ("review_mode", "visual_check")}))

        state["fail"] = True
        fake._vlm_client = None
        data = json.loads(describe(image, shot, brief, 1))
        check("VLM 失败时明确降级为模拟评审",
              data.get("review_mode") == "simulated"
              and data.get("visual_check") == "not_performed"
              and any("模拟评审" in note for note in fake.ctx.notes),
              str(data.get("review_mode")))
    finally:
        srv.shutdown()
        srv.server_close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_brand_font_version() -> None:
    """F7：品牌缓存指纹必须包含字体版本。"""
    print("\n[35] 品牌字体版本")
    import shutil
    import tempfile
    from pathlib import Path

    from pipeline import branding as pbrand

    tmp = Path(tempfile.mkdtemp(prefix="yiyan-font-"))
    saved = pbrand.FONT_CANDIDATES
    try:
        font_a = tmp / "a.ttf"
        font_b = tmp / "b.ttf"
        font_a.write_bytes(b"a" * 100)
        font_b.write_bytes(b"b" * 200)
        brand = pbrand.normalize({"name": "品牌"})
        pbrand.FONT_CANDIDATES = (str(font_a),)
        fp_a = pbrand.fingerprint(brand, tmp)
        pbrand.FONT_CANDIDATES = (str(font_b),)
        fp_b = pbrand.fingerprint(brand, tmp)
        pbrand.FONT_CANDIDATES = (str(font_a),)
        fp_a2 = pbrand.fingerprint(brand, tmp)
        check("品牌指纹随字体文件变化",
              fp_a and fp_a == fp_a2 and fp_a != fp_b, f"{fp_a}/{fp_b}")
        check("字体指纹函数可读", bool(pbrand.font_fingerprint()), pbrand.font_fingerprint())
    finally:
        pbrand.FONT_CANDIDATES = saved
        shutil.rmtree(tmp, ignore_errors=True)


def test_mux_picture_authority() -> None:
    """画面长度是权威：配音短于画面时不能把画面一起截断。

    旧实现用 `-shortest`，最后一镜没台词或 TTS 提前收尾时，成片会凭空少几秒。
    """
    print("\n[38] 混音画面长度权威")
    import shutil

    from pipeline.compose import mux_audio
    from pipeline.video import probe_duration, run_ffmpeg

    tmp = Path(tempfile.mkdtemp(prefix="yiyan-mux-"))
    try:
        picture = tmp / "picture.mp4"
        run_ffmpeg([
            "-f", "lavfi", "-i", "color=c=blue:s=160x90:d=3:r=25",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(picture),
        ])
        short_voice = tmp / "short.m4a"
        run_ffmpeg([
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-c:a", "aac", str(short_voice),
        ])
        long_voice = tmp / "long.m4a"
        run_ffmpeg([
            "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
            "-c:a", "aac", str(long_voice),
        ])

        out_short = tmp / "out_short.mp4"
        _, has_audio = mux_audio(picture, short_voice, out_short, 3.0)
        got = probe_duration(out_short)
        check("配音短于画面时画面不被截断",
              has_audio and abs(got - 3.0) < 0.3, f"{got}")

        out_long = tmp / "out_long.mp4"
        mux_audio(picture, long_voice, out_long, 3.0)
        got_long = probe_duration(out_long)
        check("配音长于画面时以画面为准",
              abs(got_long - 3.0) < 0.3, f"{got_long}")

        out_silent = tmp / "out_silent.mp4"
        _, silent_flag = mux_audio(picture, None, out_silent, 3.0)
        got_silent = probe_duration(out_silent)
        check("无配音铺等长静音轨",
              not silent_flag and abs(got_silent - 3.0) < 0.3, f"{got_silent}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_iter3_artifacts() -> None:
    """第三次迭代：旧项目母版回填与导出缓存键。

    旧项目只有 output/final.mp4，没有 manifest。不回填的话，内容哈希与
    artifact_id 永远是空的——导出缓存键和审计追溯对这批项目全失效。
    """
    print("\n[39] 旧项目母版回填")
    import hashlib
    import json
    import shutil
    import threading

    from server import artifacts

    tmp = Path(tempfile.mkdtemp(prefix="yiyan-backfill-"))

    def sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    try:
        run = tmp / "runs" / "20260101-000000-旧项目"
        (run / "output").mkdir(parents=True)
        legacy = run / "output" / "final.mp4"
        legacy.write_bytes(b"legacy-video-bytes")
        manifest_file = run / "output" / artifacts.MASTER_FILE

        check("回填前没有 manifest", not manifest_file.exists(), "")
        got = artifacts.backfill_master(run)
        check("回填补出 legacy manifest",
              manifest_file.is_file() and bool(got) and got.get("legacy") is True, str(got))
        check("回填哈希等于成片实算",
              bool(got) and got.get("sha256") == sha(legacy), str(got and got.get("sha256")))
        check("回填 artifact_id 稳定",
              bool(got) and got.get("artifact_id") == "legacy-20260101-000000-旧项目",
              str(got and got.get("artifact_id")))

        first_mtime = manifest_file.stat().st_mtime_ns
        again = artifacts.backfill_master(run)
        check("二次回填不重写",
              manifest_file.stat().st_mtime_ns == first_mtime
              and again.get("sha256") == got.get("sha256"), "")

        # 已有真实 manifest 时不许被 legacy 覆盖。
        real = tmp / "runs" / "real"
        (real / "output").mkdir(parents=True)
        (real / "output" / "final.mp4").write_bytes(b"x")
        published = artifacts.publish_master(real, "output/final.mp4", backend="comfy")
        artifacts.backfill_master(real)
        check("已有 manifest 不被回填覆盖",
              artifacts.current_manifest(real).get("artifact_id") == published["artifact_id"], "")

        # 成片缺失时不写空 manifest。
        empty = tmp / "runs" / "empty"
        (empty / "output").mkdir(parents=True)
        check("成片缺失时不写 manifest",
              artifacts.backfill_master(empty) is None
              and not (empty / "output" / artifacts.MASTER_FILE).exists(), "")

        # 损坏的 manifest 不能被悄悄替换——那是"文件坏了"，该暴露。
        broken = tmp / "runs" / "broken"
        (broken / "output").mkdir(parents=True)
        (broken / "output" / "final.mp4").write_bytes(b"y")
        (broken / "output" / artifacts.MASTER_FILE).write_text("{not json", encoding="utf-8")
        artifacts.backfill_master(broken)
        check("损坏的 manifest 不被覆盖",
              (broken / "output" / artifacts.MASTER_FILE).read_text(encoding="utf-8") == "{not json", "")

        # 并发读只应写出一份 manifest。
        conc = tmp / "runs" / "concurrent"
        (conc / "output").mkdir(parents=True)
        (conc / "output" / "final.mp4").write_bytes(b"z" * 4096)
        results: list[dict] = []
        lock = threading.Lock()

        def worker() -> None:
            value = artifacts.backfill_master(conc)
            with lock:
                results.append(value or {})

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        ids = {str(item.get("artifact_id")) for item in results}
        payload = json.loads((conc / "output" / artifacts.MASTER_FILE).read_text(encoding="utf-8"))
        check("并发回填只产出一份 manifest",
              len(ids) == 1 and payload.get("artifact_id") == "legacy-concurrent", f"{ids}")

        # 读接口不得写用户项目：补清单只能由显式迁移触发。
        readonly = tmp / "runs" / "readonly"
        (readonly / "output").mkdir(parents=True)
        (readonly / "output" / "final.mp4").write_bytes(b"r" * 64)
        view = artifacts.current_master(readonly)
        check("只读访问不为旧项目写 manifest",
              bool(view) and view.get("kind") == "legacy"
              and not (readonly / "output" / artifacts.MASTER_FILE).exists(), str(view))

        # ---- I3-B2 导出缓存键 ----
        from server import exports as sexp

        content_a = run / "output" / "a.mp4"
        content_b = run / "output" / "b.mp4"
        content_a.write_bytes(b"A" * 128)
        content_b.write_bytes(b"B" * 128)
        import os as _os
        stamp = (1_600_000_000_000_000_000, 1_600_000_000_000_000_000)
        _os.utime(content_a, ns=stamp)
        _os.utime(content_b, ns=stamp)
        fp_a = sexp._fingerprint(content_a, "", run)
        fp_b = sexp._fingerprint(content_b, "", run)
        check("同尺寸同 mtime 的不同内容指纹不同", fp_a != fp_b, f"{fp_a} / {fp_b}")
        _os.utime(content_a, ns=(stamp[0], stamp[1] + 10_000_000_000))
        check("内容相同 mtime 变化指纹不变",
              sexp._fingerprint(content_a, "", run) == fp_a, "")
        check("品牌指纹参与导出缓存键",
              sexp._fingerprint(content_a, "brand1", run)
              != sexp._fingerprint(content_a, "brand2", run), "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_iter2_backend() -> None:
    """第二轮审计 8 个后端/执行链发现的最小行为回归。"""
    print("\n[36] 第二轮：归属、母版、证据、快照、缓存、字幕")
    import hashlib
    import inspect
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path
    from types import SimpleNamespace

    from fastapi import HTTPException
    from PIL import Image

    import pipeline.config as pconfig
    import pipeline.pipeline as ppipe
    import server.app as sapp
    import server.auth as sauth
    import server.history as shist
    import server.jobs as sjobs
    import server.plan as splan
    from app.engine import Engine, RunOptions
    from pipeline import clipcache, gencache
    from pipeline.compose import overlay_text_layers
    from pipeline.genre import pick_genre
    from pipeline.image_render import render_subtitle_card
    from pipeline.models import ProjectBrief, Shot, Storyboard, storyboard_from_dict
    from pipeline.pipeline import Pipeline
    from pipeline.video import FFMPEG
    from server import artifacts
    from server.auth import OwnershipUnavailable, Principal
    from server.schemas import RenderRequest, RunRequest, SubtitlesBody

    tmp = Path(tempfile.mkdtemp(prefix="yiyan-iter2-"))
    saved = {
        "root": splan.ROOT,
        "config_root": pconfig.ROOT,
        "runs": shist.RUNS,
        "db": sauth.DB_PATH,
        "registry": splan.REGISTRY,
        "app_registry": sapp.REGISTRY,
    }
    try:
        splan.ROOT = tmp
        pconfig.ROOT = tmp
        shist.RUNS = tmp / "runs"
        sauth.DB_PATH = tmp / "work" / "yiyan.db"
        sauth.init_db()

        owner = Principal(kind="guest", id="a" * 32)

        # ---- I2-BE-01：归属失败必须在生成前中止
        original_record = sauth.record_owner
        sauth.record_owner = lambda *a, **k: (_ for _ in ()).throw(
            OwnershipUnavailable("db down"))
        job = sjobs.Job("归属测试", RunOptions(theme="归属测试"), principal=owner)
        try:
            try:
                job._on_start("run-owner-fail")
                check("归属失败向上抛并中止任务", False, "没有抛异常")
            except OwnershipUnavailable:
                check("归属失败向上抛并中止任务", True, "")
        finally:
            sauth.record_owner = original_record

        worker_src = inspect.getsource(Engine._worker)
        check("on_start 在 ctx.prepare 之前",
              worker_src.index("self.on_start(ctx.run_id)") < worker_src.index("ctx.prepare()"), "")
        check("Engine 不再吞掉归属前置异常",
              "self.on_start(ctx.run_id)\n            except" not in worker_src, "")

        class ReserveOnly:
            def __init__(self) -> None:
                self.token = ""

            def reserve(self) -> str:
                self.token = "tok"
                return self.token

            def release(self, _token: str) -> None:
                pass

            def create(self, *a, **k):  # pragma: no cover - 不应被调用
                raise AssertionError("归属失败后不应创建任务")

        sapp.REGISTRY = ReserveOnly()
        original_app_record = sapp.record_owner
        sapp.record_owner = lambda *a, **k: (_ for _ in ()).throw(
            OwnershipUnavailable("db down"))
        runs_before = {p.name for p in (tmp / "runs").iterdir()} if (tmp / "runs").is_dir() else set()
        try:
            sapp.create_run(RunRequest(theme="归属失败测试"), owner)
            check("创建任务归属失败返回 503", False, "被接受了")
        except HTTPException as exc:
            runs_after = {p.name for p in (tmp / "runs").iterdir()} if (tmp / "runs").is_dir() else set()
            check("创建任务归属失败返回 503", exc.status_code == 503, f"status={exc.status_code}")
            check("归属失败不留下空项目目录", runs_after == runs_before, str(runs_after - runs_before))
        finally:
            sapp.record_owner = original_app_record

        reg = sjobs.JobRegistry()
        token = reg.reserve()
        try:
            try:
                reg.reserve()
                check("任务预留阻止第二条请求", False, "被接受了")
            except sjobs.BusyError:
                check("任务预留阻止第二条请求", True, "")
            try:
                reg.create("x", RunOptions(theme="x"), principal=owner, reservation="wrong")
                check("错误预留令牌不能创建任务", False, "被接受了")
            except sjobs.BusyError:
                check("错误预留令牌不能创建任务", True, "")
        finally:
            reg.release(token)

        # ---- I2-BE-03：唯一母版
        run = tmp / "runs" / "master-test"
        (run / "output").mkdir(parents=True)
        (run / "painted").mkdir()
        base = run / "output" / "final.mp4"
        painted = run / "painted" / "painted.mp4"
        base.write_bytes(b"base-video")
        painted.write_bytes(b"painted-video")
        check("旧项目无 manifest 时回落到 output/final.mp4",
              artifacts.current_video(run) == base, str(artifacts.current_video(run)))
        manifest = artifacts.publish_master(
            run, painted, backend="painted", job_id="job-1", snapshot_id="snap-1")
        check("发布母版后解析到绘制版",
              artifacts.current_video(run) == painted.resolve()
              and manifest["backend"] == "painted", str(manifest))
        check("母版 manifest 记录任务与快照",
              manifest["job_id"] == "job-1" and manifest["snapshot_id"] == "snap-1", "")
        outside = tmp / "outside.mp4"
        outside.write_bytes(b"outside")
        try:
            artifacts.publish_master(run, outside)
            check("母版必须位于项目目录内", False, "被接受了")
        except ValueError:
            check("母版必须位于项目目录内", True, "")
        painted.unlink()
        check("manifest 目标丢失时不偷偷回落到基础版",
              artifacts.current_video(run) is None, str(artifacts.current_video(run)))

        job2 = sjobs.Job("发布", RunOptions(theme="发布", snapshot_id="snap-2"), principal=None)
        job2.result = {
            "project_dir": str(run),
            "compose": {"video": str(base), "backend": "kenburns"},
            "notes": [],
        }
        job2._publish_master()
        check("任务完成时发布当前母版",
              artifacts.current_video(run) == base.resolve()
              and job2.result.get("master", {}).get("job_id") == job2.job_id, "")

        (run / "output" / "storyboard.json").write_text(
            json.dumps({"project": {"title": "母版"}, "shots": []}), encoding="utf-8")
        detail = shist.run_detail(run.name, None)
        check("历史详情读取当前母版",
              detail["has_video"] and detail["master"]["job_id"] == job2.job_id, str(detail))

        # ---- I2-BE-02：已选生成图进入本地视频
        selected = tmp / "selected.png"
        Image.new("RGB", (64, 64), (200, 120, 40)).save(selected, "PNG")
        captured: dict[str, Path] = {}

        class KenBurns:
            name = "kenburns"

            def available(self):
                return True, ""

            def make_clip(self, shot, still, out_path, duration, overlay=None, work=2):
                captured["still"] = Path(still)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_bytes(b"clip")
                return out_path

        brief = ProjectBrief(theme="选图", aspect_ratio="16:9")
        shot = Shot(id=1, act="A", duration=2.0, narration="字幕", visual="面", camera="特写")
        shot.frame_path = str(selected)
        shot.frame_is_clean = True
        shot.frame_source = "comfy"
        shot.image_sha256 = "sha-selected"
        board = Storyboard(project={}, shots=[shot])
        fake = SimpleNamespace(
            ctx=SimpleNamespace(work_dir=tmp / "work", project_dir=run),
            log=SimpleNamespace(event=lambda *a, **k: None, error=lambda *a, **k: None),
            genre=SimpleNamespace(key="food"), palette_id="auto", preview=False,
            work_scale=2, still_scale=2.0, guides=False, video_chain=[KenBurns()],
            no_cache=False, only_shot=0,
            _clip_key=lambda *a, **k: "selected-key",
        )
        Pipeline._make_clip(fake, shot, brief, board)
        check("本地视频使用已选生成图而不是重画占位背景",
              captured.get("still") == selected, str(captured.get("still")))

        clean = tmp / "clean.png"
        Image.new("RGB", (32, 32), (10, 10, 10)).save(clean, "PNG")
        original_render = ppipe.render_still
        ppipe.render_still = lambda *a, **k: clean
        try:
            fake.no_cache = True
            shot.frame_path = str(selected)
            shot.frame_is_clean = False
            shot.frame_source = "offline"
            Pipeline._make_clip(fake, shot, brief, board)
            check("离线占位帧仍重画无字背景",
                  captured.get("still") == clean, str(captured.get("still")))
        finally:
            ppipe.render_still = original_render
            fake.no_cache = False

        # ---- I2-BE-04：候选证据与选中图绑定
        desc = json.dumps({
            "technical": {"ok": True, "sha256": "sha-1", "width": 64, "height": 64,
                          "mean": 110.0, "contrast": 30.0},
            "review_mode": "vlm", "visual_check": "performed", "review_rule": "r1",
        })
        evidence = Pipeline._candidate_evidence(
            SimpleNamespace(), shot, selected, desc, {"review_mode": "vlm"},
            4, "accept", "none", "comfy", 1,
        )
        chosen = Shot(id=1, act="A", duration=2.0, narration="", visual="", camera="")
        chosen.candidate_evidence = [evidence]
        Pipeline._apply_evidence(SimpleNamespace(), chosen, evidence)
        check("选中候选完整绑定证据",
              chosen.image_sha256 == "sha-1" and chosen.evidence_id == evidence["evidence_id"]
              and chosen.frame_is_clean and chosen.tech_ok is True,
              f"{chosen.image_sha256}/{chosen.evidence_id}")
        view = shist._review_shot(chosen.to_dict(), None)
        check("复核接口返回 evidence_id 与候选列表",
              view["evidence_id"] == evidence["evidence_id"]
              and len(view["candidates"]) == 1, str(view))
        modes = []
        for mode in ("vlm", "simulated", "technical_failed"):
            item = Shot(id=1, act="A", duration=1.0, narration="", visual="", camera="")
            item.review_mode = mode
            item.tech_ok = mode != "technical_failed"
            modes.append(shist._review_shot(item.to_dict(), None))
        summary = shist._review_summary(modes)
        check("混合质检汇总按模式计数",
              summary["modes"].get("vlm") == 1
              and summary["modes"].get("simulated") == 1
              and summary["modes"].get("technical_failed") == 1, str(summary["modes"]))

        # ---- I2-BE-05：不可变快照
        snap_plan = tmp / "runs" / "snap-test" / "plan"
        snap_plan.mkdir(parents=True)
        for name, payload in (
            ("brief.json", {"theme": "快照", "total_duration": 3}),
            ("script.json", {"title": "快照"}),
            ("storyboard.json", {"project": {"total_duration": 3}, "subjects": [],
                                 "shots": [{"id": 1, "act": "A", "duration": 3,
                                            "narration": "x", "visual": "x", "camera": "x"}]}),
            ("meta.json", {"revision": 0}),
        ):
            (snap_plan / name).write_text(json.dumps(payload), encoding="utf-8")
        snap_a = splan._freeze_plan(snap_plan, 0, {"live": False}, None, "aaaa")
        snap_b = splan._freeze_plan(snap_plan, 0, {"live": False}, None, "bbbb")
        digest_a = hashlib.sha256((snap_a / "storyboard.json").read_bytes()).hexdigest()
        check("同版本两次冻结使用不同快照目录",
              snap_a != snap_b and snap_a.is_dir() and snap_b.is_dir(), f"{snap_a.name}/{snap_b.name}")
        check("后一次冻结不改写前一次快照",
              hashlib.sha256((snap_a / "storyboard.json").read_bytes()).hexdigest() == digest_a, "")
        snap_manifest = json.loads((snap_a / "snapshot.json").read_text(encoding="utf-8"))
        check("快照 manifest 记录文件摘要",
              snap_manifest["snapshot_id"] == "aaaa" and "storyboard.json" in snap_manifest["files"], "")

        active_dir = snap_plan / "snapshots" / "snap-active"
        active_dir.mkdir()
        for index in range(12):
            (snap_plan / "snapshots" / f"snap-old-{index:02d}").mkdir()

        class ActiveRegistry:
            def active_snapshot_ids(self):
                return {"active"}

        splan.REGISTRY = ActiveRegistry()
        try:
            splan._freeze_plan(snap_plan, 0, {"live": False}, None, "new-one")
            check("清理旧快照时保留活动任务引用",
                  active_dir.is_dir(), "活动快照被删除")
        finally:
            splan.REGISTRY = saved["registry"]

        busy_plan = tmp / "runs" / "busy-test"
        (busy_plan / "plan").mkdir(parents=True)
        (busy_plan / "output").mkdir()
        (busy_plan / "plan" / "brief.json").write_text(
            json.dumps({"theme": "忙碌", "total_duration": 3}), encoding="utf-8")
        (busy_plan / "plan" / "script.json").write_text(json.dumps({"title": "忙碌"}), encoding="utf-8")
        (busy_plan / "plan" / "storyboard.json").write_text(json.dumps({
            "project": {"total_duration": 3}, "subjects": [],
            "shots": [{"id": 1, "act": "A", "duration": 3, "narration": "x",
                       "visual": "x", "camera": "x"}],
        }), encoding="utf-8")
        (busy_plan / "plan" / "meta.json").write_text(
            json.dumps({"revision": 0, "render_profile": {}}), encoding="utf-8")

        class BusyRegistry:
            def reserve(self):
                raise sjobs.BusyError("busy")

        splan.REGISTRY = BusyRegistry()
        try:
            try:
                splan._start_render(busy_plan.name, RenderRequest(), None)
                check("忙碌拒绝不写快照", False, "被接受了")
            except HTTPException as exc:
                snap_dir = busy_plan / "plan" / "snapshots"
                check("忙碌拒绝不写快照",
                      exc.status_code == 409 and not snap_dir.exists(), str(exc.status_code))
        finally:
            splan.REGISTRY = saved["registry"]

        # ---- I2-BE-06：分层缓存依赖
        frame_base = {"guides": False, "scale": 1.0, "total_shots": 4}
        sig_args = dict(
            shot_id=1, prompt="角色一致性：红围裙", visual_locks=["红围裙"],
            brief_key="b", genre="food", palette="auto", preview=False,
            size=(640, 360), seed=1, candidates=1,
            provider_identity=["comfy:http://127.0.0.1:8188:txt2img.json:640"],
            workflow_fingerprint="wf1", reference_hashes={},
            frame_inputs=frame_base,
        )
        sig = gencache.signature(**sig_args)
        check("生成签名稳定", sig == gencache.signature(**sig_args), sig)
        check("视觉锁定变化让生成签名失效",
              gencache.signature(**{**sig_args, "prompt": "角色一致性：蓝围裙",
                                    "visual_locks": ["蓝围裙"]}) != sig, "")
        check("工作流/模型变化让生成签名失效",
              gencache.signature(**{**sig_args, "workflow_fingerprint": "wf2"}) != sig
              and gencache.signature(**{**sig_args,
                                        "provider_identity": ["offline:"]}) != sig, "")
        check("辅助线/倍率/总镜数变化让生成签名失效",
              gencache.signature(**{**sig_args, "frame_inputs": {**frame_base, "guides": True}}) != sig
              and gencache.signature(**{**sig_args, "frame_inputs": {**frame_base, "scale": 0.5}}) != sig
              and gencache.signature(**{**sig_args, "frame_inputs": {**frame_base, "total_shots": 5}}) != sig, "")
        check("离线占位帧文案变化让生成签名失效",
              gencache.signature(**{**sig_args, "frame_inputs": {
                  **frame_base, "text": {"caption": "旧词"}}}) != sig
              and gencache.signature(**{**sig_args, "frame_inputs": {
                  **frame_base, "text": {"caption": "新词"}}}) != gencache.signature(
                  **{**sig_args, "frame_inputs": {**frame_base, "text": {"caption": "旧词"}}}), "")

        # 行为验证：_frame_inputs 只在链里有 offline 时纳入文案，且不含时间轴。
        class _FakeProvider:
            def __init__(self, name):
                self.name = name
                self.provider_id = ""

        offline_pipe = object.__new__(Pipeline)
        offline_pipe.guides = True
        offline_pipe.fixture_scale = 0.5
        offline_pipe.image_chain = [_FakeProvider("offline")]
        fi_off = Pipeline._frame_inputs(offline_pipe, shot, board)
        check("离线链纳入文案与绘制参数",
              fi_off["guides"] is True and fi_off["scale"] == 0.5
              and fi_off["total_shots"] == len(board.shots)
              and "text" in fi_off and "start" not in fi_off and "end" not in fi_off,
              str(fi_off))
        shot.caption = "改过的歌词"
        fi_off2 = Pipeline._frame_inputs(offline_pipe, shot, board)
        check("离线文案改动进入生成签名",
              gencache.signature(**{**sig_args, "frame_inputs": fi_off})
              != gencache.signature(**{**sig_args, "frame_inputs": fi_off2}), "")
        shot.start = 12.0
        check("时间轴改动不进入生成签名",
              Pipeline._frame_inputs(offline_pipe, shot, board) == fi_off2, "")
        shot.start = 0.0
        offline_pipe.image_chain = [_FakeProvider("comfy")]
        fi_cloud = Pipeline._frame_inputs(offline_pipe, shot, board)
        check("真实出图链不因文案改动失效", "text" not in fi_cloud, str(fi_cloud))
        shot.caption = ""
        cdir = tmp / "gencache"
        image = tmp / "gen.png"
        Image.new("RGB", (32, 32), (80, 80, 80)).save(image, "PNG")
        record = gencache.store(cdir, 1, "k1", image, provider="comfy", quality=4,
                                status="approved", evidence={"evidence_id": "e1"},
                                candidates=[])
        hit = gencache.load(cdir, 1, "k1")
        check("生成缓存命中并校验图像摘要",
              record and hit and hit["evidence"]["evidence_id"] == "e1", str(hit))
        Path(record["image"]).write_bytes(b"corrupt")
        check("缓存图像被改坏时按未命中处理", gencache.load(cdir, 1, "k1") is None, "")

        clip_args = dict(
            genre="food", palette="auto", preview=False, work_scale=2,
            still_scale=2.0, guides=False, providers=["kenburns:"], video_size=(1920, 1080),
        )
        k1 = clipcache.clip_key(shot, brief, board, **clip_args,
                                image_sha256="sha1", effective_prompt="p1",
                                workflow_fingerprint="wf1")
        k2 = clipcache.clip_key(shot, brief, board, **clip_args,
                                image_sha256="sha2", effective_prompt="p1",
                                workflow_fingerprint="wf1")
        shot.duration = 9.0
        k3 = clipcache.clip_key(shot, brief, board, **clip_args,
                                image_sha256="sha1", effective_prompt="p1",
                                workflow_fingerprint="wf1")
        check("片段指纹纳入选中图与实际提示词",
              k1 != k2 and clipcache.clip_key(
                  shot, brief, board, **clip_args, image_sha256="sha1",
                  effective_prompt="p2", workflow_fingerprint="wf1") != k1, "")
        check("片段指纹仍排除时间轴镜长", k1 == k3, f"{k1}/{k3}")
        process_src = inspect.getsource(Pipeline._process_shot)
        check("生成缓存检查发生在出图之前",
              process_src.index("gcache.load") < process_src.index("generated = self._generate_still"), "")

        # ---- I3-BE-01：复用路径必须恢复上次拍摄证据，否则指纹永远判 stale
        reuse_root = tmp / "reuse"
        (reuse_root / "work" / "clips").mkdir(parents=True, exist_ok=True)
        (reuse_root / "output").mkdir(parents=True, exist_ok=True)
        reuse_shot = Shot(id=1, act="A", duration=3.0, narration="热气", visual="面汤",
                          camera="特写", prompt_image="p", acceptance="清晰")
        reuse_shot.image_sha256 = "sha-reuse"
        reuse_shot.evidence_id = "ev-reuse"
        reuse_board = Storyboard(project={}, shots=[reuse_shot])
        reuse_pipe = SimpleNamespace(
            genre=pick_genre("面"), palette_id="auto", preview=False,
            work_scale=2, still_scale=2.0, fixture_scale=1.0, guides=False,
            video_chain=[], image_chain=[], _video_size=(1920, 1080),
            _workflow_hash="wf-reuse", only_shot=0,
            ctx=SimpleNamespace(work_dir=reuse_root / "work", project_dir=reuse_root,
                                out_dir=reuse_root / "output", notes=[]),
            log=SimpleNamespace(event=lambda *a, **k: None),
            _locked_prompt=lambda shot_obj, _board: (shot_obj, []),
            _workflow_fingerprint=lambda: "wf-reuse",
            _reference_hashes=lambda _shot, _board: {},
            _REUSE_STATE_FIELDS=Pipeline._REUSE_STATE_FIELDS,
        )
        reuse_clip = reuse_root / "work" / "clips" / "shot01.mp4"
        reuse_clip.write_bytes(b"fake-clip")
        clipcache.write_key(reuse_clip, Pipeline._clip_key(reuse_pipe, reuse_shot, brief, reuse_board))
        plan_dict = json.loads(json.dumps(reuse_board.to_dict(), ensure_ascii=False))
        plan_dict["shots"][0].pop("image_sha256", None)
        reloaded_board = storyboard_from_dict(plan_dict)
        check("读回分镜会清空选中图摘要",
              reloaded_board.shots[0].image_sha256 == "", "")
        check("未恢复证据时旧片段被判过期",
              clipcache.state(reuse_clip, Pipeline._clip_key(
                  reuse_pipe, reloaded_board.shots[0], brief, reloaded_board)) == "stale", "")
        # 证据文件缺失：如实拒绝并留下说明，不静默放行。
        Pipeline._restore_reuse_state(reuse_pipe, reloaded_board)
        check("证据缺失时不放行并记录原因",
              reloaded_board.shots[0].image_sha256 == ""
              and any("output/storyboard.json" in n for n in reuse_pipe.ctx.notes),
              str(reuse_pipe.ctx.notes))
        (reuse_root / "output" / "storyboard.json").write_text(
            json.dumps(reuse_board.to_dict(), ensure_ascii=False), encoding="utf-8")
        restored_board = storyboard_from_dict(plan_dict)
        Pipeline._restore_reuse_state(reuse_pipe, restored_board)
        check("恢复证据后旧片段可复用",
              restored_board.shots[0].image_sha256 == "sha-reuse"
              and restored_board.shots[0].evidence_id == "ev-reuse"
              and clipcache.state(reuse_clip, Pipeline._clip_key(
                  reuse_pipe, restored_board.shots[0], brief, restored_board)) == "current", "")
        edited_board = storyboard_from_dict(plan_dict)
        edited_board.shots[0].visual = "完全不同的画面"
        Pipeline._restore_reuse_state(reuse_pipe, edited_board)
        check("恢复证据不掩盖真实内容改动",
              clipcache.state(reuse_clip, Pipeline._clip_key(
                  reuse_pipe, edited_board.shots[0], brief, edited_board)) == "stale", "")
        skip_board = storyboard_from_dict(plan_dict)
        Pipeline._restore_reuse_state(reuse_pipe, skip_board, skip_shot=1)
        check("单镜重拍不恢复目标镜证据", skip_board.shots[0].image_sha256 == "", "")
        check("两条复用路径都恢复证据",
              "_restore_reuse_state" in inspect.getsource(Pipeline._recompose)
              and "_restore_reuse_state" in inspect.getsource(Pipeline._render_single_shot), "")

        # 行为验证：已有生成缓存时，_process_shot 不再触发出图，直接恢复证据出片。
        cache_work = tmp / "work-cache"
        cached_image = tmp / "cached.png"
        Image.new("RGB", (32, 32), (60, 90, 120)).save(cached_image, "PNG")
        gencache.store(
            cache_work / "gencache", 1, "key-hit", cached_image,
            provider="comfy", quality=4, status="approved",
            evidence={"evidence_id": "ev-hit", "sha256": "sha-hit",
                      "review_mode": "vlm", "visual_check": "performed",
                      "review_rule": "r", "verdict": "accept", "defect": "none",
                      "provider": "comfy", "attempt": 1, "technical": {"ok": True}},
            candidates=[],
        )
        make_calls: list[int] = []

        def restore_hit(shot_obj, record):
            shot_obj.frame_path = record["image"]
            shot_obj.status = record["status"]
            shot_obj.quality = record["quality"]
            shot_obj.frame_source = "comfy"
            shot_obj.frame_is_clean = True
            shot_obj.image_sha256 = "sha-hit"
            shot_obj.evidence_id = "ev-hit"
            return True

        fake_process = SimpleNamespace(
            no_cache=False, only_shot=0,
            decider=SimpleNamespace(route_task=lambda *a, **k: {},
                                     model_route=lambda *a, **k: {}),
            ctx=SimpleNamespace(work_dir=cache_work, notes=[], budget_ok=True),
            log=SimpleNamespace(event=lambda *a, **k: None, error=lambda *a, **k: None),
            _decide=lambda point, fn, *a, **k: (
                {"route": "proceed_fast"} if point == "route_task"
                else {"decision": "comfy"}
            ),
            _model_candidates=lambda: [{"id": "comfy"}],
            _locked_prompt=lambda shot_obj, _board: (shot_obj, ["锁定"]),
            _gen_signature=lambda *a, **k: "key-hit",
            _restore_generation=restore_hit,
            _make_clip=lambda *a, **k: make_calls.append(1) or (tmp / "clip.mp4"),
            _generate_still=lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("命中缓存后不应再出图")),
        )
        cache_shot = Shot(id=1, act="A", duration=2.0, narration="", visual="面", camera="特写")
        Pipeline._process_shot(fake_process, cache_shot, brief, board)
        check("生成缓存命中后不再出图且直接出片",
              make_calls == [1] and cache_shot.evidence_id == "ev-hit"
              and cache_shot.image_sha256 == "sha-hit", f"{make_calls}/{cache_shot.evidence_id}")

        # ---- I2-BE-07：字幕在最终合成叠加
        fake_sub = SimpleNamespace(
            ctx=SimpleNamespace(work_dir=tmp / "work-sub"),
            genre=pick_genre("面"), preview=True, guides=False,
            subtitle_mode="burn", subtitle_offset=0.0, subtitle_style=None,
            subtitle_lines=[{"start": 0.0, "end": 1.0, "text": "第一句"},
                            {"start": 1.0, "end": 2.0, "text": "第二句"}],
            music=SimpleNamespace(enabled=False, is_bgm=False), lyric_lines=[],
        )
        layer_shots = [
            Shot(id=1, act="A", duration=1.0, narration="旧字幕一", visual="", camera=""),
            Shot(id=2, act="A", duration=1.0, narration="旧字幕二", visual="", camera=""),
        ]
        layers = Pipeline._text_layers(fake_sub, brief, board, layer_shots)
        check("烧录模式包含固定层与逐条字幕卡", len(layers) == 4,
              str([(Path(p).name, round(s, 2), round(e, 2)) for p, s, e in layers]))
        fake_sub.subtitle_mode = "sidecar"
        check("sidecar 模式只保留固定层",
              len(Pipeline._text_layers(fake_sub, brief, board, layer_shots)) == 2, "")
        card = render_subtitle_card("最终字幕", brief, tmp / "card.png", scale=0.25)
        with Image.open(card) as im:
            check("字幕卡是透明底且尺寸正确",
                  im.mode == "RGBA" and im.size == (480, 270), f"{im.mode}/{im.size}")

        # 时间窗口：0.2-1.0 有白块，1.5 处应该仍是黑底
        base_video = tmp / "base.mp4"
        subprocess.run([
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=320x180:d=2:r=25",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(base_video),
        ], check=True, capture_output=True)
        block = tmp / "block.png"
        img = Image.new("RGBA", (320, 180), (0, 0, 0, 0))
        from PIL import ImageDraw
        ImageDraw.Draw(img).rectangle([20, 20, 120, 80], fill=(255, 255, 255, 255))
        img.save(block, "PNG")
        texted = overlay_text_layers(base_video, [(block, 0.2, 1.0)], tmp / "texted.mp4")
        for at, want_bright in ((0.5, True), (1.5, False)):
            frame = tmp / f"frame-{at}.png"
            subprocess.run([
                FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                "-ss", str(at), "-i", str(texted), "-frames:v", "1", str(frame),
            ], check=True, capture_output=True)
            with Image.open(frame) as im:
                mean = sum(im.convert("L").getdata()) / (im.width * im.height)
            check(f"文字层时间窗 {'内' if want_bright else '外'} 抽帧符合预期",
                  (mean > 20) if want_bright else (mean < 5), f"mean={mean:.1f}")

        check("RunRequest 支持字幕模式",
              RunRequest(theme="x", subtitle_mode="sidecar").subtitle_mode == "sidecar", "")
        check("RenderRequest 支持字幕模式",
              RenderRequest(subtitle_mode="none").subtitle_mode == "none", "")
        saved_mode = splan.save_subtitles(
            snap_plan.parent.name,
            SubtitlesBody(lines=[], subtitle_mode="sidecar"), None,
        )
        saved_meta = json.loads((snap_plan / "meta.json").read_text(encoding="utf-8"))
        check("保存字幕模式进入项目设置",
              saved_mode["subtitle_mode"] == "sidecar"
              and saved_meta["audio_edit"]["subtitle_mode"] == "sidecar",
              str(saved_meta.get("audio_edit")))
    finally:
        splan.ROOT = saved["root"]
        pconfig.ROOT = saved["config_root"]
        shist.RUNS = saved["runs"]
        sauth.DB_PATH = saved["db"]
        splan.REGISTRY = saved["registry"]
        sapp.REGISTRY = saved["app_registry"]
        shutil.rmtree(tmp, ignore_errors=True)


def test_iter2_frontend_contract() -> None:
    """前端 3 个发现的静态契约（真实交互见交接报告“未实测”边界）。"""
    print("\n[37] 第二轮：前端保存屏障与状态映射")
    plan = (ROOT / "web" / "src" / "pages" / "PlanPage.tsx").read_text(encoding="utf-8")
    use_run = (ROOT / "web" / "src" / "hooks" / "useRun.ts").read_text(encoding="utf-8")
    history = (ROOT / "web" / "src" / "pages" / "HistoryPage.tsx").read_text(encoding="utf-8")
    create = (ROOT / "web" / "src" / "pages" / "CreatePage.tsx").read_text(encoding="utf-8")
    check("字幕保存进入统一队列",
          "await enqueue('subtitle', async () =>" in plan
          and "saveSubtitles(runId, lines, revisionRef.current, mode)" in plan, "")
    check("每个保存域各自登记失败",
          all(f"enqueue('{domain}'" in plan for domain in
              ("subtitle", "palette", "shot", "script", "subject", "reference", "order"))
          and "failedSavesRef.current.delete(domain)" in plan, "")
    check("字幕失败进入统一失败登记",
          "await noteSaveFailure(err)" in plan, "")
    check("整表重写时锁住字段级编辑",
          "const rewriteBusy = Boolean(busyAct) || busyVersion > 0" in plan
          and plan.count("locked={rewriteBusy}") >= 2
          and "rewriteBusy={rewriteBusy}" in plan, "")
    check("正在输入的草稿不被服务端回写冲掉",
          "function isEditingField(" in plan
          and "if (!isEditingField(fieldKey('visual'))) setVisual(shot.visual)" in plan
          and "if (!isEditingField(actKey)) setDraft(" in plan
          and "data-field={fieldKey('visual')}" in plan, "")
    check("保存失败把字段退回服务端值",
          "if (!ok) setDuration(fmtSeconds(shot.duration))" in plan
          and "if (!ok) {" in plan and "setName(subject.name)" in plan, "")
    check("色板保存读取实时 revision",
          "savePalette(runId, palette, revisionRef.current)" in plan, "")
    check("保存中禁止拍摄",
          "subBusy || Boolean(paletteBusy)" in plan, "")
    check("useRun 显式区分已停止",
          "setPhase(stopped ? 'canceled' : 'done')" in use_run
          and "snap.status === 'canceled'" in use_run, "")
    check("创作页显示已停止而非已产出",
          "phase === 'canceled' ? '已停止'" in create
          and "已停止 · 部分产出" in create, "")
    check("混合质检按模式数量汇总",
          "modeCounts.vlm" in history
          and "视觉 ${modeCounts.vlm}/${reviewTotal}" in history, "")
    check("品牌未保存时阻断导出",
          "brandDirty" in history
          and "品牌模板有未保存的修改" in history, "")
    check("详情抽屉标注中断与部分产出",
          "中断 · 部分产出" in history
          and "成片只包含已经拍完的镜头" in history
          and '"status": _status(path, video is not None)' in
          (ROOT / "server" / "history.py").read_text(encoding="utf-8"), "")
    check("同画幅品牌导出在界面可见",
          "加品牌" in history
          and "EXPORT_COMBOS.filter((combo) => combo.aspect !== sameAspect)" in history
          and "const brandActive = Boolean(brand?.applied)" in history, "")
    check("离线镜头静帧标注来源",
          "离线占位背景（已抹字，非评审证据图）" in history
          and "source === 'offline'" in history
          and "source={shot.frame_source}" in history, "")
    check("导出预设可在界面选择",
          "导出规格" in history
          and "presets?: ExportPreset[]" in
          (ROOT / "web" / "src" / "lib" / "api.ts").read_text(encoding="utf-8")
          and "startExport(runId, aspect, mode, preset)" in history
          and "preset: exportPreset" in history, "")
    check("字幕模式可保存并立即排队",
          "呈现方式" in plan and "persistSubtitles(subLines, value)" in plan
          and "subtitle_mode" in (ROOT / "web" / "src" / "lib" / "api.ts").read_text(encoding="utf-8"), "")


def test_iter3_subtitle_style() -> None:
    """C2：6 套整句字幕样式的排版、缺字回退、存储与接线。"""
    print("\n[38] 第三次迭代：字幕样式模板（C2）")
    import json
    import tempfile
    from pathlib import Path

    from PIL import Image

    import server.plan as splan
    from pipeline import subtitle_style as sstyle
    from pipeline.image_render import render_subtitle_card
    from pipeline.models import ProjectBrief
    from server.schemas import SubtitleStyleBody

    check("预置 6 套样式且 id 唯一",
          len(sstyle.STYLES) == 6
          and len({item["id"] for item in sstyle.style_catalog()}) == 6
          and all(item["note"] for item in sstyle.style_catalog()), "")
    check("未知样式在接口层抛错、管线层回退",
          _raises(lambda: sstyle.style_of("nope"))
          and sstyle.resolve_style("nope").id == sstyle.DEFAULT_STYLE_ID, "")

    tmp = Path(tempfile.mkdtemp(prefix="yiyan-c2-"))
    text = "羊骨吊足八小时，汤色奶白，一口下去暖到胃里"
    bad: list[str] = []
    for aspect in ("16:9", "9:16", "1:1"):
        brief = ProjectBrief(theme="字幕样式", aspect_ratio=aspect)
        for preset in sstyle._PRESETS:
            out = tmp / f"{aspect.replace(':', 'x')}-{preset.id}.png"
            reports: list[str] = []
            render_subtitle_card(text, brief, out, scale=0.5,
                                 style=preset, report=reports)
            with Image.open(out) as image:
                bbox = image.getbbox()
                width, height = image.size
                left, top, right, bottom = sstyle.safe_box(width, height, preset)
                inside = bool(bbox) and (
                    bbox[0] >= left - 2 and bbox[2] <= right + 2
                    and bbox[1] >= top - 2 and bbox[3] <= bottom + 2
                )
                if image.mode != "RGBA" or not inside or reports:
                    bad.append(f"{aspect}/{preset.id}/{bbox}/{reports}")
    check("6 套样式 × 3 画幅都在字幕安全区内", not bad, "; ".join(bad[:3]))

    image_a = tmp / "a.png"
    image_b = tmp / "b.png"
    render_subtitle_card("同一条字幕", ProjectBrief(theme="t", aspect_ratio="16:9"),
                         image_a, scale=0.5, style=sstyle.STYLES["clean"])
    render_subtitle_card("同一条字幕", ProjectBrief(theme="t", aspect_ratio="16:9"),
                         image_b, scale=0.5, style=sstyle.STYLES["vivid"])
    check("不同样式产出不同像素",
          Image.open(image_a).tobytes() != Image.open(image_b).tobytes(), "")
    check("常见中文无缺字、emoji 明确报缺字",
          sstyle.coverage([text], sstyle.STYLES["clean"])["ok"]
          and not sstyle.coverage(["测试" + chr(0x1F642)],
                                  sstyle.STYLES["clean"])["ok"], "")

    # ---- 存储与接口
    saved_root = splan.ROOT
    try:
        splan.ROOT = tmp
        run = tmp / "runs" / "style-run"
        (run / "plan").mkdir(parents=True)
        (run / "plan" / "meta.json").write_text(
            json.dumps({"revision": 3}, ensure_ascii=False), encoding="utf-8")
        (run / "plan" / "storyboard.json").write_text(
            json.dumps({
                "project": {"total_duration": 5},
                "shots": [{"id": 1, "start": 0, "duration": 5,
                           "caption": "羊骨吊足八小时"}],
            }, ensure_ascii=False), encoding="utf-8")

        info = splan.get_subtitle_style("style-run")
        check("接口返回当前样式、6 套清单与缺字结果",
              info["current"]["id"] == sstyle.DEFAULT_STYLE_ID
              and len(info["presets"]) == 6 and info["coverage"]["ok"], "")

        out = splan.save_subtitle_style(
            "style-run", SubtitleStyleBody(preset="ink", expected_revision=3), None)
        meta = json.loads((run / "plan" / "meta.json").read_text(encoding="utf-8"))
        check("换样式独立累计样式版本并推进项目版本",
              out["current"]["id"] == "ink" and out["current"]["version"] == 1
              and out["revision"] == 4
              and meta["subtitle_style"]["preset"] == "ink", "")
        check("重复保存同一套样式不涨版本",
              splan.save_subtitle_style(
                  "style-run", SubtitleStyleBody(preset="ink", expected_revision=4), None
              )["current"]["version"] == 1, "")
        check("未知样式 422、旧 revision 409",
              _status_of(lambda: splan.save_subtitle_style(
                  "style-run", SubtitleStyleBody(preset="nope"), None)) == 422
              and _status_of(lambda: splan.save_subtitle_style(
                  "style-run", SubtitleStyleBody(preset="clean", expected_revision=3), None)
              ) == 409, "")

        snap = splan._freeze_plan(
            run / "plan", 4, {"live": False}, {"voice_gain": 1.0}, "c2snap",
            subtitle_style=out["current"])
        check("样式随快照冻结，可复查这条片子用的哪套",
              (snap / "subtitle_style.json").is_file()
              and json.loads((snap / "subtitle_style.json").read_text(
                  encoding="utf-8"))["id"] == "ink", "")
    finally:
        splan.ROOT = saved_root

    plan_src = (ROOT / "server" / "plan.py").read_text(encoding="utf-8")
    pipe_src = (ROOT / "pipeline" / "pipeline.py").read_text(encoding="utf-8")
    check("样式随 RunOptions 进入管线且只影响字幕卡",
          'subtitle_style=str(style_state["id"])' in plan_src
          and "style=self.subtitle_style" in pipe_src
          and "report=warnings" in pipe_src, "")
    check("样式不进入片段缓存键",
          "subtitle_style" not in (ROOT / "pipeline" / "clipcache.py").read_text(encoding="utf-8")
          and "subtitle_style" not in (ROOT / "pipeline" / "gencache.py").read_text(encoding="utf-8"), "")

    api_src = (ROOT / "web" / "src" / "lib" / "api.ts").read_text(encoding="utf-8")
    plan_page = (ROOT / "web" / "src" / "pages" / "PlanPage.tsx").read_text(encoding="utf-8")
    check("界面可选 6 套样式并提示缺字/只重合成",
          "fetchSubtitleStyle(runId)" in plan_page
          and "pickSubtitleStyle(item.id)" in plan_page
          and "整句样式" in plan_page
          and "换样式只影响重新合成" in plan_page
          and "saveSubtitleStyle" in api_src, "")


def test_iter3_audio_duck() -> None:
    """C5：对白优先（sidechaincompress 闪避）的参数、持久化与接线。"""
    print("\n[39] 第三次迭代：对白优先闪避（C5）")
    from pathlib import Path

    import server.plan as splan
    from pipeline.compose import DUCK_FILTER, build_mix_args
    from server.schemas import RenderRequest

    voice = Path("v.wav")
    music = Path("m.wav")
    plain = build_mix_args([(voice, 1.0, False), (music, 0.28, True)],
                           Path("mix.m4a"), duration=5.0)
    ducked = build_mix_args([(voice, 1.0, False), (music, 0.28, True)],
                            Path("mix.m4a"), duration=5.0, duck=True)
    plain_graph = plain[plain.index("-filter_complex") + 1]
    duck_graph = ducked[ducked.index("-filter_complex") + 1]
    check("默认不接闪避，链路与旧版一致",
          "sidechaincompress" not in plain_graph and "amix=inputs=2" in plain_graph,
          plain_graph)
    check("开启后接入 sidechaincompress 且人声仍入混",
          DUCK_FILTER in duck_graph
          and "[a0]asplit=2[voice][raw0]" in duck_graph
          and "[voice][d0]amix=inputs=2" in duck_graph, duck_graph)
    check("侧链键补到整片长度，避免截断音乐尾巴",
          "apad=whole_dur=5.000" in duck_graph, duck_graph)
    single = build_mix_args([(music, 0.28, True)], Path("mix.m4a"),
                            duration=5.0, duck=True)
    check("只有音乐一轨时不接闪避",
          "sidechaincompress" not in single[single.index("-filter_complex") + 1], "")
    three = build_mix_args([(voice, 1.0, False), (music, 0.2, True), (music, 0.2, True)],
                           Path("mix.m4a"), duration=5.0, duck=True)
    three_graph = three[three.index("-filter_complex") + 1]
    check("多条垫乐各自接一把侧链键",
          three_graph.count("sidechaincompress") == 2 and "[a0]asplit=3" in three_graph,
          three_graph)

    check("旧项目没有该字段时默认关闭（行为不变）",
          splan._audio_edit({})["voice_duck"] is False
          and splan._audio_edit({"audio_edit": {"voice_duck": True}})["voice_duck"] is True, "")
    check("请求可显式开关，且 None 表示继承项目保存值",
          RenderRequest(voice_duck=True).voice_duck is True
          and RenderRequest().voice_duck is None, "")

    plan_src = (ROOT / "server" / "plan.py").read_text(encoding="utf-8")
    pipe_src = (ROOT / "pipeline" / "pipeline.py").read_text(encoding="utf-8")
    engine_src = (ROOT / "app" / "engine.py").read_text(encoding="utf-8")
    compose_src = (ROOT / "pipeline" / "compose.py").read_text(encoding="utf-8")
    check("开关从请求 → 项目 → RunOptions → 管线 → 混音全程接线",
          'if req.voice_duck is not None:' in plan_src
          and 'voice_duck=bool(audio_edit.get("voice_duck", False))' in plan_src
          and "voice_duck=opts.voice_duck" in engine_src
          and "voice_duck=self.voice_duck" in pipe_src
          and "duck=bool(voice_duck and len(tracks) > 1)" in compose_src, "")
    check("没有配音时明确提示闪避未生效（不静默）",
          "对白优先" in compose_src
          and "音乐闪避未生效" in compose_src, "")

    api_src = (ROOT / "web" / "src" / "lib" / "api.ts").read_text(encoding="utf-8")
    page_src = (ROOT / "web" / "src" / "pages" / "PlanPage.tsx").read_text(encoding="utf-8")
    check("界面有「对白优先」开关并随拍摄/重合成保存",
          "voice_duck: boolean" in api_src
          and "voice_duck?: boolean" in api_src
          and "对白优先（音乐自动闪避）" in page_src
          and "voice_duck: mix.voice_duck" in page_src
          and "disabled={isMv || !hasMusic}" in page_src, "")


def test_iter3_transitions() -> None:
    """C4：转场（xfade 补边保时长）与字幕淡入的接线、默认值与界面。"""
    print("\n[40] 第三次迭代：转场与文字动效（C4）")
    import server.plan as splan
    from pipeline.compose import TEXT_FADE_SECONDS, TRANSITIONS
    from server.schemas import RenderRequest

    check("转场只提供三种 + 硬切",
          set(TRANSITIONS) == {"none", "fade", "slide", "black"}
          and TRANSITIONS["fade"][0] == "fade"
          and TRANSITIONS["slide"][0] == "slideleft"
          and TRANSITIONS["black"][0] == "fadeblack", str(TRANSITIONS))
    check("默认硬切、默认不淡入（旧项目行为不变）",
          splan._audio_edit({})["transition"] == "none"
          and splan._audio_edit({})["text_anim"] is False, "")
    check("旧项目里的未知转场回退硬切，不因坏配置拒绝拍摄",
          splan._audio_edit({"audio_edit": {"transition": "zoom"}})["transition"] == "none", "")
    check("请求可显式设置转场/动效，非法转场被 422",
          RenderRequest(transition="fade", text_anim=True).transition == "fade"
          and RenderRequest().transition is None
          and _raises(lambda: RenderRequest(transition="zoom")), "")

    compose_src = (ROOT / "pipeline" / "compose.py").read_text(encoding="utf-8")
    check("转场用 xfade，且只改拼接步骤",
          "xfade=transition={name}" in compose_src
          and "tpad=start_duration={head:.3f}:start_mode=clone" in compose_src
          and "trim=duration={target:.3f}" in compose_src
          and 'if kind != "none" and len(clips) > 1:' in compose_src, "")
    check("转场半时长不超过最短镜头的 1/4（极短镜头不丢内容）",
          "min(float(d) for d in durations) / 4" in compose_src, "")
    check("字幕淡入只作用于字幕卡，固定标题层不参与",
          "if animate and index > 1:" in compose_src
          and f"TEXT_FADE_SECONDS = {TEXT_FADE_SECONDS}" in compose_src
          and "fade=t=in:st=" in compose_src, "")

    plan_src = (ROOT / "server" / "plan.py").read_text(encoding="utf-8")
    pipe_src = (ROOT / "pipeline" / "pipeline.py").read_text(encoding="utf-8")
    engine_src = (ROOT / "app" / "engine.py").read_text(encoding="utf-8")
    check("转场/动效从请求 → 项目 → RunOptions → 管线 → 合成全程接线",
          'if req.transition is not None:' in plan_src
          and 'if req.text_anim is not None:' in plan_src
          and 'transition=str(audio_edit.get("transition") or "none")' in plan_src
          and 'text_anim=bool(audio_edit.get("text_anim", False))' in plan_src
          and "transition=opts.transition" in engine_src
          and "text_anim=opts.text_anim" in engine_src
          and "transition=self.transition" in pipe_src
          and "text_anim=self.text_anim" in pipe_src
          and "animate=text_anim" in compose_src, "")
    check("转场/动效不进入片段缓存键",
          "transition" not in (ROOT / "pipeline" / "clipcache.py").read_text(encoding="utf-8")
          and "text_anim" not in (ROOT / "pipeline" / "clipcache.py").read_text(encoding="utf-8")
          and "transition" not in (ROOT / "pipeline" / "gencache.py").read_text(encoding="utf-8")
          and "text_anim" not in (ROOT / "pipeline" / "gencache.py").read_text(encoding="utf-8"), "")

    api_src = (ROOT / "web" / "src" / "lib" / "api.ts").read_text(encoding="utf-8")
    page_src = (ROOT / "web" / "src" / "pages" / "PlanPage.tsx").read_text(encoding="utf-8")
    check("界面可选转场、可开字幕淡入，并说明时长取舍",
          "export type TransitionId" in api_src
          and "transition: TransitionId" in api_src
          and "text_anim: boolean" in api_src
          and "镜头衔接与字幕动效" in page_src
          and "TRANSITION_OPTIONS" in page_src
          and "字幕淡入" in page_src
          and "总时长与字幕时间轴不变" in page_src, "")


def test_ui_layout_batch() -> None:
    """UI-W1/W2：创作台首屏、拆解内容优先、导航归属与滚动条。"""
    print("\n[41] 前端布局：UI-W1 创作台 / UI-W2 拆解")
    create = (ROOT / "web" / "src" / "pages" / "CreatePage.tsx").read_text(encoding="utf-8")
    plan = (ROOT / "web" / "src" / "pages" / "PlanPage.tsx").read_text(encoding="utf-8")
    nav = (ROOT / "web" / "src" / "components" / "TopNav.tsx").read_text(encoding="utf-8")
    app = (ROOT / "web" / "src" / "App.tsx").read_text(encoding="utf-8")

    check("创作台 1024px 起双栏，运行栏不再等高拉长",
          "lg:grid-cols-[minmax(0,1fr)_320px]" in create
          and "items-start gap-4" in create
          and "min-h-[620px]" not in create, "")
    check("高级设置默认折叠，画质/帧率随代码绘制出现",
          "useState(false)" in create
          and "高级设置" in create
          and "{advancedOpen && (" in create
          and "{form.painted && (" in create, "")
    check("主操作常驻：sticky 操作条 + 配置摘要",
          "sticky bottom-0" in create
          and "开始生成" in create
          and "generationLabel" in create, "")
    check("待命右栏为紧凑概览，日志默认折叠",
          "实时日志 · {logs.length} 条" in create
          and "logsOpen" in create
          and "预计镜头" in create, "")

    check("拆解头部有项目面包屑与独立动作行",
          "aria-label=\"位置\"" in plan
          and "onNavigate('projects')" in plan
          and "回项目" in plan
          and "ml-auto flex flex-wrap items-center gap-2" in plan, "")
    check("项目配置默认折叠但不卸载（display contents/hidden）",
          "const [configOpen, setConfigOpen] = useState(false)" in plan
          and "configOpen ? 'contents' : 'hidden'" in plan
          and "项目配置" in plan, "")
    check("分镜在 1024px 起进入主编辑列，窄屏排前",
          "lg:grid-cols-[minmax(0,320px)_minmax(0,1fr)]" in plan
          and "order-1 px-5 py-4 lg:order-2" in plan
          and "order-2 px-5 py-4 lg:order-1" in plan, "")
    check("主导航把拆解归属到项目，Logo 统一回创作台",
          "route === 'plan' && item.key === 'projects'" in nav
          and "onNavigate('create')" in nav, "")
    check("主滚动区恢复可见细滚动条",
          'className="smooth-scroll scroll-thin relative z-10 h-full overflow-y-auto"' in app, "")


def _raises(fn) -> bool:
    try:
        fn()
    except Exception:  # noqa: BLE001
        return True
    return False


def _status_of(fn) -> int:
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        return int(getattr(exc, "status_code", 0))
    return 0


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
    test_reuse_roundtrip()
    test_master_publish()
    test_timeline_edit()
    test_access_and_versions()
    test_review_audio_sse()
    test_settings_route_upload()
    test_projects_and_versions()
    test_task_center()
    test_visual_review()
    test_preview_render()
    test_audio_subtitle_edit()
    test_subtitle_track()
    test_subject_assets()
    test_waveform()
    test_export_profiles()
    test_retry_cancel()
    test_brand_batch()
    test_provider_budget()
    test_clip_cache_palette()
    test_vision_review()
    test_brand_font_version()
    test_iter2_backend()
    test_mux_picture_authority()
    test_iter3_artifacts()
    test_iter2_frontend_contract()
    test_iter3_subtitle_style()
    test_iter3_audio_duck()
    test_iter3_transitions()
    test_ui_layout_batch()
    print(f"\n结果：{len(PASSED)} 项通过，{len(FAILED)} 项失败")
    if FAILED:
        print("失败项：")
        for name in FAILED:
            print("  - " + name)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
