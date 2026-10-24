"""`python -m server` 启动入口。

自动挑端口：8000 被别的服务占了就往后找，不直接失败——本地工具最常见的
"起不来"就是这个原因，报错信息还看不懂。
"""

from __future__ import annotations

import os
import socket
import threading
import webbrowser

import uvicorn


def free_port(start: int = 8000, span: int = 40) -> int:
    for port in range(start, start + span):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit(f"127.0.0.1 上 {start}-{start + span - 1} 全部被占用，请先释放端口")


def main() -> None:
    from .app import app

    port = int(os.environ.get("YIYAN_PORT") or 0) or free_port()
    url = f"http://127.0.0.1:{port}/"

    print("=" * 58)
    print("  一言片场 · Web 版")
    print(f"  地址：{url}")
    print("  关闭这个窗口即停止服务")
    print("=" * 58)

    if os.environ.get("YIYAN_NO_BROWSER") != "1":
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
