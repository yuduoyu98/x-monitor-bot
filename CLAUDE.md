# CLAUDE.md

## Project

**x-monitor-bot** — X (Twitter) → Telegram 增量同步。定时轮询订阅账号,把新推文(文本/图片/视频)同步到 TG 频道。跑在个人 PC 上,轻量(省内存/CPU)。

**数据源**:Scweet(直连 X GraphQL + cookie + 代理,主)/ Nitter(降级)。无需 X 官方 API、无需浏览器。

> **状态**:架构重设计**已完成**(SP1~SP4 全落地)。新架构仅存:`source/` + `sync_engine.py` + `telegram_bot.py` + `database.py` + `main.py` + `admin_gui.py`。
> **进度**:SP1 全完成(契约 + ScweetSource ✅ + NitterSource ✅)、SP2 全完成、SP3 Sink ✅、SP4 GUI ✅(手动采集 / dead_letter / 分组菜单 / 水位线直编)。47 单测 + 4 live。每订阅可配 `sync_mode`/`poll_interval`/`fetch_limit`/`skip_retweets`(水位线在 GUI 直接改)。旧架构代码已在 `dd833d4` 全删。

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

**每订阅可配**(subscriptions 表):`sync_mode`(media_only/all)、`poll_interval`(默认 1 天)、`fetch_limit`(每次取几条,默认 5)、`skip_retweets`(转推+引用,默认开)。水位线(`watermark` 列)GUI 可直接改:新增订阅默认填当前时间(不回灌),改过去即从该点回灌/重采。

## Key Components(目标 → 现状)

| 角色 | 目标 | 现状 |
|------|------|------|
| Source 契约 + 纯逻辑 | `src/source/base.py` ✅ | DiscoveredTweet/Post/MediaFile/Source + filter_newer + media_cache_path |
| ScweetSource | `src/source/scweet.py` ✅ | 活体测通过(chipsinblack:真取推 + 下媒体) |
| NitterSource | `src/source/nitter.py` ✅ | 降级源(Nitter RSS + fxTwitter,无需代理;RSS 固定窗口,深回填靠 Scweet) |
| SyncEngine(状态+调度) | `src/sync_engine.py` ✅ | 状态逻辑 + run_collect + collect_account + run_once/run_loop(全完成) |
| Sink(Telegram) | `src/telegram_bot.py` ✅ | TelegramSink(实现 Sink 契约;>50MB 改 raise 不静默丢) |
| 入口接线 | `src/main.py` ✅ | Database + ScweetSource + TelegramSink → run_loop |
| DB(subscriptions/outbox/dead_letter) | `src/database.py` ✅ | 新 schema;旧 posts/sync_log 已砍 |
| Admin GUI | `src/admin_gui.py` ✅ | 分组管理 + 配置弹窗(Tkinter) |

## Dev Standards

- **uv** 包管理,**Ruff** format/lint(行宽 100,双引号)
- **全 async**,类型注解,Pydantic 配置
- **pytest + pytest-asyncio**

## 测试策略

**边界 = 契约。契约内 mock(快、确定);契约外 live(真)。**
- SyncEngine 逻辑(watermark/重试/dead)→ mock Source+Sink 注入受控失败,单测。**核心覆盖。**
- Source 实现 → live 测试(`tests/test_source.py` 的 `*_live` + `tests/test_sync_engine.py` 的 `pipeline_*`,真实账号,人工核对"不漏")。
- 纯函数(推进算法/文件名/caption/config 校验)→ 单测。
- ❌ 别 mock 外部系统行为(写死 fxTwitter JSON)= 剧场。

## Commands

```bash
uv pip install -e ".[dev]"
python -m src.main                                    # 启动主循环(订阅在 state.db)
python -m src.admin_gui                               # 管理 订阅/分组/配置(Tkinter)
# 活体校验(真实账号;账号走 ACCOUNT env,Scweet 另需 SCWEET_AUTH_TOKEN + 代理):
ACCOUNT=<账号> SCWEET_AUTH_TOKEN=<token> pytest -k "live or pipeline"   # 取推+下媒体 / 全流程 Source→Engine→Sink→TG
pytest -v                                             # 全部单测(SyncEngine 逻辑等)
ruff format . && ruff check .
```

> 订阅是 state.db 里的状态,用 `python -m src.admin_gui` 管理(GUI:SP4)。全流程测试/验证靠**入参**(账号走 `ACCOUNT` env、token 走 `SCWEET_AUTH_TOKEN` env、TG 走 config.yaml)。

## 配置 / 密钥

- Scweet `auth_token`(env `SCWEET_AUTH_TOKEN`,绝不进 git)+ `proxy`(国内必须,curl_cffi 不继承系统代理,显式传,如 Clash `http://127.0.0.1:7890`)。
- 不提交:`config.yaml`、`state.db`、`scweet_state.db`、`cache/`、`logs/`。

## 关键决策

- **Scweet 主 / Nitter 降级**:实测 Nitter 给过期/错号 tweet ID;Scweet 直连 X GraphQL 可靠。代价:专用号 cookie + 代理。
- **砍全量 posts 表 + sync_log** → watermark + outbox + dead_letter(轻量,不对比所有 post_id)。
- **Source 打包下载**:契约只管 WHAT(Post 带文件),HOW 看方案,不强行拆 discovery/下载。
- **per-account `poll_interval`**:管理 Scweet 每日抓取预算(每次 poll 都计费)。
- **首次默认 watermark=now**(不回灌刷屏);GUI 可直接改 `watermark` → 改过去即从该点回灌/重采(新增订阅默认填当前时间)。
