# 链接 → TG 一次性管道(One-shot Link Sender)

**日期**: 2026-06-27
**状态**: 设计已批准(简化版),待写实施计划

## 目标

GUI 输入一条推文链接,一次性把该推(文本 + 媒体)发送到配置的 Telegram 频道。

## 硬约束(用户明确)

- 无 watermark 更新、无重试、无持久缓存、无任何过滤。
- 一次性尝试:取推 → 发送 → 结束。

## 非目标(YAGNI)

- 不写 `state.db`:不动 watermark / outbox / dead_letter。
- 不落持久 cache:媒体下到**临时目录**,发完即删。
- 不过滤:纯文本推照发(文本消息);转推/引用照发。
- 不重试、不进 dead_letter。
- 不新增任何配置项(复用 `config.yaml` 的 `bot_token` + `chat_id`)。
- **不引入 discover 那套抽象**:不用 `DiscoveredTweet` / `download_post` / `filter_newer`,不改 `base.py`。fxTwitter 一次就给全量,直接拼 `Post` 发。

## 数据源

fxTwitter 公开 API(`api.fweet.com/status/{id}`):免认证、免代理(国内未墙)、返回文本 + 媒体直链(photo/video/gif)。

> ScweetSource 不适用:它按账号+水位线拉 timeline,无"按推文 ID 取单条"接口,且整套是增量 discover 机制,与一次性发送无关。

## 数据流(一条直线)

```
LinkSendDialog(URL)
  → parse_tweet_url(url) → tweet_id | None
  → fetch_tweet(http, tweet_id) → FetchedTweet | None     [fxTwitter GET /status/{id}]
  → 媒体URL 逐个下到 tmp_dir → list[MediaFile]
  → 拼 Post(post_id, username, timestamp, text, media, url, display_name)
  → sink.post(post) → message_ids                         [媒体组 / 纯文本 / >50MB 降级]
  → finally: shutil.rmtree(tmp_dir)
```

## 组件

### 1. `src/source/fxtwitter.py`(新,自包含)

oneshot 专用,不依赖项目的 source 抽象。

- `parse_tweet_url(url: str) -> str | None`
  - 接受:`x.com/{u}/status/{id}`、`twitter.com/{u}/status/{id}`、`x.com/i/status/{id}`、裸数字 ID、nitter 实例 `.../{u}/status/{id}`。
  - 返回 `tweet_id`;无法识别 → `None`。

- `@dataclass FetchedTweet`(本模块局部):`post_id, username, display_name, timestamp(datetime), text, media: list[MediaRef]`(`MediaRef` 是 base.py 里现成的 `url+type` 容器,直接借用,不改契约)。

- `async fetch_tweet(http: httpx.AsyncClient, tweet_id: str) -> FetchedTweet | None`
  - `GET https://api.fweet.com/status/{tweet_id}`,带浏览器 UA(`Mozilla/5.0 ... Chrome/127`)。
  - 解析 `tweet.text`、`tweet.created_at`(ISO → datetime,UTC)、`tweet.user.{screen_name,name}`、`tweet.media.{photos,videos,gifs}` → `MediaRef`(photo:`media_url_https` + `?name=orig`;video/gif:直接 `url`)。
  - HTTP 非 200 / 无 `tweet` / 异常 → `None`。

### 2. `src/oneshot.py`(新)

编排管道。零 DB、零过滤、一次尝试。

```python
@dataclass
class OneShotResult:
    ok: bool
    message: str                 # 给 GUI 显示
    media_count: int = 0
    message_ids: list[int] = field(default_factory=list)

async def send_tweet_by_url(url: str, sink: Sink) -> OneShotResult: ...
```

行为:
- `parse_tweet_url` → None:`OneShotResult(ok=False, message="无法识别推文链接")`。
- 自建 `httpx.AsyncClient(timeout=60, follow_redirects=True, headers={UA})`(fxTwitter 不需代理),`finally: await http.aclose()`。
- `fetch_tweet` → None:`ok=False, "取推失败(可能私密/被删/fxTwitter 无缓存)"`。
- 文本与媒体都为空:`ok=False, "该推文无内容"`。
- `tmp_dir = tempfile.mkdtemp(prefix="xmon_oneshot_")`;**自己写一个小下载循环**(不复用 download_post):每个 `MediaRef.url` → `GET` → 写 `tmp_dir/{i:02d}.{ext}`(photo→`.jpg`,video/gif→`.mp4`)→ `MediaFile(path,type,url)`;任一下载失败 → `ok=False, "媒体下载失败: …"`。`finally: shutil.rmtree(tmp_dir, ignore_errors=True)`。
- 拼 `Post(post_id, username=ft.username, timestamp, text, media, url=f"https://x.com/{ft.username}/status/{ft.post_id}", display_name=ft.display_name)`。
- `sink.post(post)` 成功 → `ok=True, message="已发送({media_count} 媒体)", message_ids=…`;异常 → `ok=False, message=f"发送失败: {e}"`。
- 无重试。`sink` 实现现有 `Sink` 契约(`post`/`close`)。

### 3. `src/admin_gui.py`(改)

- 顶栏加按钮 `🔗 链接发送` → `_link_send`。
- `LinkSendDialog(tk.Toplevel)`:URL 输入框 + 发送/取消;`<Return>` 触发发送,`<Escape>` 关闭;`transient` + `grab_set`(与现有对话框一致)。
- `_link_send`:读 URL、非空校验、关对话框 → `_loop.spawn(self._run_link_send(url))` → 启动进度条 + 状态栏"发送中…"。
- `_run_link_send`:
  - `sink = self._ensure_pipeline()[1]`(复用已缓存的 sink;缺 token/chat_id → raise → catch 弹"配置错误")。
  - `await send_tweet_by_url(url, sink)`。
  - `self.root.after(0, lambda: self._finish_link_send(result))`。
- `_finish_link_send`:停进度条、更新状态栏、`messagebox`(成功 `showinfo` / 失败 `showerror`)。

## 错误处理

| 场景 | 结果 |
|---|---|
| URL 无法识别 | messagebox「无法识别推文链接」 |
| fxTwitter 取不到 | messagebox「取推失败: …」 |
| 无文本也无媒体 | messagebox「该推文无内容」 |
| 媒体下载失败 | messagebox「媒体下载失败: …」(临时文件照清) |
| 发送失败 | messagebox「发送失败: …」 |
| 缺 bot_token/chat_id | messagebox「配置错误(检查 config.yaml)」 |
| >50MB 视频 | 沿用 Sink 文本降级,正常返回 ok=True |

全流程写日志(GUI 日志窗口可见)。临时文件任何路径都清理。

## 测试(CLAUDE.md:契约内单测 + 外部 live)

- `parse_tweet_url`:纯函数单测 —— `x.com` / `twitter.com` / `x.com/i/status/` / 裸 ID / nitter / 非法 各情形。
- `send_tweet_by_url`:契约级单测,免网络 —— mock `fetch_tweet` 返回**无媒体**的 `FetchedTweet`(纯文本 → 不触发下载)+ mock `sink` → 断言 `sink.post` 调一次、`ok`/`message` 正确、tmp 目录已删、无重试;另测 `fetch_tweet` 返回 None 与 URL 非法的分支。
- `fetch_tweet`:live 测试(opt-in,沿用 NitterSource live 风格)取已知公开推,断言文本+媒体解析出。**不写死 JSON**(反剧场)。

## 范围

单一实施计划可覆盖:2 新文件(`src/source/fxtwitter.py`、`src/oneshot.py`)+ 改 `src/admin_gui.py` + 测试。**不改 `base.py`**。
