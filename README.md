# x-monitor-bot

X (Twitter) → Telegram 增量同步。订阅 X 博主,定时轮询,把新推文(文本/图片/视频)同步到 TG 频道。跑在个人 PC 上,轻量(省内存/CPU)。

**数据源**:Scweet(直连 X GraphQL,主)/ Nitter(降级)。无需 X 官方 API、无需浏览器。

> 架构重设计**已完成**(SP1~SP4):Source(Scweet/Nitter)→ SyncEngine → TelegramSink,GUI 管订阅/配置。详见 [CLAUDE.md](./CLAUDE.md)。

## 特性

- 文本/图片/视频增量同步,**不丢不重**(watermark + outbox + 重试上限 + dead-letter)
- 模块化可切换:**Source**(Scweet/Nitter)、**Sink**(Telegram)
- 轻量:水位线游标,不存全量历史;账号串行处理
- Tkinter GUI 管理订阅/配置

## 前置条件

- Python 3.11+
- Telegram Bot Token([@BotFather](https://t.me/BotFather))+ 频道 ID(bot 设为频道管理员)
- Scweet 数据源(主):
  - **专用 X 账号的 `auth_token` cookie**(浏览器登录 x.com → `F12` → Application → Cookies → `auth_token`)。⚠️ 用专用号,**勿用个人号**。
  - **代理**(国内必须):x.com 被墙,Scweet 的 curl_cffi 不继承系统代理,需显式配置(如 Clash `http://127.0.0.1:7890`)。

## 快速开始

```bash
uv venv
uv pip install -e ".[dev]"

# 配置(密钥走环境变量,其余进 config.yaml)
cp config.example.yaml config.yaml      # 编辑 telegram.bot_token/chat_id、scweet.proxy
export SCWEET_AUTH_TOKEN=<你的auth_token>

# 添加订阅(GUI)
python -m src.admin_gui

# 启动
python -m src.main
```

## 工作原理

```
Source(Scweet/Nitter)取 watermark 之后的新推(连媒体一起下载到 cache)
  → SyncEngine:进 outbox → 发 Sink → 成功推进 watermark;失败重试,超限转 dead-letter
  → Sink(Telegram):media group + caption 发到频道
```

主循环每 `loop_interval` 醒一次,每账号按自己的 `poll_interval`(门控 `last_polled`)决定本轮是否采集。详见 CLAUDE.md。

## 配置

**`config.yaml`**(连接/调度):

| 项 | 说明 |
|----|------|
| `telegram.bot_token` / `chat_id` | TG 机器人 / 目标频道 |
| `source_type` | `scweet`(主,需下方 cookie+代理)/ `nitter`(降级) |
| `scweet.auth_token` / `proxy` | X 专用号 cookie(env `SCWEET_AUTH_TOKEN` 亦可)/ 代理 URL(国内必须) |
| `fetcher.nitter_instance` | Nitter 实例(`source_type=nitter` 时用) |
| `scheduler.loop_interval_seconds` | 主循环粒度(秒);每账号实际间隔在订阅里单独配 |

**环境变量**(密钥,不进 git):`SCWEET_AUTH_TOKEN`。

**每账号**(subscriptions 表,GUI 编辑):`poll_interval`、`sync_mode`(`media_only` / `all`)、`fetch_limit`、`skip_retweets`。

## 数据库

`state.db` 三张表:`subscriptions`(含 watermark)+ `outbox`(in-flight)+ `dead_letter`(放弃的)。**不存全量 post 历史**。

## 测试 / 校验

```bash
pytest -v                                              # 单测(SyncEngine 逻辑等)
# 活体校验(真实账号;Scweet 另需 SCWEET_AUTH_TOKEN + 代理):
ACCOUNT=<账号> SCWEET_AUTH_TOKEN=<token> pytest -k "live or pipeline"   # 取推+下媒体 / 全流程→TG(不重不漏,人工核对)
```

## 项目结构

```
src/
  __init__.py          # setup_logging
  main.py              # 入口:Database + Source + Sink → run_loop
  config.py            # YAML + Pydantic 配置加载/保存
  database.py          # SQLite(subscriptions / outbox / dead_letter / groups)
  sync_engine.py       # 状态模型(watermark/outbox/dead)+ 调度(run_collect/run_once/run_loop)
  telegram_bot.py      # TelegramSink(Sink 契约;media group + caption)
  admin_gui.py         # Tkinter GUI(订阅/分组/配置)
  source/
    base.py            # Source/Sink 契约 + Post/MediaFile + filter_newer
    scweet.py          # ScweetSource(X GraphQL,主)
    nitter.py          # NitterSource(Nitter RSS + fxTwitter,降级)
    download.py        # 共享媒体下载(DiscoveredTweet → Post)
tests/                 # pytest 单测 + live 校验
```
