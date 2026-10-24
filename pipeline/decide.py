"""一言片场 · 决策层（快思考）。

两条实现，同一个接口：
- JevClient：调用 Jev 官方 REST（`jevai.org/docs` 的六个端点），请求体从提示词库 2.2-2.8 的示例改写而来
- RuleDecider：不联网时的本地规则引擎

**关键设计**：阈值与升级规则（2.4/2.6 的表格）写死在管线代码里，
无论 Jev 返回什么，都要过一遍 `apply_escalation`。Jev 是建议，管线是保险。

本模块不写死任何提示词文本：instructions/criteria 全部来自 prompts/prompts.yaml。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

from .prompts import PromptLibrary

# 提示词库 2.4 的阈值表：score → 管线动作
# 4=excellent / 3=pass → accept；2=marginal → retry；1=reject → 拒
QUALITY_VERDICT = {4: "accept", 3: "accept", 2: "retry", 1: "reject"}
# 缺陷条数 → 分数上限（0 处缺陷才有资格拿满分）
DEFECT_SCORE_CAP = {0: 4, 1: 3, 2: 2, 3: 1}
# 质检分数字典：模型可能按文档返回标签而不是数字（提示词库 2.4 的 criteria 就是标签）
QUALITY_LABELS = {
    "excellent": 4, "pass": 3, "marginal": 2, "reject": 1,
    "good": 3, "ok": 3, "poor": 1, "bad": 1, "fail": 1,
}
JEV_ENDPOINTS = {
    "model_route": "/api/v1/decisions/model-route",
    "route_task": "/api/v1/decisions/route",
    "quality": "/api/v1/decisions",
    "pick_best": "/api/v1/decisions",
    "retry": "/api/v1/decisions",
    "completion": "/api/v1/decisions/completion",
    "tool_guard": "/api/v1/decisions/tool-guard",
}
# 提示词库 2.2 的 confidence 阈值
MIN_ROUTE_CONFIDENCE = 0.6


def coerce_quality(review: dict) -> int:
    """把决策层返回的分数容错解析成 1-4（失败返回 0 = 无法判定）。

    真实模型可能返回 4 / "4" / "excellent" / {"score": 4} / 完全缺失，
    这里全部兜住——历史上解析失败会让整条运行崩掉。
    """
    for key in ("quality", "score", "rating", "grade"):
        if key in review and review[key] is not None:
            value = review[key]
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                return max(1, min(4, int(round(float(value)))))
            text = str(value).strip().lower()
            if text in QUALITY_LABELS:
                return QUALITY_LABELS[text]
            try:
                return max(1, min(4, int(round(float(text)))))
            except ValueError:
                continue
    verdict = str(review.get("verdict") or review.get("decision") or "").strip().lower()
    if verdict in ("accept", "accepted", "approve", "approved"):
        return 3
    if verdict in ("retry", "regenerate"):
        return 2
    if verdict in ("reject", "rejected"):
        return 1
    return 0


def judge(review: dict) -> tuple[int, str]:
    """**管线侧终审**：分数说了算，不采信决策层给的 verdict。

    铁律：阈值表写死在管线里，Jev（或任何实现）只能提供分数，不能自己宣布通过。
    这样即使决策层返回 {"quality": 1, "verdict": "accept"}，画面也进不了成片。
    """
    score = coerce_quality(review)
    if score <= 0:
        return 0, "review"  # 无法判定 → 转复核，不放行
    return score, QUALITY_VERDICT[score]


class Decider(Protocol):
    source: str

    def model_route(self, task: str, candidates: list[dict], priorities: list[str],
                    constraints: list[str], stakes: str) -> dict: ...
    def route_task(self, task: str, state: dict) -> dict: ...
    def review_quality(self, state: dict) -> dict: ...
    def pick_best(self, state: dict) -> dict: ...
    def retry_strategy(self, state: dict) -> dict: ...
    def completion(self, objective: str, evidence: str) -> dict: ...
    def tool_guard(self, tool: str, action: str, arguments_summary: list[str],
                   side_effects: list[str], safeguards: list[str], policy: list[str],
                   reversibility: str) -> dict: ...


@dataclass
class DecisionTrace:
    point: str
    request: dict
    response: dict
    source: str
    latency_ms: float


# ==================================================================== 本地规则
class RuleDecider:
    """离线规则决策器。所有判断都可复现、可审计，规则编号对应提示词库章节。"""

    source = "rule"

    def __init__(self, lib: PromptLibrary | None = None, weights: dict[str, float] | None = None):
        self.lib = lib
        self.weights = weights or {"quality": 0.5, "cost": 0.3, "latency": 0.2}

    # -------------------------------------------------- 2.2 模型路由
    def model_route(self, task: str, candidates: list[dict], priorities: list[str],
                    constraints: list[str], stakes: str) -> dict:
        prio = priorities or ["quality", "cost", "latency"]
        w = {k: self.weights.get(k, 0.2) for k in prio}
        total_w = sum(w.values()) or 1.0
        local_only = any("local" in c.lower() or "显存" in c for c in constraints)
        budget_tight = any(k in c.lower() for c in constraints for k in ("budget: low", "预算"))

        scored = []
        for cand in candidates:
            desc = str(cand.get("description", "")).lower()
            cid = str(cand.get("id", ""))
            # 基础分
            quality = 0.55
            if any(k in desc for k in ("best", "high quality", "highest", "高质量")):
                quality = 0.9
            elif any(k in desc for k in ("medium", "standard")):
                quality = 0.6
            latency = 0.5
            if any(k in desc for k in ("fast", "~3s", "3s", "quick")):
                latency = 0.9
            elif any(k in desc for k in ("slow", "~8s", "8s", "60s")):
                latency = 0.3
            cost = 0.5
            if any(k in desc for k in ("near-zero", "free", "local", "local only")):
                cost = 0.95
            elif any(k in desc for k in ("billed", "paid", "per image", "按量", "计费")):
                cost = 0.25
            score = (
                w.get("quality", 0) * quality
                + w.get("cost", 0) * cost
                + w.get("latency", 0) * latency
            ) / total_w
            # 约束惩罚
            is_cloud = cid.startswith("cloud") or "cloud" in cid
            if local_only and is_cloud:
                score -= 0.5
            if budget_tight and is_cloud:
                score -= 0.3
            if stakes == "high" and quality >= 0.9:
                score += 0.15
            scored.append((score, cid, cand))
        scored.sort(key=lambda x: (-x[0], x[1]))
        best = scored[0]
        runner = scored[1] if len(scored) > 1 else None
        margin = best[0] - (runner[0] if runner else 0.0)
        confidence = round(min(0.95, 0.45 + margin * 1.6), 2)
        response = {
            "decision": best[1],
            "confidence": confidence,
            "guidance": f"rule-engine: score={best[0]:.3f}, margin={margin:.3f}, priorities={prio}",
            "probabilities": {cid: round(max(0.0, s), 3) for s, cid, _ in scored},
        }
        if confidence < MIN_ROUTE_CONFIDENCE and runner:
            response["guidance"] += "；信心不足，按默认候选执行并标记待人工复核（提示词库 2.2 阈值）"
            response["needs_review"] = True
        return response

    # -------------------------------------------------- 2.3 任务分级
    def route_task(self, task: str, state: dict) -> dict:
        is_key = bool(state.get("is_key_shot"))
        has_ref = bool(state.get("has_subject_ref"))
        attempts = state.get("attempt_history") or []
        if len(attempts) >= 3 and all(a.get("defect_type") == attempts[0].get("defect_type") for a in attempts):
            return {"route": "block", "guidance": "同一缺陷连续 3 次，风险过高，转人工确认"}
        if is_key or has_ref:
            return {
                "route": "deep_review",
                "guidance": "关键镜头或含主体引用：生成 3 候选 → N 选 1 → 加严质检（提示词库 2.3）",
            }
        if int(state.get("expected_candidates") or 1) > 8:
            return {"route": "split_task", "guidance": "候选过多，先分批再裁决"}
        return {"route": "proceed_fast", "guidance": "简单镜头：直接生成，不进入 N 选 1"}

    # -------------------------------------------------- 2.1/2.4 质检
    def review_quality(self, state: dict) -> dict:
        desc = str(state.get("candidate_desc", ""))
        defects = _defect_count(desc)
        defect_type = _defect_type(desc)
        attempt = int(state.get("attempt") or 1)
        # 先按验收标准逐条核对（可核对项：清晰度/畸变/色调/目标元素）
        checks = _acceptance_checks(str(state.get("acceptance", "")), desc)
        passed = sum(1 for c in checks if c["ok"])
        total = len(checks) or 1
        base = 1 + round(3 * passed / total)
        # 缺陷条数是硬约束（提示词库 2.4：marginal 才值得同提示词重试，1 分要改词）
        score = min(base, DEFECT_SCORE_CAP.get(defects, 1))
        score = max(1, min(4, score))
        verdict = QUALITY_VERDICT[score]
        return {
            "quality": score,
            "verdict": verdict,
            "defect_type": defect_type,
            "defects": defects,
            "checks": checks,
            "guidance": f"rule-engine: {passed}/{total} 项验收通过，可见缺陷 {defects} 处，第 {attempt} 次尝试",
        }

    # -------------------------------------------------- 2.5 N 选 1
    def pick_best(self, state: dict) -> dict:
        cands = state.get("candidates") or []
        if not cands:
            return {"best": "1", "margin": "negligible", "guidance": "无候选"}
        accept = str(state.get("acceptance", ""))
        scored = []
        for cand in cands:
            desc = str(cand.get("desc", ""))
            checks = _acceptance_checks(accept, desc)
            passed = sum(1 for c in checks if c["ok"])
            scored.append((-passed + _defect_count(desc) * 0.5, str(cand.get("index")), passed))
        scored.sort(key=lambda x: (x[0], x[1]))
        best = scored[0]
        runner = scored[1] if len(scored) > 1 else None
        gap = (runner[0] - best[0]) if runner else 2.0
        margin = "negligible" if gap < 0.5 else "slight" if gap < 1.0 else "clear" if gap < 2.0 else "decisive"
        return {
            "best": best[1],
            "margin": margin,
            "guidance": f"rule-engine: 候选 {best[1]} 验收通过 {best[2]} 项，与次优差距 {gap:.2f}"
            + ("；候选区分度不足，记入日志供提示词复盘（提示词库 2.5）" if margin in ("negligible", "slight") else ""),
        }

    # -------------------------------------------------- 2.6 重试策略
    def retry_strategy(self, state: dict) -> dict:
        defect = str(state.get("defect_type") or "none")
        attempt = int(state.get("attempt") or 1)
        max_attempts = int(state.get("max_attempts") or 4)
        history = state.get("attempt_history") or []
        action, why = "retry_same", "首次失败，换种子重试成本最低"
        # 顺序与 apply_escalation 保持一致：同缺陷连击优先于次数上限
        same = 1 + sum(1 for h in history if h.get("defect_type") == defect)
        if defect != "none" and same >= 3:
            action, why = "skip", f"同一缺陷（{defect}）连续 {same} 次，跳过并转人工复核"
        elif defect != "none" and same >= 2:
            action, why = "rewrite_prompt", f"同一缺陷（{defect}）连续 {same} 次，强制改写提示词"
        elif attempt >= max_attempts:
            action, why = "switch_model", f"已达最大尝试次数 {max_attempts}，强制换模型（提示词库 2.6）"
        focus = _focus_for(defect)
        return {"action": action, "focus": focus, "guidance": why}

    # -------------------------------------------------- 2.7 完成度
    def completion(self, objective: str, evidence: str) -> dict:
        low = evidence.lower()
        missing = "missing" in low or "缺失" in evidence
        complete = not missing
        return {
            "status": "complete" if complete else "incomplete",
            "complete": complete,
            "guidance": evidence,
        }

    # -------------------------------------------------- 2.8 高危护栏
    def tool_guard(self, tool: str, action: str, arguments_summary: list[str],
                   side_effects: list[str], safeguards: list[str], policy: list[str],
                   reversibility: str) -> dict:
        text = " ".join(safeguards).lower()
        if "budget check passed" not in text and "预算" not in " ".join(safeguards):
            return {"decision": "deny", "guidance": "缺少预算校验证据，禁止付费调用（提示词库 2.8）"}
        if "quality review" not in text and "质检" not in " ".join(safeguards):
            return {"decision": "deny", "guidance": "画面未过质检，禁止付费调用"}
        if "stakes=high" in text or any("human confirm" in s.lower() for s in safeguards):
            return {"decision": "confirm", "guidance": "高风险镜头需人工确认后再付费"}
        return {"decision": "allow", "guidance": "护栏通过：预算与质检证据齐全"}


# ==================================================================== 真实 Jev
@dataclass
class JevClient:
    """Jev 官方 REST 客户端。请求体基于提示词库 2.2-2.8 的示例构造。"""

    base_url: str
    api_key: str
    lib: PromptLibrary
    source: str = "jev"
    timeout: int = 30
    max_retries: int = 2
    last_latency_ms: float = 0.0
    errors: list[str] = field(default_factory=list)

    def _post(self, endpoint: str, payload: dict) -> dict:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(raw) > 32 * 1024:
            raise ValueError(f"请求体 {len(raw) / 1024:.1f} KiB 超过 Jev 32 KiB 上限")
        url = self.base_url.rstrip("/") + endpoint
        last: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            t0 = time.perf_counter()
            req = urllib.request.Request(
                url,
                data=raw,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                self.last_latency_ms = (time.perf_counter() - t0) * 1000
                if isinstance(body, dict) and "code" in body and body.get("code") not in (0, None):
                    raise ValueError(f"Jev 返回 code={body.get('code')} message={body.get('message')}")
                data = body.get("data", body) if isinstance(body, dict) else body
                return data if isinstance(data, dict) else {"raw": data}
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "ignore")[:200]
                last = RuntimeError(f"HTTP {exc.code}: {detail}")
                if 400 <= exc.code < 500 and exc.code != 429:
                    break
            except Exception as exc:  # noqa: BLE001
                last = exc
            time.sleep(0.8 * attempt)
        self.errors.append(str(last))
        raise RuntimeError(f"Jev 调用失败（{endpoint}）：{last}")

    # 各决策点：把管线状态填进提示词库里的请求体模板
    def _payload(self, section: str, overrides: dict[str, Any]) -> dict:
        base = json.loads(self.lib.request_example(section))
        base.update(overrides)
        return base

    def model_route(self, task: str, candidates: list[dict], priorities: list[str],
                    constraints: list[str], stakes: str) -> dict:
        payload = self._payload(
            "2.2",
            {"task": task, "candidates": candidates, "priorities": priorities,
             "constraints": constraints, "stakes": stakes},
        )
        return self._post(JEV_ENDPOINTS["model_route"], payload)

    def route_task(self, task: str, state: dict) -> dict:
        return self._post(JEV_ENDPOINTS["route_task"], self._payload("2.3", {"task": task, "state": state}))

    def review_quality(self, state: dict) -> dict:
        return self._post(JEV_ENDPOINTS["quality"], self._payload("2.4", {"state": state}))

    def pick_best(self, state: dict) -> dict:
        return self._post(JEV_ENDPOINTS["pick_best"], self._payload("2.5", {"state": state}))

    def retry_strategy(self, state: dict) -> dict:
        return self._post(JEV_ENDPOINTS["retry"], self._payload("2.6", {"state": state}))

    def completion(self, objective: str, evidence: str) -> dict:
        return self._post(
            JEV_ENDPOINTS["completion"], self._payload("2.7", {"objective": objective, "evidence": evidence})
        )

    def tool_guard(self, tool: str, action: str, arguments_summary: list[str],
                   side_effects: list[str], safeguards: list[str], policy: list[str],
                   reversibility: str) -> dict:
        payload = self._payload(
            "2.8",
            {"tool": tool, "action": action, "arguments_summary": arguments_summary,
             "side_effects": side_effects, "safeguards": safeguards, "policy": policy,
             "reversibility": reversibility},
        )
        return self._post(JEV_ENDPOINTS["tool_guard"], payload)


# ==================================================================== 保险层
def apply_escalation(response: dict, state: dict) -> dict:
    """把提示词库 2.6 的升级规则强制套在（Jev 或规则的）返回之上。

    Jev 是建议，管线是保险：只允许把动作**升级**，不允许降级。
    动作名先归一化（大小写/别名），未知动作按 retry_same 处理，避免被误判成"更轻"的动作。
    """
    order = {"retry_same": 0, "rewrite_prompt": 1, "switch_model": 2, "skip": 3}
    aliases = {
        "retry": "retry_same", "retrysame": "retry_same", "same": "retry_same",
        "rewrite": "rewrite_prompt", "prompt": "rewrite_prompt", "rewriteprompt": "rewrite_prompt",
        "switch": "switch_model", "switchmodel": "switch_model", "model": "switch_model",
        "skip": "skip", "give_up": "skip", "giveup": "skip",
    }
    raw_action = str(response.get("action") or "retry_same").strip().lower().replace(" ", "_")
    action = aliases.get(raw_action.replace("_", ""), aliases.get(raw_action, "retry_same"))
    attempt = int(state.get("attempt") or 1)
    max_attempts = int(state.get("max_attempts") or 4)
    defect = str(state.get("defect_type") or "none")
    history = state.get("attempt_history") or []

    forced = None
    reason = ""
    # 顺序要紧：**同缺陷连击优先于尝试次数上限**。
    # 旧实现先判 attempt>=max，导致 attempt=3/max=3 时永远返回 switch_model，
    # 而那一轮之后镜头已被判 rejected，"同缺陷×3 → skip"这条规则根本不可达。
    same = 1 + sum(1 for h in history if h.get("defect_type") == defect)
    if defect != "none" and same >= 3:
        forced, reason = "skip", f"同一缺陷（{defect}）连续 {same} 次"
    elif defect != "none" and same >= 2:
        forced, reason = "rewrite_prompt", f"同一缺陷（{defect}）连续 {same} 次"
    elif attempt >= max_attempts:
        forced, reason = "switch_model", f"attempt({attempt}) ≥ max_attempts({max_attempts})"

    if forced and order[forced] > order[action]:
        response = dict(response)
        response["action"] = forced
        response["focus"] = response.get("focus") or _focus_for(defect)
        response["guidance"] = f"{response.get('guidance', '')} [管线强制升级：{reason}]".strip()
        response["escalated"] = True
    return response


# ==================================================================== 辅助
def _defect_count(desc: str) -> int:
    """数画面描述里的缺陷条数。

    历史坑（很隐蔽）：旧实现用字符串扫描 + `strip("[]")` 剥字符，
    对合法 JSON 永远返回 1 —— 导致 0 缺陷也按"有 1 处缺陷"降档，
    质检分数恒为 3 恒 accept，整条"质检 → 重试"闭环变成橡皮图章。
    现在直接按 JSON 解析；解析不了才退回文本启发式。
    """
    data = None
    if isinstance(desc, str):
        try:
            data = json.loads(desc)
        except (ValueError, TypeError):
            data = None
    if isinstance(data, dict):
        defects = data.get("defects")
        if isinstance(defects, list):
            return len([d for d in defects if str(d).strip()])
        if defects is None:
            return 0
        return 1 if str(defects).strip() else 0
    if data is not None:
        return 1 if data else 0
    # 非 JSON：按文本启发式判断
    low = (desc or "").lower()
    if any(k in low for k in ("no defect", "no visible defect", "无缺陷", "未见缺陷")):
        return 0
    if any(k in low for k in ("defect", "deform", "artifact", "畸形", "变形", "缺陷")):
        return 1
    return 0


DEFECT_KEYWORDS = {
    "anatomy": ("deform", "mutat", "finger", "hand", "limb", "rim", "warp", "畸形", "变形"),
    "text_artifact": ("text", "watermark", "logo", "signature", "文字", "水印"),
    "style_drift": ("style", "tone", "palette", "cold", "风格", "色调"),
    "composition": ("composition", "framing", "off-center", "crop", "构图", "偏"),
    "consistency": ("consist", "identity", "reference", "一致"),
}


def _defect_type(desc: str) -> str:
    """只有确实存在缺陷时才判类型；无缺陷必须返回 none（否则会把 'tone: warm' 误判成风格漂移）。"""
    if _defect_count(desc) == 0:
        return "none"
    low = desc.lower()
    for name, words in DEFECT_KEYWORDS.items():
        if any(w in low for w in words):
            return name
    return "none"


def _focus_for(defect: str) -> str:
    return {
        "anatomy": "subject",
        "text_artifact": "negative",
        "style_drift": "lighting",
        "composition": "composition",
        "consistency": "subject",
    }.get(defect, "subject")


def _acceptance_checks(acceptance: str, desc: str) -> list[dict]:
    """把验收标准拆成可核对项，逐条对着画面描述判定。"""
    low = desc.lower()
    items: list[tuple[str, str, tuple[str, ...]]] = [
        ("主体清晰", "sharpness", ("sharp", "clear", "focused", "清晰", "锐利")),
        ("无畸变/变形", "deformation", ("deform", "mutation", "warp", "畸形", "变形", "distort")),
        ("影调/色调达标", "tone", ("warm", "tone", "color", "色调", "温暖", "冷")),
    ]
    if any(k in acceptance for k in ("热气", "蒸汽", "雾", "水汽")):
        items.append(("热气/雾可见", "steam", ("steam", "vapor", "mist", "热气", "蒸汽")))
    if any(k in acceptance for k in ("水平线", "透视", "层次")):
        items.append(("构图/透视正确", "perspective", ("perspective", "horizon", "level", "透视", "水平")))
    if any(k in acceptance for k in ("边缘", "碗沿", "纹样", "线条")):
        items.append(("边缘/线条完整", "edge", ("edge", "rim", "line", "边缘", "线条", "完整")))
    checks = []
    for label, key, words in items:
        if key == "deformation":
            # 出现任何变形词即判不通过
            checks.append({"item": label, "key": key, "ok": not any(w in low for w in words)})
            continue
        if key == "tone":
            # 冷色偏 = 与暖调项目不符
            if any(w in low for w in ("cold", "cool cast", "冷", "偏冷")):
                checks.append({"item": label, "key": key, "ok": False})
                continue
        checks.append({"item": label, "key": key, "ok": any(w in low for w in words)})
    return checks
