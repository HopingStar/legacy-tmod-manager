# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 HopingStar
# debug.py — 调试模式：保留后端控制台窗口，实时查看日志
# 用法：python debug.py   （普通模式用 python app.py，不显示控制台）
import threading
import app as launcher

if __name__ == '__main__':
    threading.Thread(target=launcher._monitor_tmodloader, daemon=True).start()
    launcher.run_desktop(show_console=True)
