"""一言片场 · 配置与运行上下文。

配置优先级：命令行参数 > 环境变量 > 项目根目录 .env > 内置默认值。
密钥只在内存里流转，日志中一律脱敏。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


def resolve_root() -> Path:
    """确定项目根目录（数据与配置的落盘位置）。

    打包成单文件 EXE 后，`__file__` 指向 PyInstaller 的临时解包目录
    （`sys._MEIPASS`，进程退出即消失）。若把 runs/ 写在那里，用户的所有成片
    会在程序关闭后消失。因此这里区分两种形态：

    - 源码运行：项目根 = 源码目录
    - 打包运行：项目根 = **EXE 所在目录**（sys.executable 的父目录），
      数据落在 EXE 旁边，与用户预期一致；若 EXE 在只读位置，退回用户目录下的
      `YiYanStudio` 文件夹。
    """
    frozen = getattr(sys, "frozen", False)
    if frozen:
        exe_dir = Path(sys.executable).resolve().parent
        try:
            probe = exe_dir / ".yiyan_write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return exe_dir
        except Exception:  # noqa: BLE001
            return Path.home() / "YiYanStudio"
    return Path(__file__).resolve().parents[1]


ROOT = resolve_root()
ENV_FILE = ROOT / ".env"
# 源码根目录（打包形态下随包数据不在 ROOT，而在 sys._MEIPASS）
SRC_ROOT = Path(__file__).resolve().parents[1]

#: 用户上传的音轨落盘目录（`POST /api/media/audio` 写到这儿）。
#:
#: 必须在 ROOT 下、不能放进随包资源目录：上传是**运行时数据**，打包后随包目录
#: 是只读的临时解包区，写进去进程一退出就没了。同一个理由让 runs/ 也落在 ROOT。
UPLOADS_DIR = ROOT / "work" / "uploads"


def resource_dir(name: str) -> Path:
    """定位随包数据目录（prompts / workflows / docs）。

    ROOT 是**用户数据区**（EXE 同级，写 runs/ 的地方），
    随包数据却在 PyInstaller 的临时解包目录 sys._MEIPASS 下——两回事。
    曾经到处写 `ROOT / "workflows"`，源码运行没问题，打包后必然找不到，
    表现是"缺少工作流模板 txt2img.json"，导致 EXE 形态永远接不上 ComfyUI。
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        cand = Path(meipass) / name
        if cand.exists():
            return cand
    for base in (SRC_ROOT, ROOT):
        cand = base / name
        if cand.exists():
            return cand
    return ROOT / name

DEFAULTS: dict[str, str] = {
    "LLM_BASE_URL": "https://api.deepseek.com/v1",
    "LLM_MODEL": "deepseek-chat",
    "LLM_API_KEY": "",
    "VLM_MODEL": "",
    # Jev 决策层。官方文档在 docs.typesafe.ai，官方 SystemOne 端点为 api.typesafe.ai；
    # 但 2026-10-14 核查时官方端点全路径返回 HTTP 451，而 jevai.org 的 /api/v1/decisions/*
    # 路径已被文档确认，故默认用后者。切换端点只改这一处。
    "JEV_BASE_URL": "https://www.jevai.org",
    "JEV_API_KEY": "",
    "COMFYUI_BASE_URL": "http://127.0.0.1:8188",
    "VIDEO_API_BASE_URL": "",
    "VIDEO_API_KEY": "",
    "TTS_ENGINE": "auto",
    "BUDGET_CNY": "20",
}

SECRET_KEYS = ("LLM_API_KEY", "JEV_API_KEY", "VIDEO_API_KEY", "VLM_API_KEY")


def load_env_file(path: Path = ENV_FILE) -> dict[str, str]:
    """极简 .env 读取：KEY=VALUE，# 开头为注释，值可带引号。"""
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        data[key.strip()] = val
    return data


@dataclass
class Settings:
    values: dict[str, str] = field(default_factory=dict)

    def get(self, key: str, default: str = "") -> str:
        """取值优先级：**.env > 系统环境变量 > 内置默认值**。

        曾经是「.env 非空才认，否则看环境变量」，导致两个后果：
        1. 界面把某个 Key 清空保存后，若系统里有同名环境变量，值照样还在——
           用户以为删掉了密钥，实际还在用；
        2. 想用 `KEY=` 显式置空来关掉某个端点也做不到。
        现在 .env 里出现过的键一律以 .env 为准（含显式空值）。
        """
        if key in self.values:
            return self.values[key]
        env = os.environ.get(key)
        if env:
            return env
        return DEFAULTS.get(key, default)

    def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(float(self.get(key, str(default))))
        except ValueError:
            return default

    def get_float(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self.get(key, str(default)))
        except ValueError:
            return default

    def has(self, key: str) -> bool:
        return bool(self.get(key, "").strip())

    def masked(self, key: str) -> str:
        val = self.get(key, "")
        if not val:
            return "(未配置)"
        if len(val) <= 8:
            return val[:2] + "***"
        return f"{val[:4]}…{val[-4:]}"

    def summary(self) -> dict[str, str]:
        out = {}
        for key in DEFAULTS:
            out[key] = self.masked(key) if key in SECRET_KEYS else self.get(key)
        return out


@dataclass
class RunContext:
    """一次运行的上下文：输出目录、决策日志、费用水位。"""

    run_id: str
    root: Path
    project_dir: Path
    settings: Settings
    live: bool = False
    dry_run: bool = False
    verbose: bool = False
    cost_used_cny: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def out_dir(self) -> Path:
        return self.project_dir / "output"

    @property
    def work_dir(self) -> Path:
        return self.project_dir / "work"

    @property
    def log_path(self) -> Path:
        return self.project_dir / "run.jsonl"

    def prepare(self) -> None:
        for sub in (self.out_dir, self.work_dir, self.work_dir / "frames", self.work_dir / "clips"):
            sub.mkdir(parents=True, exist_ok=True)

    @property
    def budget_cny(self) -> float:
        return self.settings.get_float("BUDGET_CNY", 20.0)

    @property
    def budget_ok(self) -> bool:
        return self.cost_used_cny < self.budget_cny


def existing_run_context(
    run_id: str,
    settings: Settings,
    live: bool = False,
    dry_run: bool = False,
    verbose: bool = False,
) -> RunContext:
    """复用**已有**的运行目录。

    从拆解文件拍摄时必须用它，而不是 `new_run_context()`：plan 与成片要落在同一个
    `runs/<run_id>/` 下，否则拆解页拿着的 run_id 在拍完之后就找不到自己的成片，
    成片墙上还会多出一条主题重复的记录。
    """
    return RunContext(
        run_id=run_id,
        root=ROOT,
        project_dir=ROOT / "runs" / run_id,
        settings=settings,
        live=live,
        dry_run=dry_run,
        verbose=verbose,
    )


def new_run_context(project_name: str, settings: Settings, live: bool, dry_run: bool, verbose: bool) -> RunContext:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = "".join(ch for ch in project_name if ch not in '\\/:*?"<>|').strip() or "untitled"
    run_id = f"{stamp}-{safe[:20]}"
    project_dir = ROOT / "runs" / run_id
    return RunContext(
        run_id=run_id,
        root=ROOT,
        project_dir=project_dir,
        settings=settings,
        live=live,
        dry_run=dry_run,
        verbose=verbose,
    )
