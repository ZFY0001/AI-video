"""任务注册表的唯一实例。

单独一个模块，只为打断循环引用：`server.app` 要注册 `/api/runs`，
`server.plan` 要注册 `/api/plan/{run_id}/render`，两者都得往**同一张**任务表里塞。
放在 app.py 里就只能互相 import——单实例放这里，谁都能拿，谁都不欠谁。
"""

from __future__ import annotations

from .jobs import JobRegistry

#: 进程内唯一。单机工具，退出即清空——真正的状态在 runs/ 里。
REGISTRY = JobRegistry()
