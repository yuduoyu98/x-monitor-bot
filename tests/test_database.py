"""SP2 持久化测试:Database 新 schema(subscriptions / outbox / dead_letter)。

旧的 posts/sync_log 表已移除。outbox 存 OutboxEntry,watermark 是 subscription 的一列。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.database import Database
from src.sync_engine import OutboxEntry


@pytest.fixture
async def db():
    database = Database(":memory:")
    await database.init()
    try:
        yield database
    finally:
        await database.close()


async def test_watermark_roundtrip(db):
    await db.upsert_subscription("alice")
    assert await db.get_watermark("alice") is None  # 新订阅无 watermark

    wm = datetime(2026, 6, 1, 12, tzinfo=UTC)
    await db.set_watermark("alice", wm)
    assert await db.get_watermark("alice") == wm


async def test_upsert_subscription_preserves_watermark(db):
    """改订阅配置不能清掉 watermark。"""
    await db.upsert_subscription("alice")
    await db.set_watermark("alice", datetime(2026, 6, 1, tzinfo=UTC))
    await db.upsert_subscription("alice", sync_mode="all", remark="r")  # 再 upsert
    assert await db.get_watermark("alice") == datetime(2026, 6, 1, tzinfo=UTC)


async def test_outbox_roundtrip(db):
    await db.upsert_subscription("alice")
    entries = [
        OutboxEntry("p1", datetime(2026, 6, 1, tzinfo=UTC), "failed", 2),
        OutboxEntry("p2", datetime(2026, 6, 2, tzinfo=UTC), "pending", 0),
    ]
    await db.replace_outbox("alice", entries)
    assert await db.get_outbox("alice") == entries


async def test_replace_outbox_clears_old_entries(db):
    """replace_outbox 是全量替换:不在新列表里的旧条目被清掉。"""
    await db.upsert_subscription("alice")
    await db.replace_outbox(
        "alice", [OutboxEntry("p1", datetime(2026, 6, 1, tzinfo=UTC), "pending")]
    )
    await db.replace_outbox("alice", [])  # 清空
    assert await db.get_outbox("alice") == []


async def test_dead_letter_add_and_list(db):
    await db.upsert_subscription("alice")
    await db.add_dead_letter("alice", "p1", datetime(2026, 6, 1, tzinfo=UTC), "send failed 3x")
    dl = await db.get_dead_letter("alice")
    assert len(dl) == 1
    assert dl[0]["post_id"] == "p1"
    assert dl[0]["reason"] == "send failed 3x"
