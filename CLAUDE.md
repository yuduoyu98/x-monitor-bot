# CLAUDE.md

## Project

**x-monitor-bot** — X (Twitter) 媒体监控机器人。用 Nitter RSS + fxTwitter API 获取推文，同步图片/视频到 Telegram 频道。无需 X API、无需浏览器。

## Architecture

**Fetcher 解耦**: `Post` dataclass 是所有组件唯一数据接口。切换抓取方案改 `config.yaml` 一行，下游代码不动。

```
RSS → filter by watermark → filter by DB已知 → fxTwitter解析 → 下载 → TG发送 → 记录DB
```

## Key Components

| 模块 | 职责 |
|------|------|
| `fetcher/nitter_fxtwitter.py` | RSS 发现 + fxTwitter 解析，零浏览器 |
| `downloader.py` | httpx HTTP 下载 CDN 媒体 |
| `database.py` | SQLite (aiosqlite): subscriptions / posts / sync_log |
| `telegram_bot.py` | ptb v21，media group + caption，RetryAfter 重试 |
| `scheduler.py` | 主循环：每 5 分钟遍历订阅，按 watermark 增量抓取 |
| `admin_gui.py` | Tkinter GUI：管理订阅、配置、测试 |

## Dev Standards

- **uv** 包管理，**Ruff** format/lint (行宽100，双引号)
- **全 async**，所有 I/O 用 `async/await`
- **类型注解** 所有函数，Pydantic 验证配置
- **pytest + pytest-asyncio**，关键路径有测试
- 不提交 `config.yaml`, `state.db`, `cache/`, `logs/`

## Commands

```bash
uv pip install -e ".[dev]"
python -m src.main          # 启动
python -m src.admin_gui     # GUI
ruff format . && ruff check .
pytest -v
```
