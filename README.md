# x-monitor-bot

X (Twitter) → Telegram 增量同步。订阅 X 博主,定时轮询,把新推文(文本/图片/视频)同步到 TG 频道。跑在个人 PC 上,轻量(省内存/CPU)。

**数据源**:Scweet(直连 X GraphQL,主)/ Nitter(降级)。无需 X 官方 API、无需浏览器。

> ⚠️ **架构重设计中**。discovery 已验证(Scweet 跑通),生产管道按 SP1→SP4 重写中。详见 [CLAUDE.md](./CLAUDE.md)。

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
| `scweet.proxy` | 代理 URL(国内必须) |
| `scweet.nitter_instance` | Nitter 实例(降级用) |
| `sync.loop_interval` / `max_retries` | 主循环间隔 / 重试上限 |

**环境变量**(密钥,不进 git):`SCWEET_AUTH_TOKEN`。

**每账号**(subscriptions 表,GUI 编辑):`poll_interval`、`sync_mode`(`media_only` / `all`)。

## 数据库

`state.db` 三张表:`subscriptions`(含 watermark)+ `outbox`(in-flight)+ `dead_letter`(放弃的)。**不存全量 post 历史**。

## 测试 / 校验

```bash
pytest -v                                            # 单测(SyncEngine 逻辑等)
python -m src.verify_discovery <账号>... --backend scweet   # discovery 活体校验(不重不漏,人工核对)
```

## 项目结构

```
src/
  main.py              # 入口
  source/              # Source 契约 + Scweet/Nitter(目标,重写中)
  sync_engine.py       # 状态 + 调度(目标)
  sink/telegram.py     # TelegramSink(目标)
  database.py          # SQLite(subscriptions/outbox/dead_letter)
  admin_gui.py         # Tkinter GUI
  discovery.py         # Source 契约 + 后端(实验,沉淀中)
  verify_discovery.py  # discovery 活体校验 ✅
```

> 标"目标"的模块按新架构重写中;现生产仍用 `fetcher/` + `scheduler.py`。
