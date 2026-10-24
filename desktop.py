#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一言片场 · 桌面客户端入口（可打包为独立 EXE）。

窗口用 WebView2（Windows 10/11 自带，和 Edge 同内核），所以 backdrop-filter、
mix-blend-mode、CSS 动画都能原样呈现——Tkinter 给不了这些。

**为什么后端用内嵌线程，而不是 subprocess**
原实现是 `subprocess.Popen([sys.executable, "-m", "server"])`。源码运行时没问题，
但 PyInstaller 打包后 `sys.executable` 指向 EXE 自己——它会不断重新启动自己，
直到把机器拖死。改成同进程内跑 uvicorn，打包前后行为一致。

**为什么要有启动日志**
打包后是 windowed 程序（没有控制台），`print` 和 traceback 全部无处可去。
一旦启动失败，用户看到的就是"双击没反应"，我们连查的地方都没有。
所以这里把每一步写进 `work/desktop.log`，并在失败时弹一个原生对话框告诉用户
日志在哪——这是"发给别人也能自己排查"的最低要求。
"""

from __future__ import annotations

import ctypes
import socket
import sys
import threading
import time
import traceback
import urllib.request
from pathlib import Path

HOST = "127.0.0.1"
BOOT_TIMEOUT = 30.0

#: 项目根（源码运行 = 本文件所在目录；打包后 = EXE 所在目录）
ROOT = Path(__file__).resolve().parent
#: 随包资源根（打包后是 PyInstaller 的解包目录 _internal）
BUNDLE = Path(getattr(sys, "_MEIPASS", str(ROOT)))
#: 启动日志（放用户数据区，出问题让用户直接发这个文件）
LOG_PATH = ROOT / "work" / "desktop.log"


def _write_log(message: str) -> None:
    """尽力写日志：先写 EXE 同级，失败退到临时目录。绝不因为日志而崩溃。"""
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {message}"
    for target in (LOG_PATH, Path(__import__("tempfile").gettempdir()) / "yiyan-desktop.log"):
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            return
        except Exception:  # noqa: BLE001
            continue


def log(message: str) -> None:
    _write_log(message)
    stream = sys.stderr or sys.stdout
    if stream is not None:
        try:
            stream.write(message + "\n")
            stream.flush()
        except Exception:  # noqa: BLE001
            pass


def alert(title: str, text: str) -> None:
    """原生对话框。windowed 模式下唯一能告诉用户"出了什么事"的通道。"""
    try:
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)
    except Exception:  # noqa: BLE001
        pass


def free_port(start: int = 8010, span: int = 60) -> int:
    for port in range(start, start + span):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((HOST, port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"{start}-{start + span} 没有空闲端口")


def wait_ready(url: str, timeout: float = BOOT_TIMEOUT) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as resp:
                if resp.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    return False


def start_backend(port: int) -> threading.Thread:
    """在后台线程里跑 uvicorn。返回线程（daemon，主窗口关闭即随进程退出）。"""
    import uvicorn

    from server.app import app

    config = uvicorn.Config(app, host=HOST, port=port,
                            log_level="warning", access_log=False, log_config=None)
    server = uvicorn.Server(config)

    def _run() -> None:
        try:
            server.run()
        except Exception as exc:  # noqa: BLE001
            log(f"后端线程异常：{exc!r}\n{traceback.format_exc()}")

    thread = threading.Thread(target=_run, name="yiyan-backend", daemon=True)
    thread.start()
    return thread


def run() -> int:
    log("=" * 50)
    log(f"启动：exe={sys.executable}")
    log(f"      ROOT={ROOT}")
    log(f"      BUNDLE={BUNDLE}")
    log(f"      frozen={getattr(sys, 'frozen', False)}")

    dist = BUNDLE / "web" / "dist" / "index.html"
    log(f"前端产物：{dist} → {'存在' if dist.exists() else '缺失'}")
    if not dist.exists():
        alert("一言片场", f"缺少前端文件：\n{dist}\n\n请重新解压完整的程序包。")
        return 1

    log("导入 pywebview…")
    try:
        import webview
        log(f"pywebview 就绪：{webview.__file__}")
    except Exception as exc:  # noqa: BLE001
        log(f"pywebview 导入失败：{exc!r}\n{traceback.format_exc()}")
        alert("一言片场", f"界面组件加载失败：\n{exc}\n\n日志：{LOG_PATH}")
        return 1

    log("启动后端…")
    port = free_port()
    start_backend(port)
    url = f"http://{HOST}:{port}/"
    log(f"后端端口 {port}，等待就绪…")

    if not wait_ready(url + "api/meta"):
        log("后端 30 秒未就绪")
        alert("一言片场", f"本地服务启动超时。\n\n日志：{LOG_PATH}")
        return 1
    log("后端就绪，创建窗口")

    webview.create_window("一言片场", url, width=1340, height=880,
                          min_size=(1080, 720), background_color="#EEF1FA",
                          text_select=True)
    webview.start()
    log("窗口已关闭，退出")
    return 0


def main() -> int:
    try:
        return run()
    except Exception as exc:  # noqa: BLE001
        log(f"未捕获异常：{exc!r}\n{traceback.format_exc()}")
        alert("一言片场启动失败", f"{exc}\n\n详细日志：\n{LOG_PATH}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
