# -*- coding: utf-8 -*-
"""把桌面快捷方式改指向新的客户端入口（WebView2 + FastAPI）。

为什么不用「启动客户端.bat」当目标：那会带一个黑色控制台窗口。
直接指向 .venv 里的 pythonw.exe 并传 desktop.py，窗口干净、图标也能生效。
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import win32com.client  # noqa: E402

PROJ = r"C:\Users\ZFY\Desktop\AI video\一言片场-V3\一言片场-V3"
DESK = os.path.join(os.path.expanduser("~"), "Desktop")

pyw = os.path.join(PROJ, ".venv", "Scripts", "pythonw.exe")
entry = os.path.join(PROJ, "desktop.py")
icon = os.path.join(PROJ, "assets", "icon.ico")
lnk = os.path.join(DESK, "一言片场.lnk")

missing = [p for p in (pyw, entry, icon) if not os.path.exists(p)]
if missing:
    print("★ 缺少文件，未创建快捷方式：")
    for m in missing:
        print("   ", m)
    sys.exit(1)

shell = win32com.client.Dispatch("WScript.Shell")
if os.path.exists(lnk):
    os.remove(lnk)

sc = shell.CreateShortcut(lnk)
sc.TargetPath = pyw
sc.Arguments = f'"{entry}"'
sc.WorkingDirectory = PROJ
sc.IconLocation = icon + ",0"
sc.Description = "一言片场 · 一句话进，成片出"
sc.WindowStyle = 1
sc.Save()

chk = shell.CreateShortcut(lnk)
print("快捷方式:", "已创建" if os.path.exists(lnk) else "★失败★")
print("  目标  :", chk.TargetPath)
print("  参数  :", chk.Arguments)
print("  工作目录:", chk.WorkingDirectory)
print("  图标  :", chk.IconLocation)
ok = (os.path.normcase(chk.TargetPath) == os.path.normcase(pyw)
      and chk.IconLocation.startswith(icon))
print("\n结论:", "✓ 正确" if ok else "★回读不一致★")
