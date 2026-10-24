"""运行任务注册表。

把 `app.engine.Engine` 的五个回调（日志 / 进度 / 体检 / 完成 / 失败）收进一个
可重放的事件缓冲，HTTP 层再从缓冲里往外吐 SSE。

为什么要有缓冲而不是直接转发：
SSE 断线后 `EventSource` 会重连，但重连的新连接收不到断开期间的日志。
每个任务留最近 500 条，新连接先按 `cursor` 补发再跟实时流——否则用户刷新一次
页面，前面的阶段记录就全没了。
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Iterator

from app.engine import Engine, RunOptions

#: 事件缓冲上限。超过就丢最老的（日志是过程记录，丢老的不影响当前状态）
MAX_BUFFER = 500
#: 空闲多久发一次 ping（防止反代把长连接判定为空闲而掐断）
PING_SECONDS = 15


def jsonable(obj: Any, depth: int = 0) -> Any:
    """把管线返回值转成可 JSON 的结构。

    `check_env()` 返回的 dict 里混着 `Settings` 对象，直接丢给 json.dumps 会炸；
    这里统一降级成字符串，保证事件流不会因为一个不可序列化的字段整条断掉。
    """
    if depth > 8:
        return str(obj)
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, dict):
        return {str(k): jsonable(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [jsonable(v, depth + 1) for v in obj]
    return str(obj)


class Job:
    """一次运行。对外身份是 job_id，与管线内部的 run_id 解耦。

    两者不能合并：run_id 由 `new_run_context()` 在**工作线程里**生成，
    而 `POST /runs` 必须立刻返回一个标识给前端去订阅事件——那时 run_id 还不存在。
    """

    def __init__(self, theme: str, opts: RunOptions, owner_id: int = 0) -> None:
        self.job_id = uuid.uuid4().hex[:12]
        self.theme = theme
        self.opts = opts
        #: 发起人。0 = 游客。run_id 要等管线在工作线程里生成，所以归属
        #: 只能在完成回调里落库（见 _record_owner）
        self.owner_id = int(owner_id or 0)
        self.created = time.time()
        self.seq = 0
        self.buffer: list[tuple[int, str, dict]] = []
        self._cond = threading.Condition()
        self.finished = False
        self.failed = False
        self.error = ""
        self.result: dict | None = None
        self.engine: Engine | None = None
        self.cancel_requested = False

    # ---------------------------------------------------------------- 事件
    def _emit(self, kind: str, data: dict) -> None:
        payload = jsonable(data)
        with self._cond:
            self.buffer.append((self.seq, kind, payload))
            self.seq += 1
            if len(self.buffer) > MAX_BUFFER + 100:
                del self.buffer[: len(self.buffer) - MAX_BUFFER]
            self._cond.notify_all()

    def _on_log(self, level: str, msg: str) -> None:
        self._emit("log", {"level": level, "msg": msg, "ts": time.time()})

    def _on_progress(self, stage: str, pct: float) -> None:
        self._emit("progress", {"stage": stage, "pct": max(0.0, min(1.0, float(pct)))})

    def _on_env(self, data: dict) -> None:
        # 只挑界面需要的字段：settings 对象不进事件流（它含密钥，即便掩码也没必要外传）
        self._emit(
            "env",
            {
                "ready": bool(data.get("ready")),
                "checks": data.get("checks", []),
                "degraded": list(dict.fromkeys(data.get("degraded", []))),
                "live_ready": bool(data.get("live_ready")),
                "keys": data.get("keys", {}),
            },
        )

    def _on_done(self, result: dict) -> None:
        self.result = jsonable(result)
        self._record_owner(result)
        self._emit("done", {"result": self.result})
        with self._cond:
            self.finished = True
            self._cond.notify_all()

    def _record_owner(self, result: object) -> None:
        """把 run_id 与发起人写进 run_owners。

        为什么在这里做：`run_id` 由管线在工作线程里生成，`POST /runs` 那一刻
        还不存在，所以没法在创建时写。失败只记警告——归属丢了顶多成片进不了
        "我的作品"，不该影响任务本身成功。
        """
        if not self.owner_id:
            return
        try:
            from pathlib import Path

            from .auth import record_run_owner

            project_dir = (result or {}).get("project_dir") if isinstance(result, dict) else None
            if not project_dir:
                return
            record_run_owner(Path(str(project_dir)).name, self.owner_id)
        except Exception:  # noqa: BLE001
            pass

    def _on_error(self, msg: str) -> None:
        self.error = str(msg)
        self._emit("error", {"message": self.error})
        with self._cond:
            self.failed = True
            self.finished = True
            self._cond.notify_all()

    # ---------------------------------------------------------------- 查询
    @property
    def running(self) -> bool:
        return not self.finished

    @property
    def video(self) -> str:
        """成片绝对路径。没跑完或没产出时为空串。"""
        if not self.result:
            return ""
        return str((self.result.get("compose") or {}).get("video") or "")

    def snapshot(self) -> dict:
        return {
            "job_id": self.job_id,
            "theme": self.theme,
            "running": self.running,
            "finished": self.finished,
            "failed": self.failed,
            "error": self.error,
            "cursor": self.seq,
            "video": self.video,
            "result": self.result,
        }

    def stream(self, cursor: int = 0) -> Iterator[tuple[int, str, dict]]:
        """从 `cursor` 开始吐事件（seq, kind, data）；空闲时发 ping，吐完才收尾。

        seq 通过 SSE 的 `id:` 字段下发，前端断线重连时把它当 cursor 传回来，
        这样既不漏事件也不重复。
        """
        while True:
            with self._cond:
                if self.buffer and cursor < self.buffer[0][0]:
                    # 缓冲被裁掉的部分补不回来了，只能从现存最早的一条接上
                    cursor = self.buffer[0][0]
                pending = [item for item in self.buffer if item[0] >= cursor]
                if not pending and not self.finished:
                    self._cond.wait(timeout=PING_SECONDS)
                    if self.buffer and cursor < self.buffer[0][0]:
                        cursor = self.buffer[0][0]
                    pending = [item for item in self.buffer if item[0] >= cursor]
                done = self.finished and not pending

            if pending:
                for seq, kind, data in pending:
                    cursor = seq + 1
                    yield seq, kind, data
                continue
            if done:
                return
            yield -1, "ping", {"cursor": cursor}

    # ---------------------------------------------------------------- 控制
    def build_engine(self) -> Engine:
        engine = Engine(
            on_log=self._on_log,
            on_progress=self._on_progress,
            on_env=self._on_env,
            on_done=self._on_done,
            on_error=self._on_error,
        )
        self.engine = engine
        return engine

    def start(self) -> bool:
        self._emit("progress", {"stage": "准备", "pct": 0.0})
        engine = self.build_engine()
        return engine.start(self.opts)

    def cancel(self) -> None:
        """请求停止。管线在下一个镜头边界退出，已完成镜头照常进成片。"""
        self.cancel_requested = True
        if self.engine:
            self.engine.cancel()
        self._emit("log", {"level": "warn", "msg": "已发出停止请求，等待跑到镜头边界…",
                           "ts": time.time()})


class BusyError(RuntimeError):
    """已有任务在跑。Engine 自己也拦，但这里先拦能给出更清楚的 HTTP 状态码。"""


class JobRegistry:
    """内存里的任务表。单机工具，进程退出即清空——状态本来就在 runs/ 里。"""

    def __init__(self, keep: int = 20) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._keep = keep
        self._active: str | None = None

    def create(self, theme: str, opts: RunOptions, owner_id: int = 0) -> Job:
        with self._lock:
            if self._active:
                current = self._jobs.get(self._active)
                if current and current.running:
                    raise BusyError("已有任务在运行，请等待完成或先停止")
            job = Job(theme, opts, owner_id=owner_id)
            self._jobs[job.job_id] = job
            self._order.append(job.job_id)
            self._active = job.job_id
            while len(self._order) > self._keep:
                oldest = self._order[0]
                if oldest == self._active:
                    break
                self._order.pop(0)
                self._jobs.pop(oldest, None)
        if not job.start():
            with self._lock:
                self._active = None
            raise BusyError("引擎拒绝启动：已有任务在运行")
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def active(self) -> Job | None:
        with self._lock:
            return self._jobs.get(self._active) if self._active else None

    def recent(self) -> list[dict]:
        with self._lock:
            jobs = [self._jobs[jid] for jid in reversed(self._order) if jid in self._jobs]
        return [
            {
                "job_id": job.job_id,
                "theme": job.theme,
                "finished": job.finished,
                "failed": job.failed,
                "video": job.video,
                "created": job.created,
            }
            for job in jobs
        ]
