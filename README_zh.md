# AstroPaperDigest

[English](README.md)

基于 LLM 智能评分，自动抓取、过滤、排序每日天体物理论文，精准匹配你的研究方向。提供本地 Web 界面浏览、日期导航、个性化设置等功能。

## 功能特性

- **LLM 排序**：使用 DeepSeek（或任意 OpenAI 兼容 API）对论文相关性评分 1-10
- **Web 界面**：本地 Web UI，支持日期导航、分层浏览、实时过滤
- **设置向导**：首次启动引导配置 API 密钥和研究兴趣
- **双格式输出**：BibTeX 条目 + Markdown 摘要，按日期归档
- **macOS 应用**：双击即可运行，自动打开浏览器

## 快速开始

> **注意：** 请勿在 `~/Downloads/` 下运行——macOS 会阻止下载的文件。请先将项目移动到固定位置（如 `~/Projects/`）。

1. 双击 **`Install.command`** —— 自动配置 Python 环境并构建应用
2. 双击 **`AstroPaperDigest.app`** —— 浏览器自动打开

> **macOS 安全提示？** 前往 **系统设置 → 隐私与安全性**，点击 **"仍要打开"**。仅需操作一次。

## 环境要求

- macOS（Apple Silicon 或 Intel）
- Python 3.9+（大多数 Mac 通过 Xcode Command Line Tools 已预装）
- DeepSeek API key（或任意 OpenAI 兼容服务商）

## 致谢

Thank you to arXiv for use of its open access interoperability.

## 许可证

MIT
