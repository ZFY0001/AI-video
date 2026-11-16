"""任务状态持久化。

内存里的 `Job` 进程一退就没了。以前刷新页面还能靠 sessionStorage 接回来，
**重启服务之后就只剩一句"任务不存在"**——跑了二十分钟的片子，用户连"它曾经
跑到哪、有没有产出"都查不到。这里把任务的身份、归属、状态与成片路径写进
`work/yiyan.db`，重启后任务中心仍能如实展示。

**不落日志**：一条 MV 有几千行日志，全量落库会让库比成片还大。持久化的是
"这次运行是什么、结果如何"，过程记录仍只在内存缓冲里。

**重启语义**：进程退出时线程必然被带走，所以启动时所有 `running` 一律改判
`interrupted`。不做自动续跑——半个任务重新接上比从头跑一次更难保证一致。
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from .auth import db

#: 与前端 TasksPage 对齐的一组状态。canceled 是用户主动停且已有产出。
STATUSES = ("running", "done", "canceled", "failed", "interrupted")

#: 重启后对残留任务的说明。写在 error 里，界面直接展示，不需要再猜。
INTERRUPTED_NOTE = "服务重启，这次运行被中断；已完成的镜头与已有成片不受影响"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    job_id    TEXT PRIMARY KEY,
    owner_key TEXT NOT NULL,
    theme     TEXT NOT NULL DEFAULT '',
    kind      TEXT NOT NULL DEFAULT 'create',
    run_id    TEXT NOT NULL DEFAULT '',
    status    TEXT NOT NULL DEFAULT 'running',
    error     TEXT NOT NULL DEFAULT '',
    video     TEXT NOT NULL DEFAULT '',
    result    TEXT NOT NULL DEFAULT '',
    options   TEXT NOT NULL DEFAULT '{}',
    created   REAL NOT NULL,
    updated   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_owner ON tasks(owner_key, created);
"""


class TaskStoreUnavailable(RuntimeError):
    """任务表不可用（库损坏、磁盘只读等）。

    调用方应返回 503 而不是"没有任务"——把故障读成空列表，用户会以为自己的
    运行记录被清空了。
    """


def init_tasks() -> None:
    """建表并把上次进程留下的 running 标为中断。幂等，启动时调用一次。"""
    try:
        with db() as conn:
            conn.executescript(_SCHEMA)
            conn.execute(
                "UPDATE tasks SET status = 'interrupted', error = ?, updated = ? "
                "WHERE status = 'running'",
                (INTERRUPTED_NOTE, time.time()),
            )
    except sqlite3.Error as exc:
        raise TaskStoreUnavailable(f"任务表初始化失败：{exc}") from exc


def create_task(
    job_id: str,
    owner_key: str,
    theme: str,
    kind: str = "create",
    run_id: str = "",
    options: dict | None = None,
) -> None:
    """任务创建即落库。

    拍摄入口在起任务前就知道 run_id（项目 id），一次跑完的任务要等管线分配，
    所以 run_id 允许为空，随后由 `update_task` 回填。
    """
    now = time.time()
    try:
        with db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO tasks"
                "(job_id, owner_key, theme, kind, run_id, status, error, video,"
                " result, options, created, updated) "
                "VALUES (?,?,?,?,?,'running','','','',?,?,?)",
                (
                    str(job_id),
                    str(owner_key),
                    str(theme or ""),
                    str(kind or "create"),
                    str(run_id or ""),
                    json.dumps(options or {}, ensure_ascii=False),
                    now,
                    now,
                ),
            )
    except sqlite3.Error as exc:
        raise TaskStoreUnavailable(f"任务状态写入失败：{exc}") from exc


_UPDATABLE = {"run_id", "status", "error", "video", "result", "options"}


def update_task(job_id: str, **fields: Any) -> None:
    """按字段更新。只接受白名单，未知字段直接报错——避免调用方以为写进去了。"""
    unknown = set(fields) - _UPDATABLE
    if unknown:
        raise ValueError(f"不能写入的任务字段：{sorted(unknown)}")

    sets: list[str] = []
    values: list[Any] = []
    for key, value in fields.items():
        if key == "result":
            value = json.dumps(value, ensure_ascii=False) if value is not None else ""
        elif key == "options":
            value = json.dumps(value or {}, ensure_ascii=False)
        elif key == "status":
            if value not in STATUSES:
                raise ValueError(f"未知任务状态：{value}")
            value = str(value)
        else:
            value = str(value if value is not None else "")
        sets.append(f"{key} = ?")
        values.append(value)

    sets.append("updated = ?")
    values.extend([time.time(), str(job_id)])
    try:
        with db() as conn:
            conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE job_id = ?", values)
    except sqlite3.Error as exc:
        raise TaskStoreUnavailable(f"任务状态更新失败：{exc}") from exc


def _row_to_task(row: sqlite3.Row, with_result: bool) -> dict[str, Any]:
    task = {
        "job_id": row["job_id"],
        "owner_key": row["owner_key"],
        "theme": row["theme"],
        "kind": row["kind"],
        "run_id": row["run_id"],
        "status": row["status"],
        "error": row["error"],
        "created": row["created"],
        "updated": row["updated"],
        "options": _loads(row["options"], {}),
    }
    if with_result:
        task["video"] = row["video"]
        task["result"] = _loads(row["result"], None)
    else:
        # 列表只需要"有没有成片"，不把本地路径带出去。
        task["has_video"] = bool(row["video"])
    return task


def _loads(raw: str, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback


def get_task(job_id: str) -> dict[str, Any] | None:
    """完整任务记录（含结果）。不存在返回 None。"""
    try:
        with db() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE job_id = ?", (str(job_id),)
            ).fetchone()
    except sqlite3.Error as exc:
        raise TaskStoreUnavailable(f"任务查询失败：{exc}") from exc
    return _row_to_task(row, with_result=True) if row else None


def list_tasks(owner_key: str, limit: int = 60) -> list[dict[str, Any]]:
    """最近任务摘要。**不带结果体**：列表页只需要状态和成片有没有。"""
    if not owner_key:
        return []
    try:
        with db() as conn:
            rows = conn.execute(
                "SELECT job_id, owner_key, theme, kind, run_id, status, error, video,"
                " options, created, updated FROM tasks"
                " WHERE owner_key = ? ORDER BY created DESC LIMIT ?",
                (str(owner_key), max(1, min(int(limit), 200))),
            ).fetchall()
    except sqlite3.Error as exc:
        raise TaskStoreUnavailable(f"任务列表读取失败：{exc}") from exc
    return [_row_to_task(row, with_result=False) for row in rows]