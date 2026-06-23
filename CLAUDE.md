# CLAUDE.md

## Project

**x-monitor-bot** — X (Twitter) → Telegram 增量同步。定时轮询订阅账号,把新推文(文本/图片/视频)同步到 TG 频道。跑在个人 PC 上,轻量(省内存/CPU)。

**数据源**:Scweet(直连 X GraphQL + cookie + 代理,主)/ Nitter(降级)。无需 X 官方 API、无需浏览器。

> **状态**:架构重设计中(下述为目标)。`src/discovery.py` + `src/verify_discovery.py` 已实验性跑通;生产 `fetcher/` + `scheduler.py` 尚未按此重写。实现分 SP1(Source)→ SP2(SyncEngine)→ SP3(Sink)→ SP4(GUI)。

## Architecture

三个可切换契约 + 一个状态引擎:

```
Source(可切换) ──list[Post](带已下载媒体)──► SyncEngine(状态+调度)──► Sink(可切换)
  ScweetSource / NitterSource                 TelegramSink
                                                  ▲
                                            Admin GUI(配置/订阅)
```

- **Source**:`get_new_posts(account, watermark) -> list[Post]`。内部完成**发现+下载**(不拆)。`Post.media` 是本地文件。`watermark=None` = 首次。
- **SyncEngine**:watermark 游标 + outbox(in-flight)+ dead_letter(放弃的)。**不存全量 post 历史**(轻量)。
- **Sink**:`post(post) -> message_ids | raises`。

### 不丢不重(核心)

- **watermark = 连续已结算(sent/dead)的高水位**,只越过已结算的推。失败推留上方下轮重试。
- 失败 → outbox 重试,`MAX_RETRIES`(默认 3)后转 dead_letter(不永久卡 watermark)。
- "不漏" 依赖 **Source 完整性**(返回 watermark 后全部推)→ 故 Scweet 主、Nitter 降级。
- TG `send_media_group` 原子(全发或失败)→ 失败重试不重复。

### 调度

主循环每 `loop_interval`(粒度,默认 300s)醒一次;每账号按自己的 `poll_interval`(门控 `last_polled`)决定本轮是否采集。**串行**处理(轻量 + Scweet 限流安全)。`running` 状态防手动 Run 与主循环竞态。

## Key Components(目标 → 现状)

| 角色 | 目标 | 现状 |
|------|------|------|
| Source 契约 + Scweet/Nitter | `source/` | 实验:`src/discovery.py` |
| SyncEngine(状态+调度) | `sync_engine.py` | `src/scheduler.py`(待重写,watermark 有丢推 bug) |
| Sink(Telegram) | `sink/telegram.py` | `src/telegram_bot.py`(>50MB 静默丢,待修) |
| DB(subscriptions/outbox/dead_letter) | 精简 schema | `src/database.py`(含旧 posts/sync_log,待砍) |
| Admin GUI | 重构 | `src/admin_gui.py`(Test 按钮死代码,待重构) |
| discovery 活体校验 | — | `src/verify_discovery.py` ✅ |

## Dev Standards

- **uv** 包管理,**Ruff** format/lint(行宽 100,双引号)
- **全 async**,类型注解,Pydantic 配置
- **pytest + pytest-asyncio**

## 测试策略

**边界 = 契约。契约内 mock(快、确定);契约外 live(真)。**
- SyncEngine 逻辑(watermark/重试/dead)→ mock Source+Sink 注入受控失败,单测。**核心覆盖。**
- Source 实现 → live verify(`verify_discovery.py`,真实账号,人工核对"不漏")。
- 纯函数(推进算法/文件名/caption/config 校验)→ 单测。
- ❌ 别 mock 外部系统行为(写死 fxTwitter JSON)= 剧场。

## Commands

```bash
uv pip install -e ".[dev]"
python -m src.main                                              # 启动
python -m src.admin_gui                                         # GUI
python -m src.verify_discovery <账号>... --backend scweet       # 活体校验 discovery
ruff format . && ruff check .
pytest -v
```

## 配置 / 密钥

- Scweet `auth_token`(env `SCWEET_AUTH_TOKEN`,绝不进 git)+ `proxy`(国内必须,curl_cffi 不继承系统代理,显式传,如 Clash `http://127.0.0.1:7890`)。
- 不提交:`config.yaml`、`state.db`、`scweet_state.db`、`cache/`、`logs/`。

## 关键决策

- **Scweet 主 / Nitter 降级**:实测 Nitter 给过期/错号 tweet ID;Scweet 直连 X GraphQL 可靠。代价:专用号 cookie + 代理。
- **砍全量 posts 表 + sync_log** → watermark + outbox + dead_letter(轻量,不对比所有 post_id)。
- **Source 打包下载**:契约只管 WHAT(Post 带文件),HOW 看方案,不强行拆 discovery/下载。
- **per-account `poll_interval`**:管理 Scweet 每日抓取预算(每次 poll 都计费)。
- **首次 watermark=now**(不回灌刷屏);回灌功能 deferred(待查 Scweet 分页能力)。
