# x-monitor-bot

X (Twitter) 媒体监控机器人 — 订阅 X 博主，自动同步新发的图片/视频到 Telegram 频道。

**无需 X API、无需浏览器登录。**

## 快速开始

```bash
# 1. 安装
uv venv
uv pip install -e ".[dev]"

# 2. 配置 (TG token + 频道ID)
cp config.example.yaml config.yaml
# 编辑 config.yaml：填 telegram.bot_token 和 telegram.chat_id

# 3. 添加订阅 (GUI)
python -m src.admin_gui
# Subscriptions 标签 → Add → 填 X 用户名

# 4. 启动
python -m src.main
```

## 前置条件

- Python 3.11+
- Telegram Bot Token：[@BotFather](https://t.me/BotFather) 创建
- TG 频道 ID：频道发条消息 → 转发给 [@getidsbot](https://t.me/getidsbot)
- Bot 需设为频道**管理员**

## 数据库

`state.db` 三张表：

| 表 | 用途 |
|----|------|
| `subscriptions` | 订阅配置（GUI 管理） |
| `posts` | 已同步的推文（去重 + 水位线） |
| `sync_log` | 同步审计 |

## 工作原理

```
Nitter RSS（最新 ~20 条推文）
  → 过滤纯文字、转推、引用
  → 按水位线跳过已处理的
  → fxTwitter API 获取媒体 CDN 链接
  → 下载到本地缓存目录
  → 按时间正序发到 Telegram（media group）
  → 更新水位线 + 去重记录
```

## 配置项

| 项 | 说明 | 默认值 |
|----|------|--------|
| `telegram.bot_token` | TG 机器人 Token | - |
| `telegram.chat_id` | 目标频道 ID | - |
| `fetcher.nitter_instance` | Nitter 实例 URL | `https://nitter.net` |
| `storage.cache_dir` | 缓存目录 | `./cache` |
| `storage.cache_ttl_days` | 缓存保留天数（-1 永不删除） | 7 |
| `scheduler.loop_interval_seconds` | 主循环间隔 | 300 |

## 测试

```bash
pytest -v                 # 30 条测试
python -m src.diagnose <x用户名>   # 诊断单个账号
python -m src.test_telegram <目录> # 测 TG 发送
```

## 项目结构

```
├── src/
│   ├── main.py           # 启动入口
│   ├── admin_gui.py      # 订阅管理 GUI
│   ├── config.py         # YAML 配置
│   ├── scheduler.py      # 主循环
│   ├── downloader.py     # 媒体下载
│   ├── database.py       # SQLite
│   ├── telegram_bot.py   # TG 发送
│   └── fetcher/
│       ├── base.py       # Post 数据契约
│       └── nitter_fxtwitter.py  # RSS + fxTwitter
└── tests/
```
