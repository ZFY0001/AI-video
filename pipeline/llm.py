"""一言片场 · 创意层客户端（语言大模型）+ 离线创意引擎。

两种模式共用一个接口：
- OpenAIChatClient：任何 OpenAI 兼容端点（DeepSeek / 通义 / OpenAI / 本地 vLLM…）
- OfflineCreativeEngine：不联网也能产出结构合法的简报/剧本/分镜，用题材词库填充
- CachedClient：磁盘缓存，重跑同一条不重复烧钱

提示词一律来自 pipeline.prompts（即 docs/提示词库-V1.md），本模块不写死任何提示词文本。
"""

from __future__ import annotations

import hashlib
import json
import re
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from . import net
from .common import extract_json, split_sentences
from .genre import Genre, clean_theme, pick_genre, sanitize_input, stable_choice, theme_tokens
from .models import ProjectBrief, Script, Shot, Storyboard, Subject, build_timeline
from .prompts import PromptLibrary
from .tts import MAX_TEMPO


# ------------------------------------------------------------------ 接口
class ChatClient(Protocol):
    name: str

    def chat(self, system: str, user: str, temperature: float = 0.7) -> str: ...


class LLMError(RuntimeError):
    pass


# 各家 OpenAI 兼容端点的报错文案差异很大，这里把常见 HTTP 状态码翻译成
# 「哪里错了 + 怎么改」，避免用户只看到一串 JSON 就怀疑人生。
_LLM_MODEL_HINTS = {
    "deepseek-chat": "DeepSeek：https://api.deepseek.com/v1，模型 deepseek-chat 或 deepseek-reasoner",
    "deepseek-reasoner": "DeepSeek：https://api.deepseek.com/v1，模型 deepseek-chat 或 deepseek-reasoner",
    "qwen-plus": "通义千问：https://dashscope.aliyuncs.com/compatible-mode/v1，模型 qwen-plus",
    "qwen-turbo": "通义千问：https://dashscope.aliyuncs.com/compatible-mode/v1，模型 qwen-turbo",
    "gpt-4o-mini": "OpenAI：https://api.openai.com/v1，模型 gpt-4o-mini",
    "gpt-4o": "OpenAI：https://api.openai.com/v1，模型 gpt-4o",
    "moonshot-v1-8k": "Moonshot：https://api.moonshot.cn/v1，模型 moonshot-v1-8k",
}


def _explain_llm_http(code: int, raw: str, base_url: str, model: str) -> str:
    """把 HTTP 错误翻成可执行的排查建议。"""
    body = raw[:200]
    low = raw.lower()
    hint = _LLM_MODEL_HINTS.get(model, "")
    if code == 401:
        return (f"HTTP 401 鉴权失败：密钥无效或没生效。请检查 ① 密钥是否复制完整（常见是少复制/多空格）"
                f"② 是否填到了「大模型端点」以外的字段 ③ 保存后是否重新体检。"
                f" 服务端原文：{body}")
    if code == 403:
        return f"HTTP 403 无权限：密钥有效但无权访问该模型，或账号欠费/未实名。服务端原文：{body}"
    if code == 404:
        return (f"HTTP 404 端点或模型不存在。最常见两种原因："
                f"① 端点少了或多了路径段——应为 .../v1（不是 .../v1/chat/completions，"
                f"程序会自己补）；② 模型名 {model!r} 在该服务不存在。参考：{hint or '查服务商模型列表'}")
    if code == 400:
        if "temperature" in low:
            return f"HTTP 400 参数不合法：该模型不接受当前 temperature。服务端原文：{body}"
        return f"HTTP 400 请求被拒：多为模型名/参数不被支持。服务端原文：{body}"
    if code == 402:
        return f"HTTP 402 余额不足：请充值或换用其他服务。服务端原文：{body}"
    if code == 413:
        return f"HTTP 413 请求过大：提示词或上下文超出模型限制，可换更大的模型。服务端原文：{body}"
    if code == 429:
        return f"HTTP 429 限流：请求太快或额度用尽，稍后重试。服务端原文：{body}"
    if code == 500 or code == 502 or code == 503:
        return f"HTTP {code} 服务端错误：服务商临时故障或模型不可用，稍后重试。服务端原文：{body}"
    return f"HTTP {code}：{body}（端点 {base_url}，模型 {model}）"


# ------------------------------------------------------------------ 真实客户端
@dataclass
class OpenAIChatClient:
    base_url: str
    api_key: str
    model: str
    name: str = "llm"
    timeout: int = 120
    max_retries: int = 3

    def _endpoint(self) -> str:
        """拼出最终的 chat/completions 地址，容忍用户填法的各种不规范。

        实测踩过：用户会填 `https://api.deepseek.com`（漏 /v1，得到 404）、
        也会填 `https://api.deepseek.com/v1/chat/completions`（填全了，再拼一次
        就变成 .../chat/completions/chat/completions，同样 404）。这里统一规整：
        去掉尾部斜杠 → 已经以 /chat/completions 结尾就直接用 →
        已有 /v1 就只补 chat/completions → 两者都没有就补 /v1/chat/completions。
        """
        url = (self.base_url or "").strip().rstrip("/")
        if not url:
            raise LLMError("未配置大模型端点（LLM_BASE_URL）")
        if not url.startswith(("http://", "https://")):
            raise LLMError(f"大模型端点必须以 http:// 或 https:// 开头，当前是：{url!r}")
        if url.endswith("/chat/completions"):
            return url
        if url.rstrip("/").endswith("/v1"):
            return url + "/chat/completions"
        return url + "/v1/chat/completions"

    def diagnose(self) -> tuple[bool, str]:
        """用一次极小的请求验证端点/密钥/模型名是否真的可用。

        为什么必须有这个方法：原先体检只看「LLM_API_KEY 非空」，于是密钥打错、
        端点少写 /v1、模型名不存在（deepseek-chat vs deepseek-chat-reasoner）、
        账户欠费——这些全都要等真正跑片时才炸，用户看到的往往只是一句
        「语言大模型调用失败（deepseek-chat）：HTTP 401」。这里发一个
        max_tokens=1 的最小请求，把错误翻译成能照着改的提示。
        """
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1,
            "temperature": 0,
        }
        url = self._endpoint()
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with net.open_url(req, timeout=30) as resp:
                json.loads(resp.read().decode("utf-8"))
            return True, f"{self.model} 连通正常"
        except urllib.error.HTTPError as exc:
            return False, _explain_llm_http(
                exc.code, exc.read().decode("utf-8", "ignore"), self.base_url, self.model
            )
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, ssl.SSLError):
                return False, f"TLS 握手失败（证书或代理拦截）：{reason}"
            return False, f"连不上 {self.base_url}：{reason}（检查网络/代理/防火墙）"
        except Exception as exc:  # noqa: BLE001
            return False, f"未知错误：{type(exc).__name__}: {exc}"

    def chat(self, system: str, user: str, temperature: float = 0.7) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        url = self._endpoint()
        last_err: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            req = urllib.request.Request(
                url,
                data=data,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
                method="POST",
            )
            try:
                with net.open_url(req, timeout=self.timeout) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                return body["choices"][0]["message"]["content"]
            except urllib.error.HTTPError as exc:  # 4xx 不重试
                detail = exc.read().decode("utf-8", "ignore")
                last_err = LLMError(_explain_llm_http(exc.code, detail, self.base_url, self.model))
                if 400 <= exc.code < 500 and exc.code != 429:
                    break
            except Exception as exc:  # noqa: BLE001
                last_err = exc
            time.sleep(min(2 ** attempt, 8))
        raise LLMError(f"语言大模型调用失败（{self.model}）：{last_err}")


@dataclass
class CachedClient:
    inner: ChatClient
    cache_dir: Path
    name: str = "llm+cache"

    def chat(self, system: str, user: str, temperature: float = 0.7) -> str:
        key = hashlib.sha256(
            f"{self.inner.name}|{temperature}|{system}|{user}".encode("utf-8")
        ).hexdigest()[:24]
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self.cache_dir / f"{key}.txt"
        if path.exists():
            return path.read_text(encoding="utf-8")
        out = self.inner.chat(system, user, temperature)
        path.write_text(out, encoding="utf-8")
        return out


# ------------------------------------------------------------------ 创意层三步
class CreativeLayer:
    """创意层：简报 → 剧本 → 分镜。真实模型与离线引擎走同一套提示词管线。"""

    def __init__(self, lib: PromptLibrary, client: ChatClient | None):
        self.lib = lib
        self.client = client  # None 表示离线
        # 解析失败不再静默：每次降级都记在这里，界面能如实告诉用户
        # 「真实模型返回了但格式不合契约，已降级到离线引擎」。
        # 之前三处都是 `except ValueError: data = {}`，于是接了 Key 也看不出
        # 到底用没用上模型——文案质量差却查不到原因（实测踩过）。
        self.degradations: list[str] = []

    def _ask_json(self, stage: str, system: str, user: str, temperature: float,
                  required: tuple[str, ...]) -> dict[str, Any]:
        """向模型要 JSON，解析失败时记录原因并返回空 dict（交由调用方降级）。

        `required` 是该阶段必须存在的字段：缺了就说明模型没按提示词返回，
        这时把前 200 字原文记进 degradations，方便用户看清问题。
        """
        if self.client is None:
            return {}
        raw = self.client.chat(system, user, temperature=temperature)
        try:
            parsed = extract_json(raw)
        except ValueError:
            self.degradations.append(
                f"{stage}：模型输出不是合法 JSON，已降级到离线引擎。"
                f" 返回开头：{raw.strip()[:200]!r}"
            )
            return {}
        if not isinstance(parsed, dict):
            self.degradations.append(
                f"{stage}：模型返回的是 {type(parsed).__name__} 而不是 JSON 对象"
            )
            return {}
        # 关键：模型**返回了合法 JSON 但缺必需字段**时也必须降级，
        # 不能当成成功继续往下走。之前这里没校验 required，于是部分 JSON
        # 会一路走到 make_script/make_storyboard 再抛「缺少 narration_full」——
        # 用户表现为「一勾真实模型就报错」，而根因是模型没按提示词返回。
        missing = [k for k in required if not parsed.get(k)]
        if missing:
            self.degradations.append(
                f"{stage}：模型返回的 JSON 缺少必需字段 {'、'.join(missing)}，"
                f"已降级到离线引擎。返回的键：{sorted(parsed)}"
            )
            return {}
        return parsed

    # ---------------------------------------------------------- 1.1 简报
    def make_brief(self, raw_input: str, overrides: dict[str, Any] | None = None) -> ProjectBrief:
        overrides = overrides or {}
        # 先把口语指令洗成创作主题再送模型（"给我做一条 15 秒宣传片" → "方城烩面"）
        topic = sanitize_input(raw_input) or raw_input
        data: dict[str, Any] = {}
        if self.client is not None:
            user_p = self.lib.render_user("1.1", {"user_raw_input": topic, "raw_input": topic})
            data = self._ask_json("简报", self.lib.system("1.1"), user_p, 0.2, ("theme",))
        if not data:
            data = _offline_brief(topic)
        for key, val in overrides.items():
            if val not in (None, ""):
                data[key] = val
        brief = ProjectBrief(
            # 原来切到 60 字：用户写详细一点的描述就被砍掉一半，而且**没有任何提示**。
            # 放宽到 300（后端 RunRequest.theme 上限是 2000，这里只是给模型输出兜底）。
            theme=str(data.get("theme") or topic)[:300],
            total_duration=int(data.get("total_duration") or 60),
            aspect_ratio=str(data.get("aspect_ratio") or "16:9"),
            style=str(data.get("style") or ""),
            language=str(data.get("language") or "zh"),
            must_include=list(data.get("must_include") or []),
            must_avoid=list(data.get("must_avoid") or []),
            reference=str(data.get("reference") or ""),
        )
        if not brief.style:
            brief.style = pick_genre(brief.theme + raw_input).tone
        if brief.aspect_ratio not in ("16:9", "9:16", "1:1"):
            brief.aspect_ratio = "16:9"
        brief.total_duration = max(4, min(600, brief.total_duration))
        return brief

    # ---------------------------------------------------------- 1.2 剧本
    def make_script(self, brief: ProjectBrief) -> Script:
        data: dict[str, Any] = {}
        if self.client is not None:
            data = self._ask_json(
                "剧本",
                self.lib.system("1.2"),
                self.lib.render_user(
                    "1.2",
                    {
                        "total_duration": brief.total_duration,
                        "project_brief_json": brief.to_dict(),
                        "project_brief": brief.to_dict(),
                    },
                ),
                0.9,
                ("narration_full",),
            )
        if not data:
            data = _offline_script(brief)
        script = Script(
            title=str(data.get("title") or clean_theme(brief.theme))[:20],
            logline=str(data.get("logline") or ""),
            tone=str(data.get("tone") or brief.style),
            target_audience=str(data.get("target_audience") or "本地受众与泛兴趣观众"),
            hook=str(data.get("hook") or ""),
            acts=list(data.get("acts") or []),
            narration_full=str(data.get("narration_full") or ""),
            key_subjects=list(data.get("key_subjects") or []),
        )
        if not script.narration_full:
            # 之前是裸抛一句「缺少 narration_full」，用户完全不知道是模型没返回
            # 还是提示词有问题。这里把降级原因和模型原文一起带上。
            detail = "；".join(self.degradations) if self.degradations else (
                f"模型返回了 JSON 但没有 narration_full 字段，返回的键：{sorted(data)}"
            )
            raise LLMError(f"剧本缺少 narration_full（解说词），无法继续分镜。原因：{detail}")
        return script

    # ---------------------------------------------------------- 1.3 分镜
    def make_storyboard(
        self,
        brief: ProjectBrief,
        script: Script,
        avg_shot_len: float = 5.0,
        workflows: list[dict[str, Any]] | None = None,
    ) -> Storyboard:
        data: dict[str, Any] = {}
        if self.client is not None:
            data = self._ask_json(
                "分镜",
                self.lib.system("1.3"),
                self.lib.render_user(
                    "1.3",
                    {
                        "avg_shot_len": avg_shot_len,
                        "script_json": script.to_dict(),
                        "script": script.to_dict(),
                    },
                ),
                0.6,
                ("shots",),
            )
        if not data:
            data = _offline_storyboard(brief, script, avg_shot_len)
        try:
            return _normalize_storyboard(data, brief, script, avg_shot_len, workflows or [])
        except LLMError as exc:
            # 分镜契约不满足时，把创意层的降级记录一并带上——否则用户看到的
            # 永远是「分镜缺少 shots 字段」这种没有前因后果的话。
            if self.degradations:
                raise LLMError(f"{exc}（此前：{'；'.join(self.degradations)}）") from exc
            raise

    # ---------------------------------------------------------- 1.3b 单场重拆
    def make_act_shots(
        self,
        brief: ProjectBrief,
        script: Script,
        act_name: str,
        act_text: str,
        target_seconds: float,
        avg_shot_len: float = 5.0,
        previous: list[dict[str, Any]] | None = None,
        subjects: list[Subject] | None = None,
        prev_shot: dict[str, Any] | None = None,
        next_shot: dict[str, Any] | None = None,
        workflows: list[dict[str, Any]] | None = None,
    ) -> list[Shot]:
        """只重拆一场，其余场次的镜头一个都不碰。

        **不重跑整片**是这一期的全部意义：用户改的往往只是一句话，整片重拆会
        连带改掉其他场已经拍板、甚至已经逐镜调过的镜头。代价是"这一场"与
        "其他场"的衔接只能靠上下文约束，不做语义级依赖分析——这是设计上
        接受的粗放。

        返回的镜头 `id` 是从 1 开始的**本场内部序号**，不是全片镜号；
        全片重排由调用方（`Pipeline.replan_act`）负责，因为只有它知道这一场
        该插在哪个位置。
        """
        genre = pick_genre(brief.theme + " " + brief.style)
        default_wf = _workflow_for(brief, genre, workflows or [])
        # 解说词只从本场文字里取：拿整片解说词来分，会把别的场的词搬进这一场，
        # 用户改的那句话反而被稀释掉。切成小句是为了装得进单镜的字数上限。
        sentences = _act_units(act_text) or _act_units(script.narration_full)

        data: dict[str, Any] = {}
        if self.client is not None:
            system = self.lib.slot("1.3", "system").render(
                {"avg_shot_len": avg_shot_len}, strict=False
            )
            user = _act_replan_prompt(
                brief=brief,
                script=script,
                act_name=act_name,
                act_text=act_text,
                target_seconds=target_seconds,
                avg_shot_len=avg_shot_len,
                previous=previous or [],
                prev_shot=prev_shot,
                next_shot=next_shot,
            )
            data = self._ask_json("分镜(单场)", system, user, 0.6, ("shots",))
        if not data:
            data = _offline_act_shots(
                brief, script, act_name, act_text, target_seconds, avg_shot_len
            )

        raw_shots = [r for r in (data.get("shots") or []) if isinstance(r, dict)]
        if not raw_shots:
            raise LLMError(f"场次「{act_name}」重拆后没有任何镜头")
        shots = [
            _normalize_shot(
                raw,
                i,
                brief=brief,
                genre=genre,
                default_wf=default_wf,
                subjects=subjects or [],
                sentences=sentences,
                avg_shot_len=avg_shot_len,
                total=len(raw_shots),
                fallback_act=act_name,
            )
            for i, raw in enumerate(raw_shots)
        ]
        # 场次名强制回写：模型偶尔会自作主张把 act 换成自己的命名，
        # 一旦换了，这一场在分镜里就等于没拆到（按场筛选靠 act 字符串对齐）。
        for shot in shots:
            shot.act = act_name
        _enforce_narration_pacing(shots, text=act_text, clauses=True)
        return shots


def _act_replan_prompt(
    *,
    brief: ProjectBrief,
    script: Script,
    act_name: str,
    act_text: str,
    target_seconds: float,
    avg_shot_len: float,
    previous: list[dict[str, Any]],
    prev_shot: dict[str, Any] | None,
    next_shot: dict[str, Any] | None,
) -> str:
    """单场重拆的 user 消息。

    system 仍然用提示词库 1.3（分镜 JSON 契约的唯一定义处），这里只负责把
    "只拆这一场"这个任务边界说清楚。放在代码里而不是提示词库，是因为它是
    运行时拼装的上下文，不是一个可独立改写的模板。
    """
    suggested = max(1, int(round(target_seconds / max(1.0, avg_shot_len))))
    lines = [
        "这是一次**局部重拆**：整片分镜已经存在，只重拆其中一场，其余场次的镜头一律不动。",
        "",
        "【要重拆的这一场】",
        f"场次名：{act_name}",
        f"本场内容：{act_text or '（剧本没有给这一场写文字，按整片主题自行补足）'}",
        f"本场目标时长：{target_seconds:.1f} 秒",
        f"建议镜数：{suggested}（= 目标时长 ÷ 平均镜长 {avg_shot_len:g} 秒，可按内容增减 1 镜）",
        "",
        "【硬性约束】",
        '1. 只输出 {"shots": [...]}，不要输出 project、subjects，也不要任何解释文字',
        f'2. 每个镜头的 act 字段一律写 "{act_name}"',
        f"3. 所有镜头 duration 累加必须严格等于 {target_seconds:.1f} 秒，单镜 3–8 秒",
        "4. 解说词只能取自本场内容，不得挪用其他场次的句子",
        "5. 其余字段仍按分镜 JSON 契约填全："
        "visual / camera / prompt_image / prompt_video / subject_refs / workflow_id / acceptance",
        "",
        "【整片上下文】",
        f"片名：{script.title}　风格：{brief.style or script.tone}"
        f"　画幅：{brief.aspect_ratio}　整片时长：{brief.total_duration} 秒",
        "剧本各场：",
    ]
    for act in script.acts or []:
        name = str(act.get("name") or "")
        text = str(act.get("content") or act.get("summary") or "")
        mark = "　← 就是这一场" if name == act_name else ""
        lines.append(f"- {name}：{text}{mark}")
    if script.narration_full:
        lines += ["整片解说词：", script.narration_full]

    if previous:
        lines += ["", "【这一场原来的镜头（供参考：可保留，也可推翻重来）】"]
        for i, shot in enumerate(previous, 1):
            lines.append(
                f"{i}. {float(shot.get('duration') or 0):.1f}s｜"
                f"{shot.get('camera') or ''}｜{shot.get('visual') or ''}｜"
                f"台词「{shot.get('narration') or ''}」"
            )

    if prev_shot or next_shot:
        lines += ["", "【与其他场的衔接】"]
        if prev_shot:
            lines.append(
                f"上一场末镜：{prev_shot.get('visual') or ''}"
                f"（{prev_shot.get('camera') or ''}）"
            )
        else:
            lines.append("这一场是开篇，没有上一场。")
        if next_shot:
            lines.append(
                f"下一场首镜：{next_shot.get('visual') or ''}"
                f"（{next_shot.get('camera') or ''}）"
            )
        else:
            lines.append("这一场是收尾，没有下一场。")
    lines += ["", "请只输出这一场的 shots。"]
    return "\n".join(lines)


# ------------------------------------------------------------------ 归一化
CAMERA_CYCLE = [
    ("缓慢推镜，中景", "slow push-in, medium shot"),
    ("固定机位，特写", "static camera, close-up"),
    ("缓慢拉镜，全景", "slow pull-back, wide shot"),
    ("轻微手持，特写", "subtle handheld sway, close-up"),
    ("横向摇移，中景", "slow pan right, medium shot"),
    ("俯视下摇，局部特写", "tilt down, detail shot"),
    ("低角度仰拍，全景", "low angle, wide shot"),
    ("固定机位，过肩中景", "locked-off shot, over-the-shoulder medium shot"),
]

ACCEPTANCE_POOL = {
    "default": ["主体清晰、边缘无畸变、影调统一", "主体位置正确、无多余元素、细节完整"],
    "food": ["主体清晰、汤面反光自然、热气可见", "碗沿无畸形、色泽温暖、焦点准确"],
    "city": ["主体清晰、水平线平直、色调统一", "透视正确、高光不过曝、层次分明"],
    "person": ["面部清晰、五官无变形、光比自然", "手部结构正确、质感真实、无多指"],
    "product": ["主体边缘锐利、无杂点、背景干净", "反光均匀、透视正确、无变形"],
    "culture": ["构图留白合理、线条完整、层次分明", "纹样完整、色调统一、无畸变"],
}

# 解说词节奏。提示词库 1.3 第 7 条写的是"按时长 × 4 字/秒分配"，
# 但实测 Windows SAPI 中文音色（Microsoft Huihui）只有 3.2-4.0 字/秒
# （17 字 → 4.66s，22 字 → 5.57s）。按 4 字/秒写出来的稿子配音必然超时，
# 被 atempo 压到 1.35× 上限还是装不进镜头，成片结尾音画错位。
# 因此区分两个常数：
NARRATION_CHARS_PER_SEC = 3.6  # 写稿用：片长 → 解说词总字数（按实测语速）
CHARS_PER_SEC = 4.2            # 分镜用：每镜字数预算（≈ 1.17× 变速余量）
MIN_SHOT_CHARS = 6
MAX_SHOT_CHARS = 26
# 空镜兜底句（正常情况下用不到，兜住"宁可重复也不能没声音"这条底线）
EMPTY_SHOT_FILLER = ("镜头继续。", "画面还在走。", "故事没有停。")


def distribute_narration(sentences: list[str], durations: list[float]) -> list[str]:
    """按每镜时长把解说词切开，保证每镜的字数与其时长匹配、且不出现空镜。

    大模型（和离线引擎）都会把一整句话直接丢给一个镜头，导致 5 秒镜头塞 47 个字；
    这里做硬约束。两条规则：
    1. **全局预算**：总字数按各镜时长等比分配，余量多的镜头先拿（避免前几镜吃光、
       末镜只剩 0 字）；
    2. **切句优先在标点处**：必须截断时找最近的逗号/顿号，找不到才硬切。
    """
    if not durations:
        return []
    # 每镜的字数预算（舒适值），以及硬上限（= 语速 × 最大变速倍数）。
    # 硬上限之内 atempo 一定压得进镜头；超过就会顶错画面。
    budgets = [max(MIN_SHOT_CHARS, int(d * CHARS_PER_SEC)) for d in durations]
    caps = [
        max(budget, int(d * NARRATION_CHARS_PER_SEC * MAX_TEMPO))
        for d, budget in zip(durations, budgets)
    ]
    out: list[str] = [""] * len(durations)
    pool = [s for s in sentences if s.strip()]

    # 第一轮：按镜头顺序贪心填整句，不超舒适预算
    for i, budget in enumerate(budgets):
        while pool and len(out[i]) + len(pool[0]) <= budget:
            out[i] += pool.pop(0)

    # 第二轮：剩下的整句塞进剩余空间最大的镜头（可超舒适预算，但不得超硬上限）。
    # 不做句中硬切——切开会造出「…手腕一抖」这种半截话，比少说一句更难看。
    leftovers: list[str] = []
    for nxt in pool:
        for i in sorted(range(len(out)), key=lambda k: caps[k] - len(out[k]), reverse=True):
            if len(out[i]) + len(nxt) <= caps[i]:
                out[i] += nxt
                break
        else:
            leftovers.append(nxt)  # 装不下就整句丢弃：宁可少说，不要音画错位

    # 兜底：任何空镜用落点句填上（宁可重复，也不能出现无声镜头）。
    # 按镜头序号取不同的兜底句，避免连着两镜都是同一句"镜头继续。"。
    base = "|".join(sentences) or "filler"
    for i, chunk in enumerate(out):
        if not chunk.strip():
            out[i] = stable_choice(EMPTY_SHOT_FILLER, f"{base}#{i}")
    return out


MOTION_POOL = [
    ("slow push-in", "steam drifting upward", "slow and gentle"),
    ("slow pull-back", "cloth fluttering slightly", "cinematic pacing"),
    ("slow pan right", "dust particles drifting in light", "subtle motion"),
    ("subtle handheld sway", "light shifting across the surface", "rhythmic"),
    ("tilt down", "hands moving steadily", "slow and gentle"),
    ("static camera", "soft environmental motion only", "very subtle motion"),
]


def _normalize_shot(
    raw: dict[str, Any],
    order: int,
    *,
    brief: ProjectBrief,
    genre: Genre,
    default_wf: str,
    subjects: list[Subject],
    sentences: list[str],
    avg_shot_len: float,
    total: int,
    fallback_act: str = "",
) -> Shot:
    """把模型给的一个镜头补成合法契约。

    抽成独立函数是为了让「整片拆」和「按场重拆」走**同一份**补全规则：
    两边各写一套的话，重拆出来的镜头会缺 prompt_image、acceptance 退化成主观词，
    而这些字段正是下游质检要用的，缺了不会报错，只会让成片质量莫名变差。
    """
    idx = order + 1
    visual = str(raw.get("visual") or "").strip() or (
        f"{genre.subjects[order % len(genre.subjects)]}，"
        f"{genre.actions[order % len(genre.actions)]}"
    )
    cam_cn, cam_en = CAMERA_CYCLE[order % len(CAMERA_CYCLE)]
    camera = str(raw.get("camera") or cam_cn)
    prompt_image = str(raw.get("prompt_image") or "").strip()
    if not prompt_image:
        prompt_image = compose_image_prompt(genre, visual, brief.style, cam_en, order)
    prompt_video = str(raw.get("prompt_video") or "").strip()
    if not prompt_video:
        prompt_video = compose_motion_prompt(order, visual)
    acceptance = str(raw.get("acceptance") or "").strip()
    if not acceptance or any(bad in acceptance for bad in ("好看", "有感觉", "高级")):
        pool = ACCEPTANCE_POOL.get(genre.key, ACCEPTANCE_POOL["default"])
        acceptance = stable_choice(tuple(pool), f"{brief.theme}{idx}")
    narration = str(raw.get("narration") or "").strip()
    if not narration and sentences:
        narration = sentences[order % len(sentences)]
    refs = [str(r) for r in (raw.get("subject_refs") or []) if str(r) in {s.id for s in subjects}]
    return Shot(
        id=idx,
        act=str(raw.get("act") or fallback_act or _act_for(order, total)),
        duration=float(raw.get("duration") or avg_shot_len),
        narration=narration,
        visual=visual,
        camera=camera,
        prompt_image=prompt_image,
        prompt_video=prompt_video,
        subject_refs=refs,
        workflow_id=str(raw.get("workflow_id") or default_wf),
        acceptance=acceptance[:24],
    )


def _normalize_storyboard(
    data: dict[str, Any],
    brief: ProjectBrief,
    script: Script,
    avg_shot_len: float,
    workflows: list[dict[str, Any]],
) -> Storyboard:
    """把模型/离线引擎给的分镜补全为合法契约，并强制时长与结构约束。"""
    project = dict(data.get("project") or {})
    project.setdefault("title", script.title)
    project.setdefault("style", brief.style)
    project.setdefault("aspect_ratio", brief.aspect_ratio)
    project["total_duration"] = brief.total_duration

    subjects: list[Subject] = []
    for raw_sub in data.get("subjects") or []:
        if isinstance(raw_sub, dict) and raw_sub.get("id"):
            subjects.append(
                Subject(
                    id=str(raw_sub["id"]),
                    name=str(raw_sub.get("name") or raw_sub["id"]),
                    visual_lock=str(raw_sub.get("visual_lock") or ""),
                )
            )

    raw_shots = data.get("shots") or []
    if not raw_shots:
        raise LLMError("分镜缺少 shots 字段")

    genre = pick_genre(brief.theme + " " + brief.style)
    default_wf = _workflow_for(brief, genre, workflows)
    shots: list[Shot] = []
    sentences = split_sentences(script.narration_full)
    for i, raw in enumerate(raw_shots):
        if not isinstance(raw, dict):
            continue
        shots.append(
            _normalize_shot(
                raw,
                i,
                brief=brief,
                genre=genre,
                default_wf=default_wf,
                subjects=subjects,
                sentences=sentences,
                avg_shot_len=avg_shot_len,
                total=len(raw_shots),
            )
        )
    if not shots:
        raise LLMError("分镜 shots 全部非法")
    build_timeline(shots, brief.total_duration)
    _enforce_narration_pacing(shots, script)
    return Storyboard(project=project, subjects=subjects, shots=shots)


def _act_units(text: str) -> list[str]:
    """把一场的文字切成能装进单个镜头的短语。

    `split_sentences` 只按句末标点切，而剧本里一场戏常常就是**一整句 30 字的长句**。
    4 秒镜头的字数硬上限只有 19 字左右，整句塞不进去，`distribute_narration`
    会把它整句丢掉（"宁可少说，不要音画错位"），最后每个镜头都落进
    「画面还在走。」这类兜底句——表现为"重拆了，台词却全是废话"。

    所以这里先按句末切，再把过长的句子按逗号/顿号拆成小句。拆分保留了标点，
    拼回去仍是通顺的原句。
    """
    units: list[str] = []
    for sentence in split_sentences(text or ""):
        if len(sentence) <= 12:
            units.append(sentence)
            continue
        parts = [p for p in re.split(r"(?<=[，、；,;])", sentence) if p.strip()]
        units.extend(parts or [sentence])
    return units


def _enforce_narration_pacing(
    shots: list[Shot],
    script: Script | None = None,
    text: str | None = None,
    clauses: bool = False,
) -> None:
    """强制解说词节奏（提示词库 1.3 第 7 条）。

    之前只有离线引擎走 distribute_narration，真实模型返回的分镜是原样照收，
    于是出现「5 秒镜头塞 47 个字」——配音被压到 1.35 倍速还说不完，成片音画错位。
    现在两条路径共用同一道闸：空镜或超预算就按镜头时长重新分配。

    ``text`` 给了就只用它当字数来源，不读 ``script.narration_full``。
    按场重拆时必须这样：拿整片解说词去重新分配，会把别的场的词搬进这一场。

    ``clauses=True`` 用 :func:`_act_units` 切成小句再分配，给单场重拆用——
    整片走的是"一句一镜"的老逻辑，不动它。
    """
    if not shots:
        return
    need_fix = False
    for s in shots:
        budget = max(MIN_SHOT_CHARS, int(s.duration * CHARS_PER_SEC))
        if not (s.narration or "").strip() or len(s.narration) > budget + 8:
            need_fix = True
            break
    if not need_fix:
        return
    source = text if text is not None else (script.narration_full if script else "")
    units = _act_units(source) if clauses else split_sentences(source)
    fixed = distribute_narration(units, [s.duration for s in shots])
    if len(fixed) != len(shots):
        return
    for s, text in zip(shots, fixed):
        s.narration = text


def _act_for(index: int, total: int) -> str:
    if total <= 1:
        return "起"
    ratio = index / total
    if ratio < 0.18:
        return "起"
    if ratio < 0.6:
        return "承"
    if ratio < 0.85:
        return "转"
    return "合"


def _workflow_for(brief: ProjectBrief, genre: Genre, workflows: list[dict[str, Any]]) -> str:
    if workflows:
        for wf in workflows:
            caps = wf.get("capabilities") or []
            if genre.key in caps or "txt2img" in caps:
                return str(wf.get("workflow_id") or wf.get("id") or "wf_txt2img")
    return f"wf_txt2img_{genre.key}_v1"


def compose_image_prompt(genre: Genre, visual: str, style: str, camera_en: str, index: int) -> str:
    """文生图提示词公式：**[用户写的主体]**, [光线], [构图景别], [风格媒介], [质量词]

    **这里曾经有一个很严重的错误**：原来的实现把主体和动作取自
    `genre.subjects` / `genre.actions`（题材模板里的固定词），而 `visual`
    ——也就是用户/剧本真正写的那句话——**只被当作 stable_choice 的哈希种子**，
    根本没进提示词。结果就是：无论用户写"手挑面条"还是"老人坐在门槛上"，
    出图里永远是模板那个"一碗热汤面 + 掀帘进门"。

    现在改为：**`visual` 原文放最前面作为主体**，模板词退成补充修饰
    （只保留 look/light 这类不改变语义的词）。用户的意思一个字都不动，
    系统只在后面追加修饰——这是"忠实模式"的基础。
    """
    look = stable_choice(genre.look, f"{visual}{index}")
    light = stable_choice(
        ("golden hour", "warm rim light", "soft window light", "dramatic side light", "practical warm lights"),
        f"{visual}light{index}",
    )
    subject = (visual or "").strip()
    if not subject:
        # 兜底：剧本没给画面描述时才退回模板，保证不会空提示词
        subject = f"{genre.subjects[index % len(genre.subjects)]} ({genre.label})"
    parts = [
        subject,
        look,
        light,
        camera_en,
        f"{genre.label} style, {_en_style(style)}",
        "masterpiece, best quality, highly detailed, sharp focus, film grain",
    ]
    # 上限从 600 放到 900：用户描述可能很长，宁可长一点也别切掉意思
    return ", ".join(p for p in parts if p)[:900]


def _en_style(style: str) -> str:
    mapping = {
        "烟火气": "documentary food photography",
        "暖色调": "warm color grading",
        "纪实风": "observational documentary",
        "电影感": "cinematic still",
        "国风": "oriental aesthetics",
    }
    hits = [v for k, v in mapping.items() if k in style]
    return ", ".join(hits) if hits else "documentary photography"


def compose_motion_prompt(index: int, visual: str) -> str:
    """视频运动提示词公式：[镜头运动],[主体动作],[环境动态],[氛围速度]"""
    cam, env, pace = MOTION_POOL[index % len(MOTION_POOL)]
    return f"{cam}, {env}, {pace}, natural motion, no scene change"[:300]


# ------------------------------------------------------------------ 离线引擎
def _offline_brief(raw_input: str) -> dict[str, Any]:
    genre = pick_genre(raw_input)
    tokens = theme_tokens(raw_input)
    return {
        "theme": f"{tokens['theme']}短片" if "短" not in tokens["theme"] else tokens["theme"],
        "total_duration": 15,
        "aspect_ratio": "16:9",
        "style": genre.tone,
        "language": "zh",
        "must_include": [tokens["subject"]],
        "must_avoid": [],
        "reference": "",
    }


def _offline_script(brief: ProjectBrief) -> dict[str, Any]:
    """按题材词库拼出有信息量的解说词：钩子 → 细节 → 情绪 → 落点。

    句式硬约束：不把"的"硬插进主体词，避免出现"一位老木的匠的故事"这类病句；
    题材句池有 12 句，短镜头数时不靠重复凑数。
    """
    genre = pick_genre(brief.theme + brief.style)
    tokens = theme_tokens(brief.theme)
    place, subject = tokens["place"], tokens["subject"]
    # 解说词总量按**实测语速**定，而不是拍脑袋的 4 字/秒：
    # 稿子写长了，配音压到限速上限也装不进镜头，成片结尾必然音画错位。
    target_chars = max(24, int(brief.total_duration * NARRATION_CHARS_PER_SEC))
    # 至少 4 句：起承转合四段各要有内容
    min_units = 4
    pool = _narration_pool(genre, place, subject)
    sentences: list[str] = []
    used = 0
    for line in pool:
        if len(sentences) >= min_units and used + len(line) > target_chars:
            break
        sentences.append(line)
        used += len(line)
    while len(sentences) < min_units:
        sentences.append(
            stable_choice(genre.values, f"{subject}{len(sentences)}") + "，就是它最该被看见的地方。"
        )
    narration = "".join(sentences)
    n = len(sentences)
    marks = [0, max(1, int(n * 0.18)), max(2, int(n * 0.6)), max(3, int(n * 0.85)), n]
    names = ["起", "承", "转", "合"]
    acts = []
    for i, name in enumerate(names):
        start = brief.total_duration * marks[i] / n
        end = brief.total_duration * marks[i + 1] / n
        chunk = "".join(sentences[marks[i] : marks[i + 1]])
        acts.append(
            {
                "name": name,
                "time_range": f"{start:.0f}-{end:.0f}s",
                "content": chunk[:60] or genre.tone,
            }
        )
    return {
        "title": f"{subject}·{genre.label[:2]}",
        "logline": f"{brief.total_duration} 秒，讲清{subject}最动人的一面",
        "tone": genre.tone,
        "target_audience": "本地受众与泛兴趣观众",
        "hook": genre.hook,
        "acts": acts,
        "narration_full": narration,
        "key_subjects": [subject, genre.subjects[0]],
    }


def _narration_pool(genre: Genre, place: str, subject: str) -> list[str]:
    """每题材 12 句独立句子：开场 2 + 细节 4 + 情绪 4 + 落点 2。

    句式刻意避免"主体 + 的 + 名词"的拼接，从根上杜绝"一位老木的匠的故事"这类病句；
    place 为空时不硬凑地点（避免"老木老木匠"）。
    """
    detail = genre.details[0]
    detail2 = genre.details[1 % len(genre.details)]
    detail3 = genre.details[2 % len(genre.details)]
    action1 = genre.actions[0]
    action2 = genre.actions[1 % len(genre.actions)]
    action3 = genre.actions[2 % len(genre.actions)]
    value = genre.values[0]
    mood = genre.emotions[0]
    mood2 = genre.emotions[-1]
    scope = f"{place}的" if place else ""
    lines = [
        f"{scope}{subject}，先从一个细节说起。",
        genre.hook,
        f"{detail}，是这里最不声张的讲究。",
        f"{action1}的那一下，{detail2}跟着动起来。",
        f"镜头拉近，{detail3}。",
        f"{action2}的这几秒，比什么话都有说服力。",
        # value 本身可能以「把」开头（如"把时间留成样子"），
        # 直接套模板会拼出"却把把时间…"这种叠字病句，先剥掉。
        f"它不喧哗，却把{value[1:] if value.startswith('把') else value}留了下来。",
        f"对{scope}人来说，这就是{mood}的来源。" if place else f"对它来说，这就是{mood}的来源。",
        f"慢下来才看得清，{mood2}是攒出来的。",
        f"{action3}，日子在这里有它自己的节奏。",
        genre.peak,
        genre.close,
    ]
    return lines


def _offline_storyboard(brief: ProjectBrief, script: Script, avg_shot_len: float) -> dict[str, Any]:
    genre = pick_genre(brief.theme + brief.style)
    tokens = theme_tokens(brief.theme)
    subject = tokens["subject"]
    n_shots = max(3, int(round(brief.total_duration / max(2.0, avg_shot_len))))
    sentences = split_sentences(script.narration_full)
    per_shot_dur = [round(brief.total_duration / n_shots, 2)] * n_shots
    # 按镜头时长分配解说词（提示词库 1.3 第 7 条的节奏约束）
    per_shot = distribute_narration(sentences, per_shot_dur)
    if not any(per_shot):
        per_shot = [genre.hook] * n_shots

    shots = []
    for i in range(n_shots):
        subj = genre.subjects[i % len(genre.subjects)]
        act = genre.actions[i % len(genre.actions)]
        cam_cn, cam_en = CAMERA_CYCLE[i % len(CAMERA_CYCLE)]
        pool = ACCEPTANCE_POOL.get(genre.key, ACCEPTANCE_POOL["default"])
        shots.append(
            {
                "id": i + 1,
                "act": _act_for(i, n_shots),
                "duration": round(brief.total_duration / n_shots, 2),
                "narration": per_shot[i],
                "visual": f"{subj}：{act}；{genre.details[i % len(genre.details)]}",
                "camera": cam_cn,
                "prompt_image": "",
                "prompt_video": "",
                "subject_refs": [s.id for s in _offline_subjects(genre, subject, brief)][:1],
                "workflow_id": "",
                "acceptance": stable_choice(tuple(pool), f"{subject}{i}"),
            }
        )
    return {
        "project": {
            "title": script.title,
            "style": brief.style,
            "aspect_ratio": brief.aspect_ratio,
            "total_duration": brief.total_duration,
        },
        "subjects": [
            {"id": s.id, "name": s.name, "visual_lock": s.visual_lock}
            for s in _offline_subjects(genre, subject, brief)
        ],
        "shots": shots,
    }


def _offline_act_shots(
    brief: ProjectBrief,
    script: Script,
    act_name: str,
    act_text: str,
    target_seconds: float,
    avg_shot_len: float,
) -> dict[str, Any]:
    """离线引擎的单场重拆。

    没有模型也要能重拆——否则没配 Key 的用户一按按钮就报错，而"改一句话重拆
    这一场"本身不需要模型也能做对一半：镜数按时长摊，解说词按新时长重切。
    画面描述用题材词库轮转，比原样不动强，但确实只是兜底。
    """
    genre = pick_genre(brief.theme + brief.style)
    n_shots = max(1, min(12, int(round(target_seconds / max(1.0, avg_shot_len)))))
    durations = [round(target_seconds / n_shots, 3)] * n_shots
    sentences = _act_units(act_text) or _act_units(script.narration_full)
    per_shot = distribute_narration(sentences, durations) if sentences else [""] * n_shots
    if not any(per_shot):
        per_shot = [genre.hook] * n_shots

    # 画面描述**取自本场文字**，不套题材词库的固定池。
    # 用户改的就是这一场的画面，重拆后画面描述若还是原来那几句通用句，
    # 等于"改了但没生效"——而这是没有模型时唯一能保住的东西。
    clauses = [c.strip() for c in re.split(r"[，。；！？,;!?\n]+", act_text or "") if c.strip()]

    shots = []
    for i in range(n_shots):
        cam_cn, _ = CAMERA_CYCLE[i % len(CAMERA_CYCLE)]
        if clauses:
            visual = clauses[i % len(clauses)]
            if len(clauses) > n_shots:
                # 句子比镜头多时把相邻的并起来，别把后半场的话整段丢掉
                visual = "，".join(clauses[i::n_shots])
        else:
            visual = (f"{genre.subjects[i % len(genre.subjects)]}："
                      f"{genre.actions[i % len(genre.actions)]}；"
                      f"{genre.details[i % len(genre.details)]}")
        shots.append(
            {
                "act": act_name,
                "duration": durations[i],
                "narration": per_shot[i],
                "visual": visual,
                "camera": cam_cn,
                "prompt_image": "",
                "prompt_video": "",
                "subject_refs": [],
                "workflow_id": "",
                "acceptance": "",
            }
        )
    return {"shots": shots}


def _offline_subjects(genre: Genre, subject: str, brief: ProjectBrief) -> list[Subject]:
    return [
        Subject(
            id="hero",
            name=subject,
            visual_lock=f"{subject}：{genre.look[0] if genre.look else 'consistent appearance'}，全片保持同一形态与配色",
        ),
        Subject(
            id="scene",
            name=f"{clean_theme(brief.theme)}场景",
            visual_lock=f"环境基调：{genre.palette[-1][1]} 暖调，光位统一",
        ),
    ]
