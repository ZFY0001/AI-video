# -*- coding: utf-8 -*-
"""创建 V5 桌面快捷方式，并删掉旧的 V4.5 那个（避免点错版本）。"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TARGET = r"C:\一言片场-6.0\02-客户端\YiYanStudio-V6\YiYanStudio-V6.exe"
DESK = os.path.join(os.path.expanduser("~"), "Desktop")
LNK = os.path.join(DESK, "一言片场 V6.lnk")
OLD = os.path.join(DESK, "一言片场 V5.lnk")

if not os.path.exists(TARGET):
    print("目标不存在:", TARGET)
    sys.exit(1)


def main() -> int:
    import win32com.client

    shell = win32com.client.Dispatch("WScript.Shell")
    if os.path.exists(OLD):
        os.remove(OLD)
        print("已删除旧快捷方式: 一言片场 V5.lnk")
    if os.path.exists(LNK):
        os.remove(LNK)

    sc = shell.CreateShortcut(LNK)
    sc.TargetPath = TARGET
    sc.WorkingDirectory = os.path.dirname(TARGET)
    sc.IconLocation = TARGET + ",0"
    sc.Description = "一言片场 V6"
    sc.Save()

    chk = shell.CreateShortcut(LNK)
    ok = os.path.normcase(chk.TargetPath) == os.path.normcase(TARGET)
    print("快捷方式:", LNK)
    print("  目标:", chk.TargetPath)
    print("  结论:", "正确" if ok else "不一致")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
