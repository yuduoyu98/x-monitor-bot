"""SyncEngine:状态模型 + 调度(SP2)。

watermark + outbox + dead_letter 三件套(替代旧 posts 表 + sync_log)。
纯逻辑(advance_watermark / 重试决策)+ 单账号采集(collect_account)+ 调度循环(run_once/run_loop)。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from src.source.base import Post, Sink, Source

logger = logging.getLogger(__name__)


@dataclass
class OutboxEntry:
    """outbox 里的一条 in-flight 推。"""

    post_id: str
    post_ts: datetime
    status: str  # "pending" | "sent" | "failed" | "dead"
    attempts: int = 0

    @property
    def settled(self) -> bool:
        """sent / dead / skipped 都算 settled(不再阻塞 watermark)。"""
        return self.status in ("sent", "dead", "skipped")


def advance_watermark(entries: list[OutboxEntry], current: datetime) -> datetime:
    """推进 watermark 到"连续已结算"的最高点。

    watermark = 最大的 T,使所有 post_ts ≤ T 的推都已 settled(sent/dead)。
    遇到第一个含未结算推的时间点就停;同一时间点有多条时,任一未结算则该时间点未结算。
    """
    by_ts: dict[datetime, list[bool]] = {}
    for e in entries:
        by_ts.setdefault(e.post_ts, []).append(e.settled)
    new_wm = current
    for ts in sorted(by_ts):
        if all(by_ts[ts]):
            new_wm = ts
        else:
            break
    return new_wm


def mark_failed(entry: OutboxEntry, max_retries: int) -> str:
    """失败后应进入的状态:未达上限 → 'failed'(下轮重试);达上限 → 'dead'(放弃)。

    entry.attempts 是本次失败"之前"的尝试次数。
    """
    return "dead" if entry.attempts + 1 >= max_retries else "failed"


def should_poll(last_polled: datetime | None, poll_interval: int, *, now: datetime) -> bool:
    """该账号本轮是否该采集:距上次采集已过 poll_interval 秒(或从未采集)。"""
    if last_polled is None:
        return True
    return (now - last_polled).total_seconds() >= poll_interval


@dataclass
class CollectResult:
    """一次采集的结果。"""

    outbox: list[OutboxEntry]  # 剩余 in-flight(未结算 + 已结算但在 watermark 之上)
    watermark: datetime
    sent: list[str]  # 本次成功发送的 post_id
    dead: list[str]  # 本次转为 dead 的 post_id(写 dead_letter)


async def run_collect(
    discovered: list[Post],
    outbox: list[OutboxEntry],
    watermark: datetime,
    send: Callable[[Post], Awaitable[Any]],
    *,
    max_retries: int = 3,
    send_attempts: int = 3,
    send_retry_delay: float = 1.0,
    sync_mode: str = "all",
    skip_retweets: bool = True,
) -> CollectResult:
    """处理一次采集:发现的新推进 outbox → 发 Sink → 更新状态 → 推进 watermark。

    send 抛异常视为该条失败(下轮重试,达上限转 dead)。
    """
    working: dict[str, OutboxEntry] = {
        e.post_id: OutboxEntry(e.post_id, e.post_ts, e.status, e.attempts) for e in outbox
    }
    sent: list[str] = []
    dead: list[str] = []
    skipped_text = 0
    skipped_rt = 0
    failed_send = 0
    for post in sorted(discovered, key=lambda p: p.timestamp):  # 按发帖时间从早到晚发送
        entry = working.get(post.post_id)
        if entry is None:
            entry = OutboxEntry(post_id=post.post_id, post_ts=post.timestamp, status="pending")
            working[post.post_id] = entry
        if entry.settled:
            continue  # 已结算 → 跳过(不重复发)
        ts = post.timestamp.strftime("%Y-%m-%d %H:%M")
        snippet = (post.text or "").replace("\n", " ").strip()[:40]
        prefix = (
            f"[collect] {post.post_id} | {ts} | media={len(post.media)} "
            f"rt={post.is_retweet} | {snippet}"
        )
        if sync_mode == "media_only" and not post.media:
            entry.status = "skipped"
            skipped_text += 1
            logger.info("%s → 跳过(文本,media_only)", prefix)
            continue
        if skip_retweets and post.is_retweet:
            entry.status = "skipped"
            skipped_rt += 1
            logger.info("%s → 跳过(转推/引用)", prefix)
            continue
        # 立即重试:网络抖动等瞬时失败同一轮内重试 send_attempts 次(不等下一轮 poll)。
        # 429/RetryAfter 已在 TelegramSink 内部处理;这里管的是 httpx 连接类瞬时错误。
        last_exc: Exception | None = None
        succeeded_at = 0
        for attempt in range(1, send_attempts + 1):
            try:
                await send(post)
                succeeded_at = attempt
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                if attempt < send_attempts:
                    await asyncio.sleep(send_retry_delay)
        if succeeded_at:
            entry.status = "sent"
            sent.append(post.post_id)
            note = f"(第 {succeeded_at} 次尝试成功)" if succeeded_at > 1 else ""
            logger.info("%s → 已发%s", prefix, note)
        else:
            entry.status = mark_failed(entry, max_retries)
            entry.attempts += 1
            failed_send += 1
            dest = "转 dead_letter" if entry.status == "dead" else "留 outbox 下轮重试"
            logger.warning(
                "%s → 发送 TG 失败,立即重试 %d 次仍败,%s(poll attempt %d/%d): %s",
                prefix,
                send_attempts,
                dest,
                entry.attempts,
                max_retries,
                last_exc,
            )
            if entry.status == "dead":
                dead.append(post.post_id)

    logger.info(
        "[collect] 统计: %d 条 → 已发=%d 跳过(文本=%d, 转推=%d) 发送失败=%d",
        len(discovered),
        len(sent),
        skipped_text,
        skipped_rt,
        failed_send,
    )
    new_wm = advance_watermark(list(working.values()), watermark)
    # 只淘汰"已结算 且 post_ts ≤ watermark"的(Source 不会再返回它们);
    # 已结算但在 watermark 之上(gap:下方有失败推卡住)必须留,否则被重新发现时重发 = 重复。
    pruned = [e for e in working.values() if not e.settled or e.post_ts > new_wm]
    return CollectResult(outbox=pruned, watermark=new_wm, sent=sent, dead=dead)


class SyncStore(Protocol):
    """collect_account 依赖的持久层接口(Database 实现它;避免 sync_engine 反向依赖 database)。"""

    async def get_watermark(self, account_id: str) -> datetime | None: ...
    async def set_watermark(self, account_id: str, watermark: datetime | None) -> None: ...
    async def get_outbox(self, account_id: str) -> list[OutboxEntry]: ...
    async def replace_outbox(self, account_id: str, entries: list[OutboxEntry]) -> None: ...
    async def add_dead_letter(
        self, account_id: str, post_id: str, post_ts: datetime, reason: str
    ) -> None: ...


async def collect_account(
    store: SyncStore,
    source: Source,
    sink: Sink,
    account: str,
    *,
    max_retries: int = 3,
    send_attempts: int = 3,
    send_retry_delay: float = 1.0,
    now: datetime | None = None,
    sync_mode: str = "all",
    skip_retweets: bool = True,
    fetch_limit: int = 20,
) -> CollectResult:
    """单个账号一次采集:读状态 → Source 取推 → run_collect → 写回(outbox/watermark/dead_letter)。

    watermark=None(首次)→ 设成 now 跳过历史,不处理。(GUI 新增订阅时已把水位线设成当前时间。)
    """
    now = now or datetime.now(UTC)
    watermark = await store.get_watermark(account)
    if watermark is None:
        logger.info("[collect] @%s 首次 → watermark=now,跳过历史", account)
        await store.set_watermark(account, now)
        return CollectResult(outbox=[], watermark=now, sent=[], dead=[])

    outbox = await store.get_outbox(account)
    logger.info(
        "[collect] @%s 开始 watermark=%s outbox=%d", account, watermark.isoformat(), len(outbox)
    )
    discovered = await source.get_new_posts(account, watermark, limit=fetch_limit)
    logger.info("[collect] @%s 发现 %d 条新推", account, len(discovered))
    result = await run_collect(
        discovered,
        outbox,
        watermark,
        sink.post,
        max_retries=max_retries,
        send_attempts=send_attempts,
        send_retry_delay=send_retry_delay,
        sync_mode=sync_mode,
        skip_retweets=skip_retweets,
    )

    await store.replace_outbox(account, result.outbox)
    await store.set_watermark(account, result.watermark)
    pending = sum(1 for e in result.outbox if not e.settled)
    logger.info(
        "[collect] @%s 完成 sent=%d dead=%d 待重试(outbox)=%d → watermark=%s",
        account,
        len(result.sent),
        len(result.dead),
        pending,
        result.watermark.isoformat(),
    )
    ts_by_id = {p.post_id: p.timestamp for p in discovered}
    for dead_id in result.dead:
        await store.add_dead_letter(
            account, dead_id, ts_by_id.get(dead_id, now), "send failed (max retries)"
        )
    return result


class LoopStore(SyncStore, Protocol):
    """run_once 额外需要的调度字段(Database 实现它)。"""

    async def get_enabled_subscriptions(self) -> list[dict]: ...
    async def set_last_polled(self, account_id: str, ts: datetime) -> None: ...
    async def set_running(
        self, account_id: str, running: bool, since: datetime | None = None
    ) -> None: ...


def _parse_ts(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def _is_stale(running_since: datetime | None, now: datetime, threshold_s: int = 1800) -> bool:
    """running 超过阈值未结束 → 视作崩溃,可被接管(防卡死)。"""
    if running_since is None:
        return True
    return (now - running_since).total_seconds() > threshold_s


async def run_once(
    store: LoopStore,
    source: Source,
    sink: Sink,
    *,
    now: datetime | None = None,
    max_retries: int = 3,
) -> None:
    """调度循环的一轮:遍历启用的订阅,按 poll_interval 门控 + running 防并发,逐个采集。"""
    now = now or datetime.now(UTC)
    subs = await store.get_enabled_subscriptions()
    polled = 0
    for sub in subs:
        account = sub["account_id"]
        if not should_poll(
            _parse_ts(sub.get("last_polled")), sub.get("poll_interval", 300), now=now
        ):
            continue
        if sub.get("running") and not _is_stale(_parse_ts(sub.get("running_since")), now):
            continue  # 别的执行者正在处理(手动 Run 等)→ 跳过,避免并发重复
        await store.set_running(account, True, now)
        try:
            await collect_account(
                store,
                source,
                sink,
                account,
                max_retries=max_retries,
                now=now,
                sync_mode=sub.get("sync_mode", "media_only"),
                fetch_limit=sub.get("fetch_limit", 20),
                skip_retweets=bool(sub.get("skip_retweets", 1)),
            )
            await store.set_last_polled(account, now)
            polled += 1
        except Exception:
            logger.exception("collect failed for @%s", account)
        finally:
            await store.set_running(account, False)
    logger.info("[loop] 本轮完成:%d 启用,%d 到期采集", len(subs), polled)


async def run_loop(
    store: LoopStore,
    source: Source,
    sink: Sink,
    *,
    loop_interval: int = 300,
    max_retries: int = 3,
    stop_event: asyncio.Event | None = None,
) -> None:
    """主循环:每 loop_interval 秒跑一轮 run_once,直到 stop_event 被 set。"""
    while stop_event is None or not stop_event.is_set():
        try:
            await run_once(store, source, sink, max_retries=max_retries)
        except Exception:
            logger.exception("run_once failed")
        if stop_event is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=loop_interval)
        else:
            await asyncio.sleep(loop_interval)
