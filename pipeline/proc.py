"""子进程窗口隐藏——打包后出片时不再弹黑框。

**为什么会弹窗**
Windows 上，一个**没有控制台**的进程（PyInstaller `--windowed` 打包出来的 EXE 就是）
去启动一个**控制台程序**（ffmpeg / node / cmd）时，系统会给子进程
**新建一个控制台窗口**。父进程自己没有控制台可继承，子进程就默认开一个新的。

表现：出片时桌面不停闪黑框；代码绘制时**每渲染一帧弹一个**（15 秒 × 24fps = 360 个）。
用户第一眼就会否掉这个产品，所以这不是小瑕疵。

**怎么修**
两道一起上，缺一不可：
- `CREATE_NO_WINDOW`：告诉系统不要为这个子进程创建控制台
- `STARTUPINFO(SW_HIDE)`：部分启动路径（比如经由 cmd）会忽略上一条，这一层兜住

**用在哪**
所有**运行期**要起子进程的地方：ffmpeg、ffprobe、node 渲染、mklink。
不用管 `setup.py` / `build_web.py` / `tools/` 这些开发脚本——它们本来就是命令行跑的。
"""
from __future__ import annotations

import os
import subprocess
from typing import Any


def hidden_kwargs() -> dict[str, Any]:
    """放进 `subprocess.run/Popen/call` 的关键字参数，把子进程窗口藏起来。

    非 Windows 返回空 dict（POSIX 上没有这个问题）。
    """
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "startupinfo": startupinfo,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
    }


def run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """`subprocess.run` 的隐藏窗口版本。其余参数原样透传。"""
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs.setdefault("encoding", "utf-8")
    kwargs.setdefault("errors", "replace")
    return subprocess.run(cmd, **hidden_kwargs(), **kwargs)
