"""正在运行的子进程登记处：停止任务时能真正释放它们。

为什么需要：管线原来只在镜头边界检查取消信号，单次 FFmpeg 出片/合成或
node 绘制可以跑几分钟。用户点停止后界面说"已请求"，进程却还在满负荷转，
机器风扇照响——这不是"停止"，只是"不再往下排新活"。

这里登记长任务子进程，取消时按**进程树**结束（Windows 上 ffmpeg 可能自己
拉起子进程；node 会拉起无头浏览器，只杀直接子进程会留下吃 CPU 的孤儿）。
"""

from __future__ import annotations

import os
import subprocess
import threading
from typing import Any

#: 当前是否有取消请求。单个进程同时只跑一条任务（Registry 用 BusyError 拦），
#: 所以用一个全局标志足够，不需要按 job 分桶。
_CANCELLED = False
_PROCS: set[Any] = set()
_LOCK = threading.Lock()


class ProcessCancelled(RuntimeError):
    """子进程因用户取消而被终止。与"命令自身失败"区分开。"""


def begin_run() -> None:
    """新任务开始时清掉上一次的取消状态。

    必须在起任务之前调用：否则上一次取消留下的标志会让这一次的第一个子进程
    一起头就被杀掉，表现为"刚点拍摄就失败"。
    """
    global _CANCELLED
    with _LOCK:
        _CANCELLED = False
        _PROCS.clear()


def cancelled() -> bool:
    with _LOCK:
        return _CANCELLED


def register(proc: Any) -> bool:
    """登记一个 Popen。已取消时立刻结束它并返回 False。"""
    with _LOCK:
        if _CANCELLED:
            kill_now = True
        else:
            _PROCS.add(proc)
            kill_now = False
    if kill_now:
        kill_tree(getattr(proc, "pid", 0))
        return False
    return True


def unregister(proc: Any) -> None:
    with _LOCK:
        _PROCS.discard(proc)


def active_count() -> int:
    """当前登记在案的子进程数。供自检确认"取消前确实有进程在跑"。"""
    with _LOCK:
        return len(_PROCS)


def request_cancel() -> int:
    """标记取消并结束所有已登记进程，返回处理的进程数。"""
    global _CANCELLED
    with _LOCK:
        _CANCELLED = True
        procs = list(_PROCS)
    for proc in procs:
        try:
            if proc.poll() is not None:
                continue
        except Exception:  # noqa: BLE001
            pass
        kill_tree(getattr(proc, "pid", 0))
    return len(procs)


def kill_tree(pid: int) -> None:
    """连同子孙进程一起结束。

    Windows 用 `taskkill /T /F`（自带、按父子关系整棵树）；
    非 Windows 退化为 process group kill，最后再兜底 terminate。
    """
    if pid <= 0:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True,
                timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return
        except (OSError, subprocess.SubprocessError):
            return
    try:
        os.killpg(os.getpgid(pid), 9)
    except (OSError, AttributeError):
        try:
            os.kill(pid, 9)
        except OSError:
            pass