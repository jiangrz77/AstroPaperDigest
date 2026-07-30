# ArXivDailyDigest

[English](README.md)

基于 LLM 智能评分，自动抓取、过滤、排序每日 arXiv 论文，精准匹配你的研究方向。提供 Web 界面浏览、日期导航、个性化设置等功能。

## 功能特性

- **Web 界面**：Flask 驱动的本地 Web UI，支持论文浏览、日期导航、实时过滤
- **首次设置向导**：首次启动自动引导配置 LLM API、研究兴趣、邮件通知
- **智能过滤**：arXiv 分类 + 关键词双重筛选，支持排除交叉列表和替换论文
- **LLM 排序**：使用 DeepSeek（或任意 OpenAI 兼容 API）对论文相关性评分 1-10
- **日期导航**：按日期浏览历史摘要，自动识别 arXiv 更新日（工作日）和非更新日（周末/节假日）
- **双格式输出**：BibTeX 条目 + Markdown 摘要，按日期归档
- **反馈系统**：对论文标记"过高评分"或"过低评分"，用于校准未来推荐
- **邮件通知**：可选每日摘要邮件推送（自发自收模式）
- **macOS 应用**：双击即可运行，自动打开浏览器

## 快速开始

### 方式一：macOS 应用（推荐）

双击 `ArxivRecommend.app`，浏览器自动打开：
- 首次使用：显示设置向导，引导配置 API 密钥和研究兴趣
- 后续使用：直接显示最新论文摘要

### 方式二：命令行

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置环境
cp .env.example .env
# 编辑 .env，填入你的 API key 和邮件配置

# 3. 配置研究兴趣
# 编辑 config.yaml：分类、关键词等

# 4. 运行（命令行模式）
python main.py

# 5. 或启动 Web 界面
python src/gui.py
```

## Web 界面功能

启动 Web 界面后（`python src/gui.py` 或双击 .app），浏览器打开 `http://127.0.0.1:5123`：

- **日期导航**：顶部日期选择器 + 左右箭头切换日期
- **分类浏览**：按"高度相关"/"可能相关"/"边缘相关"分层展示
- **实时过滤**：勾选/取消"Cross-listed"和"Replacements"即时过滤论文
- **反馈按钮**：每篇论文可标记"Overrated"或"Underrated"
- **设置页面**：点击工具栏齿轮图标修改配置
- **Re-run**：点击 ↻ 按钮为当前日期重新抓取和排序论文

## 配置说明

### 设置向导（Web 界面）

首次启动时自动显示，也可通过工具栏齿轮图标随时访问：

1. **LLM API 配置**：选择服务商（DeepSeek/OpenAI/自定义）、填入 API Key 和模型名
2. **研究兴趣**：快速模式（选择分类 + 关键词）或上传 .bib 文件
3. **邮件通知**：配置 SMTP 服务器、端口、发件人（可选）

### 配置文件 (config.yaml)

| 配置项 | 说明 |
|--------|------|
| `arxiv_categories` | 监控的 arXiv 分类（如 `astro-ph.GA`） |
| `keywords` | 初筛关键词列表 |
| `bib_file` | 你的 BibTeX 论文库路径（用于构建兴趣画像） |
| `llm` | LLM 服务商设置（base_url, model, api_key_env） |
| `output` | BibTeX 和摘要的输出目录 |
| `email` | SMTP 邮件通知设置 |
| `filter` | 评分阈值、最大候选数、抓取天数 |

### 环境变量 (.env)

```bash
DEEPSEEK_API_KEY="sk-..."        # LLM API 密钥（必填）
EMAIL_APP_PASSWORD="..."          # 邮件应用密码（可选）
EMAIL_SENDER="you@example.com"    # 发件人邮箱
EMAIL_RECIPIENT="you@example.com" # 收件人邮箱（与发件人相同）
SMTP_SERVER="smtp.gmail.com"      # SMTP 服务器
SMTP_PORT="587"                   # SMTP 端口
```

## 命令行参数

```bash
python main.py [选项]

--config PATH        配置文件路径（默认：config.yaml）
--days N             抓取最近 N 天的论文（默认：3）
--threshold N        BibTeX 输出的最低评分（默认：7）
--target-date DATE   指定目标日期抓取（格式：YYYY-MM-DD）
--no-cross           排除交叉列表论文
--no-replacements    排除替换（更新版）论文
--no-email           跳过邮件通知
--dry-run            跳过 LLM 排序（用于测试）
--update-profile     打印兴趣画像摘要后退出
```

## 输出结构

```
output/
  bibtex/
    recommendations_2026-07-29.bib
    recommendations_2026-07-30.bib
  digests/
    digest_2026-07-29.md
    digest_2026-07-30.md
```

## arXiv 更新时间表

arXiv 在工作日发布新论文（美东时间 20:00 = 北京时间次日 08:00）：

| 提交截止（美东） | 发布时间（美东） | 北京时间 |
|------------------|------------------|----------|
| 周一 14:00 - 周二 14:00 | 周二 20:00 | 周三 08:00 |
| 周二 14:00 - 周三 14:00 | 周三 20:00 | 周四 08:00 |
| 周三 14:00 - 周四 14:00 | 周四 20:00 | 周五 08:00 |
| 周四 14:00 - 周五 14:00 | 周日 20:00 | 周一 08:00 |
| 周五 14:00 - 周一 14:00 | 周一 20:00 | 周二 08:00 |

周末和节假日不发布。系统会自动识别非更新日并显示相应提示。

## 邮件配置

> **注意：** 邮件通知功能仍在开发中，无需填写，可跳过本节。

邮件采用"自发自收"模式：发件人和收件人为同一邮箱，你会收到自己发给自己的每日摘要。

**常见 SMTP 设置：**
| 邮箱 | 服务器 | 端口 | SSL |
|------|--------|------|-----|
| Gmail | smtp.gmail.com | 587 | false |
| 中科院邮箱 | cstnet.mail.cn | 587 | false |
| QQ 邮箱 | smtp.qq.com | 465 | true |
| 163 邮箱 | smtp.163.com | 465 | true |

## 项目结构

```
├── main.py              # 命令行入口
├── config.yaml          # 配置文件
├── .env                 # 环境变量（API key 等）
├── .env.example         # 环境变量模板
├── requirements.txt     # Python 依赖
├── src/
│   ├── gui.py           # Web 界面（Flask）
│   ├── fetch_arxiv.py   # arXiv 论文抓取
│   ├── filter.py        # 关键词过滤
│   ├── ranker.py        # LLM 排序
│   ├── profile.py       # 兴趣画像构建
│   ├── output.py        # 输出生成
│   ├── notifier.py      # 邮件通知
│   └── digest_parser.py # 摘要解析
├── data/                # 数据文件（.bib 论文库）
├── output/              # 输出目录
│   ├── bibtex/          # BibTeX 文件
│   └── digests/         # Markdown 摘要
├── assets/              # 应用图标
└── ArxivRecommend.app/  # macOS 应用包
```

## 环境要求

- Python 3.9+
- DeepSeek API key（或任意 OpenAI 兼容服务商）
- 可选：邮件应用专用密码

## 许可证

MIT
