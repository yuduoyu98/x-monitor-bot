"""SP2 SyncEngine 测试:状态模型(watermark 推进、重试/dead)。

按 CLAUDE.md 测试策略:状态逻辑是"我们的逻辑"→ 单测(mock Source/Sink 边界)。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from src.database import Database
from src.source.base import Post
from src.sync_engine import (
    OutboxEntry,
    advance_watermark,
    mark_failed,
    run_once,
    run_tick,
    should_poll,
    tick_account,
)


def _entry(post_id: str, iso_ts: str, status: str) -> OutboxEntry:
    return OutboxEntry(post_id=post_id, post_ts=datetime.fromisoformat(iso_ts), status=status)


def test_advance_watermark_stops_at_first_unsettled():
    """watermark = 连续已结算的最高点;遇到第一个未结算就停。"""
    entries = [
        _entry("A", "2026-06-01T00:00:00+00:00", "sent"),
        _entry("B", "2026-06-02T00:00:00+00:00", "failed"),  # 未结算 → 阻断
        _entry("C", "2026-06-03T00:00:00+00:00", "sent"),  # 已结算但在 B 之后,越过不了
    ]
    current = datetime(2026, 5, 1, tzinfo=UTC)

    assert advance_watermark(entries, current) == datetime(2026, 6, 1, tzinfo=UTC)


def test_advance_watermark_equal_ts_one_unsettled_blocks():
    """同一时间点两条,任一未结算 → 该时间点未结算,watermark 不越过它。"""
    entries = [
        _entry("A", "2026-06-01T00:00:00+00:00", "sent"),
        _entry("B1", "2026-06-02T00:00:00+00:00", "sent"),
        _entry("B2", "2026-06-02T00:00:00+00:00", "failed"),  # 同 ts,未结算 → 阻断
    ]
    current = datetime(2026, 5, 1, tzinfo=UTC)

    assert advance_watermark(entries, current) == datetime(2026, 6, 1, tzinfo=UTC)


def test_mark_failed_retries_until_max_then_dead():
    """失败后:未达上限 → failed(下轮重试);达上限 → dead(放弃,转 dead_letter)。"""
    base = datetime(2026, 6, 1, tzinfo=UTC)

    assert mark_failed(OutboxEntry("x", base, "failed", attempts=0), 3) == "failed"
    assert mark_failed(OutboxEntry("x", base, "failed", attempts=1), 3) == "failed"
    assert mark_failed(OutboxEntry("x", base, "failed", attempts=2), 3) == "dead"


# --- should_poll:per-account poll_interval 门控 ---


def test_should_poll_true_when_interval_elapsed():
    last = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
    now = datetime(2026, 6, 1, 0, 10, tzinfo=UTC)  # 10 min later
    assert should_poll(last, poll_interval=300, now=now) is True  # 5min 间隔,已过 10min


def test_should_poll_false_when_interval_not_elapsed():
    last = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
    now = datetime(2026, 6, 1, 0, 2, tzinfo=UTC)  # 2 min later
    assert should_poll(last, poll_interval=300, now=now) is False


def test_should_poll_true_when_never_polled():
    assert should_poll(None, poll_interval=300, now=datetime(2026, 6, 1, tzinfo=UTC)) is True


# --- run_tick:不丢不重的编排(用 fake send 注入受控失败) ---


def _post(post_id: str, iso_ts: str, *, is_retweet: bool = False) -> Post:
    return Post(
        post_id=post_id,
        username="u",
        timestamp=datetime.fromisoformat(iso_ts),
        text=post_id,
        media=[],
        is_retweet=is_retweet,
    )


async def test_run_tick_happy_path_sends_all_and_advances_watermark():
    """全部发送成功 → 都已发,watermark 推到最新,outbox 清空。"""
    discovered = [_post("A", "2026-06-01T00:00:00+00:00"), _post("B", "2026-06-02T00:00:00+00:00")]
    sent: list[str] = []

    async def send(p: Post) -> None:
        sent.append(p.post_id)

    result = await run_tick(discovered, [], datetime(2026, 5, 1, tzinfo=UTC), send, max_retries=3)

    assert sent == ["A", "B"]
    assert result.watermark == datetime(2026, 6, 2, tzinfo=UTC)
    assert result.outbox == []
    assert result.dead == []


async def test_run_tick_does_not_resend_settled_post_above_watermark():
    """gap 场景:A 发、B 失败、C 发 → watermark 卡在 A。C(已发但在 watermark 之上)
    下一轮被重新发现时必须跳过,绝不能重发。"""
    wm0 = datetime(2026, 5, 1, tzinfo=UTC)
    sent: list[str] = []

    async def send_b_fails(p: Post) -> None:
        if p.post_id == "B":
            raise RuntimeError("send fail")
        sent.append(p.post_id)

    discovered = [
        _post("A", "2026-06-01T00:00:00+00:00"),
        _post("B", "2026-06-02T00:00:00+00:00"),
        _post("C", "2026-06-03T00:00:00+00:00"),
    ]
    r1 = await run_tick(discovered, [], wm0, send_b_fails, max_retries=3)

    assert sent == ["A", "C"]  # B 失败
    assert r1.watermark == datetime(2026, 6, 1, tzinfo=UTC)  # 卡在 A(B 阻断)
    assert {e.post_id: e.status for e in r1.outbox} == {"B": "failed", "C": "sent"}

    # tick 2:B 修复了;Source 重新发现 B、C(都 > watermark=A)
    sent.clear()

    async def send_ok(p: Post) -> None:
        sent.append(p.post_id)

    r2 = await run_tick(
        [_post("B", "2026-06-02T00:00:00+00:00"), _post("C", "2026-06-03T00:00:00+00:00")],
        r1.outbox,
        r1.watermark,
        send_ok,
        max_retries=3,
    )

    assert sent == ["B"]  # C 没被重发!
    assert r2.watermark == datetime(2026, 6, 3, tzinfo=UTC)  # B、C 都 settled → 推到 C


async def test_run_tick_marks_dead_after_max_retries_and_unblocks_watermark():
    """永久失败:重试到上限 → dead → watermark 越过它(不永久卡死)。"""
    post = _post("X", "2026-06-01T00:00:00+00:00")

    async def always_fail(p: Post) -> None:
        raise RuntimeError("always")

    r1 = await run_tick([post], [], datetime(2026, 5, 1, tzinfo=UTC), always_fail, max_retries=3)
    assert r1.dead == []
    r2 = await run_tick([post], r1.outbox, r1.watermark, always_fail, max_retries=3)
    assert r2.dead == []
    r3 = await run_tick([post], r2.outbox, r2.watermark, always_fail, max_retries=3)
    assert r3.dead == ["X"]
    assert r3.watermark == datetime(2026, 6, 1, tzinfo=UTC)  # dead 算 settled → 越过


async def test_run_tick_media_only_skips_text_post_but_advances_watermark():
    """media_only:纯文本推(无媒体)不发,但 watermark 照样越过(= 已处理,下轮不重复发现)。"""
    text_post = _post("T", "2026-06-01T00:00:00+00:00")  # media=[]
    sent: list[str] = []

    async def send(p: Post) -> None:
        sent.append(p.post_id)

    result = await run_tick(
        [text_post],
        [],
        datetime(2026, 5, 1, tzinfo=UTC),
        send,
        max_retries=3,
        sync_mode="media_only",
    )

    assert sent == []  # 文本推被跳过,没发
    assert result.watermark == datetime(2026, 6, 1, tzinfo=UTC)  # watermark 仍越过它


async def test_run_tick_skips_retweet_by_default_but_advances_watermark():
    """转推默认跳过(不发),但 watermark 照样越过(= 已处理,不重复发现)。"""
    rt = _post("RT", "2026-06-01T00:00:00+00:00", is_retweet=True)
    sent: list[str] = []

    async def send(p: Post) -> None:
        sent.append(p.post_id)

    result = await run_tick([rt], [], datetime(2026, 5, 1, tzinfo=UTC), send, max_retries=3)

    assert sent == []  # 转推被跳过
    assert result.watermark == datetime(2026, 6, 1, tzinfo=UTC)


async def test_run_tick_sends_retweet_when_skip_disabled():
    """关掉转推过滤(skip_retweets=False)→ 转推照发。"""
    rt = _post("RT", "2026-06-01T00:00:00+00:00", is_retweet=True)
    sent: list[str] = []

    async def send(p: Post) -> None:
        sent.append(p.post_id)

    await run_tick(
        [rt], [], datetime(2026, 5, 1, tzinfo=UTC), send, max_retries=3, skip_retweets=False
    )

    assert sent == ["RT"]


# --- tick_account:Store ↔ run_tick ↔ Source/Sink 的桥接 ---


class FakeSource:
    """按 watermark 过滤的可变测试源(往 self.posts 追加 = 账号发了新推)。"""

    def __init__(self, posts: list[Post]) -> None:
        self.posts = posts
        self.last_limit: int | None = None

    async def get_new_posts(self, account, watermark, *, limit=20):
        self.last_limit = limit
        if watermark is None:
            return list(self.posts)
        return [p for p in self.posts if p.timestamp > watermark]

    async def close(self) -> None: ...


class FakeSink:
    def __init__(self, fail_on=()) -> None:
        self.sent: list[str] = []
        self._fail_on = set(fail_on)

    async def post(self, post: Post):
        if post.post_id in self._fail_on:
            raise RuntimeError("send fail")
        self.sent.append(post.post_id)

    async def close(self) -> None: ...


@pytest.fixture
async def db():
    d = Database(":memory:")
    await d.init()
    try:
        yield d
    finally:
        await d.close()


async def test_tick_account_first_run_sets_watermark_to_now_and_skips(db):
    """首次(watermark=None)→ 设成 now,跳过历史,不发任何东西。"""
    await db.upsert_subscription("alice")
    src = FakeSource([_post("A", "2026-06-01T00:00:00+00:00")])
    sink = FakeSink()
    now = datetime(2026, 6, 5, tzinfo=UTC)

    result = await tick_account(db, src, sink, "alice", now=now)

    assert result.sent == []
    assert await db.get_watermark("alice") == now


async def test_tick_account_processes_persists_and_advances(db):
    """正常:发现→发送→存 watermark;跨 tick 持久化(已发的不再发)。"""
    await db.upsert_subscription("alice")
    await db.set_watermark("alice", datetime(2026, 5, 1, tzinfo=UTC))
    src = FakeSource([_post("A", "2026-06-01T00:00:00+00:00")])
    sink = FakeSink()

    await tick_account(db, src, sink, "alice", now=datetime(2026, 6, 10, tzinfo=UTC))
    assert sink.sent == ["A"]
    assert await db.get_watermark("alice") == datetime(2026, 6, 1, tzinfo=UTC)

    # tick 2:账号发了新推 B;A 已在 watermark 之下,不被重新发现/重发
    src.posts.append(_post("B", "2026-06-02T00:00:00+00:00"))
    await tick_account(db, src, sink, "alice", now=datetime(2026, 6, 10, tzinfo=UTC))
    assert sink.sent == ["A", "B"]  # 只多了 B
    assert await db.get_watermark("alice") == datetime(2026, 6, 2, tzinfo=UTC)


async def test_tick_account_records_dead_letter(db):
    """永久失败到上限 → 写 dead_letter,watermark 越过。"""
    await db.upsert_subscription("alice")
    await db.set_watermark("alice", datetime(2026, 5, 1, tzinfo=UTC))
    src = FakeSource([_post("X", "2026-06-01T00:00:00+00:00")])
    sink = FakeSink(fail_on=["X"])

    for _ in range(3):
        await tick_account(db, src, sink, "alice", max_retries=3)

    dl = await db.get_dead_letter("alice")
    assert [d["post_id"] for d in dl] == ["X"]
    assert await db.get_watermark("alice") == datetime(2026, 6, 1, tzinfo=UTC)


async def test_tick_account_passes_fetch_limit_to_source(db):
    """每订阅的 fetch_limit 透传给 Source(决定取多少条/探 gap 的页大小)。"""
    await db.upsert_subscription("alice")
    await db.set_watermark("alice", datetime(2026, 5, 1, tzinfo=UTC))
    src = FakeSource([_post("A", "2026-06-01T00:00:00+00:00")])
    sink = FakeSink()

    await tick_account(
        db, src, sink, "alice", now=datetime(2026, 6, 10, tzinfo=UTC), fetch_limit=50
    )

    assert src.last_limit == 50


# --- run_once:调度循环的一轮(poll_interval 门控 + running 防并发) ---


async def test_run_once_polls_account_when_interval_elapsed(db):
    await db.upsert_subscription("alice", poll_interval=300, sync_mode="all")
    await db.set_watermark("alice", datetime(2026, 5, 1, tzinfo=UTC))  # 非首次
    src = FakeSource([_post("A", "2026-06-01T00:00:00+00:00")])
    sink = FakeSink()

    await run_once(db, src, sink, now=datetime(2026, 6, 1, 0, 10, tzinfo=UTC))

    assert sink.sent == ["A"]


async def test_run_once_skips_account_when_interval_not_elapsed(db):
    await db.upsert_subscription("alice", poll_interval=300, sync_mode="all")
    await db.set_watermark("alice", datetime(2026, 5, 1, tzinfo=UTC))
    await db.set_last_polled("alice", datetime(2026, 6, 1, 0, 5, tzinfo=UTC))  # 5min 前刚采
    src = FakeSource([_post("A", "2026-06-01T00:00:00+00:00")])
    sink = FakeSink()

    await run_once(db, src, sink, now=datetime(2026, 6, 1, 0, 8, tzinfo=UTC))  # 才过 3min

    assert sink.sent == []


async def test_run_once_skips_currently_running_account(db):
    """账号正在被处理(如手动 Run)→ 本轮跳过,避免并发重复发。"""
    await db.upsert_subscription("alice", poll_interval=300, sync_mode="all")
    await db.set_watermark("alice", datetime(2026, 5, 1, tzinfo=UTC))
    now = datetime(2026, 6, 1, 0, 10, tzinfo=UTC)
    await db.set_running("alice", True, now)
    src = FakeSource([_post("A", "2026-06-01T00:00:00+00:00")])
    sink = FakeSink()

    await run_once(db, src, sink, now=now)

    assert sink.sent == []


async def test_run_once_respects_per_subscription_skip_retweets(db):
    """订阅级 skip_retweets:False → 转推照发(默认 True 会跳过)。"""
    await db.upsert_subscription("alice", poll_interval=300, sync_mode="all", skip_retweets=False)
    await db.set_watermark("alice", datetime(2026, 5, 1, tzinfo=UTC))
    src = FakeSource([_post("RT", "2026-06-01T00:00:00+00:00", is_retweet=True)])
    sink = FakeSink()

    await run_once(db, src, sink, now=datetime(2026, 6, 10, tzinfo=UTC))

    assert sink.sent == ["RT"]  # skip_retweets=False → 转推发了


# --- 全流程活体:ScweetSource → tick_account → TelegramSink → Database ---
# 账号作入参(TEST_ACCOUNT);会真发到 config 里的 TG 频道。无 CLI、无 GUI,纯入参驱动。


@pytest.mark.skipif(
    not os.environ.get("SCWEET_AUTH_TOKEN"),
    reason="needs SCWEET_AUTH_TOKEN + config.yaml(TG) + proxy",
)
async def test_full_flow_live_syncs_post_to_telegram(tmp_path):
    from src.config import load_config
    from src.source.scweet import ScweetSource
    from src.telegram_bot import TelegramSink

    config = load_config("config.yaml")
    account = os.environ.get("TEST_ACCOUNT", "chipsinblack")
    sync_mode = os.environ.get("SYNC_MODE", "media_only")

    start = datetime.now(UTC) - timedelta(days=int(os.environ.get("WATERMARK_DAYS", "2")))
    db = Database(":memory:")
    await db.init()
    await db.upsert_subscription(account)
    await db.set_watermark(account, start)  # 回灌近 2 天用于验证

    source = ScweetSource(auth_token=os.environ["SCWEET_AUTH_TOKEN"], cache_dir=tmp_path)
    sink = TelegramSink(bot_token=config.telegram.bot_token, chat_id=config.telegram.chat_id)
    try:
        result = await tick_account(db, source, sink, account, sync_mode=sync_mode)
    finally:
        await source.close()
        await sink.close()
        await db.close()

    print(
        f"\n[full_flow] {account} mode={sync_mode} sent={result.sent} "
        f"dead={result.dead} wm={result.watermark.isoformat()}"
    )
    # pipeline 跑通即可(watermark 没倒退);媒体推会落到 TG 频道(media_only 下只发媒体)。
    assert result.watermark >= start
