# AstroPaperDigest

[English](README.md)

基于 LLM 智能评分，自动抓取、过滤、排序每日天体物理论文，精准匹配你的研究方向。提供原生桌面窗口浏览、日期导航、个性化设置等功能。

## 功能特性

- **LLM 排序**：使用 DeepSeek（或任意 OpenAI 兼容 API）对论文相关性评分 1-10
- **桌面界面**：原生 macOS 窗口（WebKit），支持日期导航、分层浏览、实时过滤
- **设置向导**：首次启动引导配置 API 密钥和研究兴趣
- **双格式输出**：BibTeX 条目 + Markdown 摘要，按日期归档
- **macOS 应用**：双击即可运行，打开原生桌面窗口

## 快速开始

> **注意：** 请勿在 `~/Downloads/` 下运行——macOS 会阻止下载的文件。请先将项目移动到固定位置（如 `~/Projects/`）。

1. 双击 **`Install.command`** —— 自动配置 Python 环境并构建应用
2. 双击 **`AstroPaperDigest.app`** —— 原生桌面窗口自动打开

> **macOS 安全提示？** 前往 **系统设置 → 隐私与安全性**，点击 **"仍要打开"**。仅需操作一次。

## 更新机制

- **检查时机**：应用启动时自动在后台检查一次（网络失败静默），也可在 **设置页（⛭）→ General → Update** 手动点击「Check for Updates」。
- **推送提示**：发现新版本时，Digest 页顶部显示蓝色横幅；设置页的 Update 分组显示当前/最新版本与更新日志。
- **安装流程**（半自动）：在设置页点击「Download Update」→ 下载完成后自动做 SHA-256 校验 → 点击「Install & Restart」→ 自动备份旧代码、替换源码、重建 .app 并重新打开。
- **更新源**：GitHub Releases（公开仓库）。检查接口：`https://api.github.com/repos/jiangrz77/AstroPaperDigest/releases/latest`。
- **版本号**：单一版本源 `version.txt`（构建 .app 时由 `build_app.sh` 读取）。
- **保留文件**：更新不会覆盖 `.env`、`config.yaml`、`preferences.json`、`feedback.json`、`data/`、`output/`、`.venv`；旧代码自动备份到 `backups/`。

## 发布新版本（开发者）

1. 修改 `version.txt`（如 `1.0.3`），提交并推送：
   ```bash
   git add . && git commit -m "v1.0.3" && git push origin main
   git tag v1.0.3 && git push origin v1.0.3
   ```
2. 运行 `./release.sh` —— 自动生成 `AstroPaperDigest-v1.0.3.zip` 与 `version.json`（含 SHA-256）。
3. GitHub 网页：仓库 → **Releases → Draft a new release** → 选择标签 `v1.0.3` → 写更新日志 → 上传 zip 附件 → **Publish release**（不要勾选 Pre-release）。
4. 用户端启动 App 或点「检查更新」即可收到新版本提示并一键更新。

> 若仓库为私有：GitHub Releases 无法匿名访问。可将 `release.sh` 生成的 `version.json` + zip 上传到任意静态托管，并把 `config.yaml` 中 `update.github_repo` 改为对应地址（或直接使用自建静态 JSON 更新源）。

## 环境要求

- macOS（Apple Silicon 或 Intel）
- Python 3.9+（大多数 Mac 通过 Xcode Command Line Tools 已预装）
- DeepSeek API key（或任意 OpenAI 兼容服务商）

## 致谢

Thank you to arXiv for use of its open access interoperability.

## 许可证

MIT