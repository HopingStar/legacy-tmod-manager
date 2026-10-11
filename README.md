# ⚡ tModManager（Python 版） — 已归档

> tModManager 的 **Python 版**（Flask + pywebview）。**已停止维护**，本仓库仅作存档保留。

![状态](https://img.shields.io/badge/状态-已归档-lightgrey)
![运行环境](https://img.shields.io/badge/运行环境-Python%203-blue)
![许可证](https://img.shields.io/badge/License-MIT-green)

---

## 🚫 已停止维护

**不再提供任何功能更新，不再修复任何 bug，不保证与最新 tModLoader 版本兼容。**

项目已全部迁移到 **C# 版**（.NET 8 + WPF + WebView2），后续开发与维护**仅**针对 C# 版：

### 👉 https://github.com/HopingStar/tMod-Manager

| | Python 版（本仓库，已归档） | C# 版（推荐） |
|---|---|---|
| 技术栈 | Flask + pywebview | .NET 8 + WPF + WebView2 |
| 状态 | **已停止支持** | 持续维护 |
| 启动更快 / 占用更低 | ❌ | ✅ |
| 需要 Python 运行时 | ✅ | ❌（改需 .NET 8 运行时） |

> ⚠️ 你可以继续运行本版本，但将不再获得任何更新与维护支持，功能可能已过时或异常。

---

## 📦 这是什么

一个管理 **tModLoader 多版本启动**与**模组配置**的桌面启动器：管理各版本的启动脚本（`*.bat`）、全局整合包、模组列表（本地 `.tmod` 逆向 + Steam Workshop 元数据）、特殊加载，界面为深色 HTML / CSS / JS。

- 启动器主入口：`app.py`
- 前端：`templates/` + `static/`
- 工具模块：`modules/`（`tmod_parser.py` / `icon_util.py` / `net_fetch.py`）
- 数据：`data.json`、`mod_meta.json` —— 首次运行时在本目录自动生成，**不入库**

## ▶️ 运行

需要 Python 3：

```bash
pip install -r requirements.txt
python app.py            # 原生窗口模式（需要 pywebview）
python app.py --web      # 仅浏览器模式，不弹原生窗口
python debug.py          # 调试模式：保留后端控制台，实时查看日志
```

## ⚠️ 注意事项

- **数据独立**：本版本与 C# 版的数据文件相互独立，互不复制、互不影响。
- **⛔ 请勿与 C# 版同时运行**：两者共享同一个真实 Mods 目录
  （默认 `D:\Documents\My Games\Terraria\tModLoader\Mods`），同时运行会互踩
  `enabled.json` 与 `.tmod.disabled`，导致特殊加载状态错乱。
- 启动后顶部红字横幅与弹窗会提示本版本已停更，可在「全局设置」中关闭横幅。

## 📄 License

本仓库以 **MIT License** 开源，详见 [`LICENSE`](LICENSE)。

> 本版本虽已停止支持，但代码仍以 MIT 开源，任何人都可基于它继续开发或修改。
