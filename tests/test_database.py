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


async def test_clear_all_running_resets_running_flags(db):
    """关闭应用清理:把所有 running=1 的订阅清零(防"采集中"孤儿状态)。"""
    await db.upsert_subscription("alice")
    await db.upsert_subscription("bob")
    await db.set_running("alice", True)
    await db.set_running("bob", True)
    await db.clear_all_running()
    subs = await db.get_subscriptions()
    assert all(s["running"] == 0 for s in subs)
    assert all(s["running_since"] is None for s in subs)


async def test_toggle_enabled_off_clears_running(db):
    """关掉订阅时清掉 running(开→关 → 不再"采集中")。"""
    await db.upsert_subscription("alice")
    await db.set_running("alice", True)
    assert await db.toggle_enabled("alice") is False  # 关
    alice = next(s for s in await db.get_subscriptions() if s["account_id"] == "alice")
    assert alice["running"] == 0
    assert alice["running_since"] is None


async def test_toggle_enabled_on_keeps_running(db):
    """开启订阅不动 running(关→开)。"""
    await db.upsert_subscription("alice")
    await db.toggle_enabled("alice")  # 先关
    await db.set_running("alice", True)
    await db.toggle_enabled("alice")  # 开
    alice = next(s for s in await db.get_subscriptions() if s["account_id"] == "alice")
    assert alice["running"] == 1  # 没被清


async def test_set_group_moves_subscription(db):
    """set_group 只改 group_name,不动其它配置。"""
    await db.upsert_group("游戏")
    await db.upsert_subscription("alice", sync_mode="all", remark="r")
    await db.set_group("alice", "游戏")
    alice = next(s for s in await db.get_subscriptions() if s["account_id"] == "alice")
    assert alice["group_name"] == "游戏"
    assert alice["sync_mode"] == "all"  # 其它配置没被清
    assert alice["remark"] == "r"
    # 移回未分组
    await db.set_group("alice", None)
    alice = next(s for s in await db.get_subscriptions() if s["account_id"] == "alice")
    assert alice["group_name"] is None


# --- groups ---


async def test_group_roundtrip(db):
    await db.upsert_group("游戏")
    groups = await db.get_groups()
    assert len(groups) == 1
    assert groups[0]["name"] == "游戏"
    assert groups[0]["enabled"] == 1


async def test_toggle_group(db):
    await db.upsert_group("游戏")
    assert await db.toggle_group("游戏") is False  # 1→0
    assert await db.toggle_group("游戏") is True  # 0→1


async def test_delete_group_nulls_subscriptions(db):
    await db.upsert_group("游戏")
    await db.upsert_subscription("alice", group_name="游戏")
    await db.delete_group("游戏")
    subs = await db.get_subscriptions()
    assert subs[0]["group_name"] is None


async def test_get_enabled_subscriptions_filters_disabled_group(db):
    """组关了 → 组内订阅不被 get_enabled_subscriptions 返回(即使个人 enabled=1)。"""
    await db.upsert_group("游戏")
    await db.upsert_subscription("alice", group_name="游戏")
    await db.upsert_subscription("bob")  # 无组

    enabled = await db.get_enabled_subscriptions()
    assert {s["account_id"] for s in enabled} == {"alice", "bob"}

    await db.toggle_group("游戏")  # 关组
    enabled = await db.get_enabled_subscriptions()
    assert {s["account_id"] for s in enabled} == {"bob"}


# --- 小组(分组下嵌套小组,两级)---


async def test_upsert_subgroup_with_parent(db):
    await db.upsert_group("主号")
    await db.upsert_group("工作号", parent_name="主号")
    groups = {g["name"]: g for g in await db.get_groups()}
    assert groups["主号"]["parent_name"] is None
    assert groups["工作号"]["parent_name"] == "主号"


async def test_delete_top_group_sends_all_subs_ungrouped_and_promotes_subgroups(db):
    """删顶级分组:直挂 + 各小组里的订阅全归未分组;小组提升为顶级(保留)。"""
    await db.upsert_group("张三")
    await db.upsert_group("工作号", parent_name="张三")
    await db.upsert_group("私人号", parent_name="张三")
    await db.upsert_subscription("a", group_name="张三")  # 直挂
    await db.upsert_subscription("b", group_name="工作号")  # 小组里
    await db.upsert_subscription("c", group_name="私人号")  # 小组里

    await db.delete_group("张三")

    subs = {s["account_id"]: s for s in await db.get_subscriptions()}
    assert subs["a"]["group_name"] is None
    assert subs["b"]["group_name"] is None
    assert subs["c"]["group_name"] is None
    groups = {g["name"]: g for g in await db.get_groups()}
    assert "张三" not in groups  # 顶级已删
    assert groups["工作号"]["parent_name"] is None  # 小组提升为顶级
    assert groups["私人号"]["parent_name"] is None


async def test_delete_subgroup_sends_its_subs_ungrouped(db):
    """删小组:该小组订阅归未分组;兄弟小组与顶级不动。"""
    await db.upsert_group("张三")
    await db.upsert_group("工作号", parent_name="张三")
    await db.upsert_group("私人号", parent_name="张三")
    await db.upsert_subscription("a", group_name="工作号")
    await db.upsert_subscription("b", group_name="私人号")

    await db.delete_group("工作号")

    subs = {s["account_id"]: s for s in await db.get_subscriptions()}
    assert subs["a"]["group_name"] is None  # 工作号的订阅归未分组
    assert subs["b"]["group_name"] == "私人号"  # 兄弟小组不动
    groups = {g["name"]: g for g in await db.get_groups()}
    assert "工作号" not in groups
    assert groups["私人号"]["parent_name"] == "张三"  # 兄弟小组父不变


async def test_rename_top_group_updates_subgroup_parents(db):
    """重命名顶级分组:其下小组的 parent_name 跟着改。"""
    await db.upsert_group("张三")
    await db.upsert_group("工作号", parent_name="张三")
    await db.upsert_subscription("a", group_name="张三")

    await db.rename_group("张三", "zs")

    subs = {s["account_id"]: s for s in await db.get_subscriptions()}
    assert subs["a"]["group_name"] == "zs"
    groups = {g["name"]: g for g in await db.get_groups()}
    assert groups["工作号"]["parent_name"] == "zs"


async def test_get_enabled_subscriptions_respects_parent_group(db):
    """小组订阅需小组 enabled 且父分组 enabled;父分组关 → 其下全不采。"""
    await db.upsert_group("张三")
    await db.upsert_group("工作号", parent_name="张三")
    await db.upsert_subscription("a", group_name="工作号")
    await db.upsert_subscription("b", group_name="张三")

    assert {s["account_id"] for s in await db.get_enabled_subscriptions()} == {"a", "b"}

    await db.toggle_group("张三")  # 关父分组
    assert await db.get_enabled_subscriptions() == []  # a(小组)+ b(直挂)都不采


async def test_toggle_subgroup_syncs_account_enabled(db):
    """小组开关 ≠ 顶级:翻转小组 enabled 并同步组内账号 enabled(关→全关+清running;开→全开)。"""
    await db.upsert_group("张三")
    await db.upsert_group("工作号", parent_name="张三")
    await db.upsert_subscription("a", group_name="工作号")
    await db.upsert_subscription("b", group_name="工作号")
    await db.upsert_subscription("c", group_name="张三")  # 顶级直挂,不受影响
    await db.set_running("a", True)

    new = await db.toggle_subgroup("工作号")  # 关
    assert new is False
    subs = {s["account_id"]: s for s in await db.get_subscriptions()}
    assert subs["a"]["enabled"] == 0
    assert subs["b"]["enabled"] == 0
    assert subs["a"]["running"] == 0  # 关时清 running
    assert subs["c"]["enabled"] == 1  # 顶级直挂不动
    assert {g["name"]: g["enabled"] for g in await db.get_groups()}["工作号"] == 0

    new = await db.toggle_subgroup("工作号")  # 开
    assert new is True
    subs = {s["account_id"]: s for s in await db.get_subscriptions()}
    assert subs["a"]["enabled"] == 1
    assert subs["b"]["enabled"] == 1
