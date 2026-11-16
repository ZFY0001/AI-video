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

from app.engine import Engine, RunOptions, render_profile_of

from . import taskstore

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

    def __init__(self, theme: str, opts: RunOptions, principal: Any = None,
                 kind: str = "create") -> None:
        self.job_id = uuid.uuid4().hex[:12]
        self.theme = theme
        self.opts = opts
        #: 任务种类：create / plan / shot / recompose。任务中心据此说明"这次在干什么"。
        self.kind = str(kind or "create")
        #: 发起人（账号或访客）。任务接口一律按它鉴权；没有主体就不该有任务。
        #: 归属落库分两处：拍摄入口在起任务前就有 run_id，先写；一次跑完的
        #: 任务要等管线在工作线程里生成 run_id，只能在完成回调里补写。
        self.principal = principal
        self.owner_key = principal.key if principal is not None else ""
        self.run_id = str(getattr(opts, "run_id", "") or "")
        self.created = time.time()
        self.updated = self.created
        self.seq = 0
        self.buffer: list[tuple[int, str, dict]] = []
        self._cond = threading.Condition()
        self.finished = False
        self.failed = False
        self.error = ""
        self.result: dict | None = None
        self.engine: Engine | None = None
        self.cancel_requested = False
        #: 状态落库失败时只提示一次，不刷屏——重试也解决不了磁盘只读。
        self._persist_error = ""
        self._persist_warned = False
        self._stored = False
        self._persist_create()

    def _persist_create(self) -> None:
        if self.principal is None:
            return
        try:
            taskstore.create_task(
                self.job_id, self.owner_key, self.theme, self.kind,
                run_id=self.run_id, options=self.options(),
            )
            self._stored = True
        except taskstore.TaskStoreUnavailable as exc:
            self._persist_error = str(exc)

    def _persist(self, **fields: Any) -> None:
        """状态落库尽力而为：写失败只警告，不打断正在跑的渲染。"""
        if not self._stored:
            return
        try:
            taskstore.update_task(self.job_id, **fields)
        except (taskstore.TaskStoreUnavailable, ValueError) as exc:
            if self._persist_warned:
                return
            self._persist_warned = True
            self._emit("log", {
                "level": "warn",
                "msg": f"任务状态无法写盘，重启后这次运行可能不在任务中心里：{exc}",
                "ts": time.time(),
            })

    @property
    def status(self) -> str:
        if not self.finished:
            return "running"
        if self.failed:
            return "failed"
        if isinstance(self.result, dict) and self.result.get("stopped"):
            return "canceled"
        return "done"

    def options(self) -> dict:
        return {
            **render_profile_of(self.opts),
            "only_shot": int(self.opts.only_shot or 0),
            "recompose": bool(self.opts.recompose),
            "palette": str(self.opts.palette or "auto"),
            "snapshot_id": str(getattr(self.opts, "snapshot_id", "") or ""),
        }

    # ---------------------------------------------------------------- 事件
    def _emit(self, kind: str, data: dict) -> None:
        payload = jsonable(data)
        with self._cond:
            self.updated = time.time()
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

    def _publish_master(self) -> None:
        """把本次成功产物发布为项目当前母版。

        只在正式运行（非预览、非单镜预览）且确有视频时发布。发布失败不把
        已经产出的成片判失败，但要在事件流里显式说明——否则历史/导出会继续
        读到上一版，用户会以为新片没生效。
        """
        if not isinstance(self.result, dict) or self.opts.preview:
            return
        project_dir = str(self.result.get("project_dir") or "")
        compose = self.result.get("compose") if isinstance(self.result.get("compose"), dict) else {}
        video = str(compose.get("video") or "")
        if not project_dir or not video:
            return
        try:
            from pathlib import Path

            from .artifacts import publish_master

            manifest = publish_master(
                Path(project_dir), video,
                backend=str(compose.get("backend") or "kenburns"),
                job_id=self.job_id,
                snapshot_id=str(getattr(self.opts, "snapshot_id", "") or ""),
            )
            self.result["master"] = manifest
            self.result["snapshot_id"] = manifest.get("snapshot_id") or ""
        except Exception as exc:  # noqa: BLE001
            self._emit("log", {
                "level": "error",
                "msg": f"当前母版发布失败，历史与导出可能仍读到上一版：{exc}",
                "ts": time.time(),
            })
            self.result.setdefault("notes", []).append(
                f"当前母版发布失败：{exc}；成片文件已生成，但历史/导出可能仍指向上一版"
            )

    def _on_done(self, result: dict) -> None:
        self.result = jsonable(result)
        self.result["snapshot_id"] = str(getattr(self.opts, "snapshot_id", "") or "")
        self._record_owner(result)
        self._publish_master()
        # 落库放在发事件之前：前端收到 done 就会开播，此时记录必须已经能查到，
        # 否则任务中心刷新一下会短暂显示"还在跑"。
        stopped = isinstance(self.result, dict) and bool(self.result.get("stopped"))
        self._persist(
            run_id=self.run_id or str((self.result or {}).get("run_id") or ""),
            status="canceled" if stopped else "done",
            result=self.result,
            video=self.video,
        )
        self._emit("done", {"result": self.result})
        with self._cond:
            self.finished = True
            self._cond.notify_all()

    def _on_start(self, run_id: str) -> None:
        """生成开始前的归属前置。

        归属失败必须中止任务，而不是只留警告继续出片：一次数据库抖动之后，
        后来成功补写归属的路径并不存在，项目会永久变成“公共旧项目”。
        这里抛出异常，由 Engine 在调用生成器之前终止。
        """
        self.run_id = str(run_id or "")
        self._persist(run_id=self.run_id)
        if self.principal is None or not self.run_id:
            return
        try:
            from .auth import record_owner

            record_owner(self.run_id, self.principal)
        except Exception as exc:  # noqa: BLE001
            self._emit("log", {
                "level": "error",
                "msg": f"归属登记失败，已中止生成：{exc}",
                "ts": time.time(),
            })
            raise

    def _record_owner(self, result: object) -> None:
        """把 run_id 与发起人写进 run_owners。

        为什么在这里做：`run_id` 由管线在工作线程里生成，`POST /runs` 那一刻
        还不存在，所以没法在创建时写。失败**不再静默**——在事件流里留一条警告，
        否则归属丢了只会表现为"成片突然变成公共的"。
        """
        if self.principal is None:
            return
        try:
            from pathlib import Path

            from .auth import record_owner

            project_dir = (result or {}).get("project_dir") if isinstance(result, dict) else None
            if not project_dir:
                return
            record_owner(Path(str(project_dir)).name, self.principal)
        except Exception as exc:  # noqa: BLE001
            self._emit("log", {
                "level": "warn",
                "msg": f"归属记录失败，这条成片可能不会出现在「我的作品」里：{exc}",
                "ts": time.time(),
            })

    def _on_error(self, msg: str) -> None:
        self.error = str(msg)
        self._persist(status="failed", error=self.error)
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

    def visible_to(self, principal: Any) -> bool:
        """任务可见性。空 owner_key 只可能来自旧的内存任务，一律拒绝。"""
        if not self.owner_key or principal is None:
            return False
        return self.owner_key == principal.key

    def snapshot(self) -> dict:
        return {
            "job_id": self.job_id,
            "run_id": self.run_id,
            "theme": self.theme,
            "kind": self.kind,
            "status": self.status,
            "running": self.running,
            "finished": self.finished,
            "failed": self.failed,
            "error": self.error,
            "created": self.created,
            "updated": self.updated,
            "has_video": bool(self.video),
            "cursor": self.seq,
            "video": self.video,
            "result": self.result,
            # 实际生效的渲染配置。界面据此确认"我选的是离线 + 代码绘制"，
            # 而不是等出片后从画风上猜。
            "options": self.options(),
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
            on_start=self._on_start,
        )
        self.engine = engine
        return engine

    def start(self) -> bool:
        if self._persist_error:
            self._emit("log", {
                "level": "warn",
                "msg": f"任务状态无法落库，重启后这次运行不会出现在任务中心：{self._persist_error}",
                "ts": time.time(),
            })
        self._emit("progress", {"stage": "准备", "pct": 0.0})
        engine = self.build_engine()
        return engine.start(self.opts)

    def cancel(self) -> None:
        """请求停止。

        会立即结束正在运行的子进程（FFmpeg / node 绘制），并让管线在下一个
        镜头边界退出；已完成的镜头照常保留，不会被删除。
        """
        self.cancel_requested = True
        if self.engine:
            self.engine.cancel()
        self._emit("log", {
            "level": "warn",
            "msg": "已请求停止：正在运行的子进程会被释放，已完成的镜头仍保留",
            "ts": time.time(),
        })


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
        #: 接纳中的任务预留。归属登记、快照冻结等前置写入在这段时间内完成；
        #: 没有它，两条请求都能通过"当前空闲"检查，后完成的前置写入会在
        #: Registry 拒绝第二条任务时已经污染第一条的快照。
        self._reserved: str | None = None

    def reserve(self) -> str:
        """预留唯一执行槽位。失败抛 BusyError，不做任何文件写入。"""
        with self._lock:
            if self._reserved:
                raise BusyError("已有任务正在接纳，请稍后重试")
            if self._active:
                current = self._jobs.get(self._active)
                if current and current.running:
                    raise BusyError("已有任务在运行，请等待完成或先停止")
            token = uuid.uuid4().hex
            self._reserved = token
            return token

    def release(self, token: str | None) -> None:
        with self._lock:
            if token and self._reserved == token:
                self._reserved = None

    def create(self, theme: str, opts: RunOptions, principal: Any = None,
               kind: str = "create", reservation: str | None = None) -> Job:
        if principal is None:
            raise BusyError("缺少访客凭据，无法创建任务")
        with self._lock:
            if reservation is not None:
                if self._reserved != reservation:
                    raise BusyError("任务预留已失效，请重新发起")
            elif self._reserved:
                raise BusyError("已有任务正在接纳，请稍后重试")
            if self._active:
                current = self._jobs.get(self._active)
                if current and current.running:
                    raise BusyError("已有任务在运行，请等待完成或先停止")
            self._reserved = None
            job = Job(theme, opts, principal=principal, kind=kind)
            self._jobs[job.job_id] = job
            self._order.append(job.job_id)
            self._active = job.job_id
            while len(self._order) > self._keep:
                oldest = self._order[0]
                if oldest == self._active:
                    break
                self._order.pop(0)
                self._jobs.pop(oldest, None)
        try:
            started = job.start()
        except BaseException as exc:
            # 线程起不来等异常：_active 已经指向这条永远不会结束的任务，
            # 不清掉就会把后续所有请求挡成"已有任务在运行"。
            with self._lock:
                if self._active == job.job_id:
                    self._active = None
            job._on_error(f"任务启动失败：{exc}")
            raise
        if not started:
            with self._lock:
                if self._active == job.job_id:
                    self._active = None
            raise BusyError("引擎拒绝启动：已有任务在运行")
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def active(self) -> Job | None:
        with self._lock:
            return self._jobs.get(self._active) if self._active else None

    def active_snapshot_ids(self) -> set[str]:
        """仍在运行的任务引用的快照 ID。清理历史快照时必须保留。"""
        with self._lock:
            active = self._jobs.get(self._active) if self._active else None
        if active is None or not active.running:
            return set()
        value = str(getattr(active.opts, "snapshot_id", "") or "")
        return {value} if value else set()

    def recent(self, principal: Any = None) -> list[dict]:
        """最近任务。**按主体过滤**：列表会先暴露 job_id，随机 ID 不能当权限用。"""
        with self._lock:
            jobs = [self._jobs[jid] for jid in reversed(self._order) if jid in self._jobs]
        return [
            {
                "job_id": job.job_id,
                "run_id": job.run_id,
                "theme": job.theme,
                "kind": job.kind,
                "status": job.status,
                "error": job.error,
                "finished": job.finished,
                "failed": job.failed,
                "has_video": bool(job.video),
                "video": job.video,
                "created": job.created,
                "updated": job.updated,
                "options": job.options(),
            }
            for job in jobs
            if job.visible_to(principal)
        ]
