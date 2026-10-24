"""一言片场 · 引擎包。

分层：
- engine.py   引擎桥接（后台线程跑管线 + 历史记录扫描 + 自检）
- version.py  版本号唯一来源

界面不在这里：V4 起界面是 `web/`（React）装在 `desktop.py` 的 WebView2 窗口里。
旧 Tkinter 的 `gui.py` / `ui.py` / `theme.py` 已在 4.3 删除，
`__all__` 里也就不能再列 `gui`——否则 `from app import *` 会去导入一个不存在的模块。
"""

__all__ = ["engine", "version"]
