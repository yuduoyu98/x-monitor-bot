"""SQLite 持久层:subscriptions / outbox / dead_letter / groups。

watermark 是 subscription 的一列;outbox 只存"未结算 + 已结算但在 watermark 之上"的推。
groups 支持订阅分组,每组有总开关(关了 → 组内订阅不轮询)。
"""

from __future__ import annotations

import contextlib
import logging
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from src.sync_engine import OutboxEntry

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS subscriptions (
    account_id TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1,
    sync_mode TEXT NOT NULL DEFAULT 'media_only',
    watermark TEXT,
    remark TEXT NOT NULL DEFAULT '',
    poll_interval INTEGER NOT NULL DEFAULT 86400,
    fetch_limit INTEGER NOT NULL DEFAULT 5,
    skip_retweets INTEGER NOT NULL DEFAULT 1,
    group_name TEXT,
    last_polled TEXT,
    running INTEGER NOT NULL DEFAULT 0,
    running_since TEXT
);
CREATE TABLE IF NOT EXISTS groups (
    name TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    parent_name TEXT
);
CREATE TABLE IF NOT EXISTS outbox (
    account_id TEXT NOT NULL,
    post_id TEXT NOT NULL,
    post_ts TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, post_id)
);
CREATE TABLE IF NOT EXISTS dead_letter (
    account_id TEXT NOT NULL,
    post_id TEXT NOT NULL,
    post_ts TEXT NOT NULL,
    reason TEXT,
    abandoned_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outbox_account ON outbox(account_id);
"""


class Database:
    """subscriptions + outbox + dead_letter 的 async 持久层。"""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def init(self) -> None:
        if str(self._db_path) != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self._db_path))
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA_SQL)
        # 迁移:旧 DB 可能没有 group_name / parent_name 列
        with contextlib.suppress(Exception):
            await self._conn.execute("ALTER TABLE subscriptions ADD COLUMN group_name TEXT")
        with contextlib.suppress(Exception):
            await self._conn.execute("ALTER TABLE groups ADD COLUMN parent_name TEXT")
        await self._conn.commit()
        logger.info("Database initialized at %s", self._db_path)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    # --- subscriptions ---

    async def upsert_subscription(
        self,
        account_id: str,
        *,
        sync_mode: str = "media_only",
        remark: str = "",
        poll_interval: int = 86400,
        fetch_limit: int = 5,
        skip_retweets: bool = True,
        group_name: str | None = None,
    ) -> None:
        """插入或更新订阅配置。ON CONFLICT 只改配置列,保留 watermark/last_polled/running。"""
        await self._conn.execute(
            "INSERT INTO subscriptions "
            "(account_id, sync_mode, remark, poll_interval, "
            "fetch_limit, skip_retweets, group_name) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(account_id) DO UPDATE SET "
            "sync_mode=excluded.sync_mode, remark=excluded.remark, "
            "poll_interval=excluded.poll_interval, fetch_limit=excluded.fetch_limit, "
            "skip_retweets=excluded.skip_retweets, group_name=excluded.group_name",
            (
                account_id,
                sync_mode,
                remark,
                poll_interval,
                fetch_limit,
                int(skip_retweets),
                group_name,
            ),
        )
        await self._conn.commit()

    async def get_subscriptions(self) -> list[dict]:
        cursor = await self._conn.execute("SELECT * FROM subscriptions")
        return [dict(row) for row in await cursor.fetchall()]

    async def delete_subscription(self, account_id: str) -> None:
        await self._conn.execute("DELETE FROM subscriptions WHERE account_id = ?", (account_id,))
        await self._conn.commit()

    async def set_group(self, account_id: str, group_name: str | None) -> None:
        """只改订阅的 group_name(移动分组),不动其它配置/watermark。"""
        await self._conn.execute(
            "UPDATE subscriptions SET group_name = ? WHERE account_id = ?",
            (group_name, account_id),
        )
        await self._conn.commit()

    async def toggle_enabled(self, account_id: str) -> bool:
        """翻转订阅 enabled,返回新状态。关掉时顺手清 running(不再"采集中")。"""
        await self._conn.execute(
            "UPDATE subscriptions SET enabled = 1 - enabled WHERE account_id = ?",
            (account_id,),
        )
        # 刚被关掉(enabled=0)→ 清 running / running_since
        await self._conn.execute(
            "UPDATE subscriptions SET running = 0, running_since = NULL "
            "WHERE account_id = ? AND enabled = 0",
            (account_id,),
        )
        await self._conn.commit()
        cursor = await self._conn.execute(
            "SELECT enabled FROM subscriptions WHERE account_id = ?", (account_id,)
        )
        row = await cursor.fetchone()
        return bool(row[0]) if row else False

    # --- groups ---

    async def upsert_group(
        self, name: str, parent_name: str | None = None, sort_order: int = 0
    ) -> None:
        await self._conn.execute(
            "INSERT INTO groups (name, parent_name, sort_order) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET "
            "sort_order=excluded.sort_order, parent_name=excluded.parent_name",
            (name, parent_name, sort_order),
        )
        await self._conn.commit()

    async def get_groups(self) -> list[dict]:
        cursor = await self._conn.execute("SELECT * FROM groups ORDER BY sort_order, name")
        return [dict(row) for row in await cursor.fetchall()]

    async def toggle_group(self, name: str) -> bool:
        """翻转组开关,返回新状态。"""
        await self._conn.execute("UPDATE groups SET enabled = 1 - enabled WHERE name = ?", (name,))
        await self._conn.commit()
        cursor = await self._conn.execute("SELECT enabled FROM groups WHERE name = ?", (name,))
        row = await cursor.fetchone()
        return bool(row[0]) if row else False

    async def toggle_subgroup(self, name: str) -> bool:
        """小组开关(≠ 顶级):翻转小组 enabled 并同步组内账号 enabled。
        开 → 组内账号全开;关 → 全关 + 清 running。返回新状态。"""
        cur = await self._conn.execute("SELECT enabled FROM groups WHERE name = ?", (name,))
        row = await cur.fetchone()
        if row is None:
            return False
        new = 0 if row["enabled"] else 1
        await self._conn.execute("UPDATE groups SET enabled = ? WHERE name = ?", (new, name))
        await self._conn.execute(
            "UPDATE subscriptions SET enabled = ? WHERE group_name = ?", (new, name)
        )
        if new == 0:
            await self._conn.execute(
                "UPDATE subscriptions SET running = 0, running_since = NULL WHERE group_name = ?",
                (name,),
            )
        await self._conn.commit()
        return bool(new)

    async def delete_group(self, name: str) -> None:
        """删组。
        - 顶级分组:其下全部订阅(直挂 + 各小组里)→ 未分组;小组提升为顶级(parent_name 清空)。
        - 小组:该小组订阅 → 未分组。
        """
        cur = await self._conn.execute("SELECT parent_name FROM groups WHERE name = ?", (name,))
        row = await cur.fetchone()
        is_top = row is None or row["parent_name"] is None
        if is_top:
            cur = await self._conn.execute("SELECT name FROM groups WHERE parent_name = ?", (name,))
            names = [name] + [r["name"] for r in await cur.fetchall()]
            placeholders = ",".join("?" * len(names))
            await self._conn.execute(
                f"UPDATE subscriptions SET group_name = NULL WHERE group_name IN ({placeholders})",
                names,
            )
            await self._conn.execute(
                "UPDATE groups SET parent_name = NULL WHERE parent_name = ?", (name,)
            )
        else:
            await self._conn.execute(
                "UPDATE subscriptions SET group_name = NULL WHERE group_name = ?", (name,)
            )
        await self._conn.execute("DELETE FROM groups WHERE name = ?", (name,))
        await self._conn.commit()

    async def rename_group(self, old: str, new: str) -> None:
        await self._conn.execute("UPDATE groups SET name = ? WHERE name = ?", (new, old))
        await self._conn.execute(
            "UPDATE subscriptions SET group_name = ? WHERE group_name = ?", (new, old)
        )
        # 若是顶级分组,其下小组的 parent_name 跟着改(重命名小组时此句无匹配,安全)
        await self._conn.execute(
            "UPDATE groups SET parent_name = ? WHERE parent_name = ?", (new, old)
        )
        await self._conn.commit()

    # --- watermark ---

    async def get_watermark(self, account_id: str) -> datetime | None:
        cursor = await self._conn.execute(
            "SELECT watermark FROM subscriptions WHERE account_id = ?", (account_id,)
        )
        row = await cursor.fetchone()
        if row and row[0]:
            return datetime.fromisoformat(row[0])
        return None

    async def set_watermark(self, account_id: str, watermark: datetime | None) -> None:
        await self._conn.execute(
            "UPDATE subscriptions SET watermark = ? WHERE account_id = ?",
            (watermark.isoformat() if watermark else None, account_id),
        )
        await self._conn.commit()

    # --- outbox ---

    async def get_outbox(self, account_id: str) -> list[OutboxEntry]:
        cursor = await self._conn.execute(
            "SELECT post_id, post_ts, status, attempts FROM outbox "
            "WHERE account_id = ? ORDER BY post_ts ASC",
            (account_id,),
        )
        return [
            OutboxEntry(
                post_id=row[0],
                post_ts=datetime.fromisoformat(row[1]),
                status=row[2],
                attempts=row[3],
            )
            for row in await cursor.fetchall()
        ]

    async def replace_outbox(self, account_id: str, entries: list[OutboxEntry]) -> None:
        """全量替换某账号的 outbox(run_collect 产出裁剪后的新列表,直接覆盖)。"""
        now = datetime.now(UTC).isoformat()
        await self._conn.execute("DELETE FROM outbox WHERE account_id = ?", (account_id,))
        if entries:
            await self._conn.executemany(
                "INSERT INTO outbox (account_id, post_id, post_ts, status, attempts, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (account_id, e.post_id, e.post_ts.isoformat(), e.status, e.attempts, now)
                    for e in entries
                ],
            )
        await self._conn.commit()

    # --- dead_letter ---

    async def add_dead_letter(
        self, account_id: str, post_id: str, post_ts: datetime, reason: str
    ) -> None:
        await self._conn.execute(
            "INSERT INTO dead_letter (account_id, post_id, post_ts, reason, abandoned_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (account_id, post_id, post_ts.isoformat(), reason, datetime.now(UTC).isoformat()),
        )
        await self._conn.commit()

    async def get_dead_letter(self, account_id: str) -> list[dict]:
        cursor = await self._conn.execute(
            "SELECT * FROM dead_letter WHERE account_id = ? ORDER BY abandoned_at DESC",
            (account_id,),
        )
        return [dict(row) for row in await cursor.fetchall()]

    # --- 调度(SP2-d)---

    async def get_enabled_subscriptions(self) -> list[dict]:
        """返回 enabled=1 且所属分组(及其父分组)enabled=1 的订阅。"""
        cursor = await self._conn.execute(
            "SELECT s.* FROM subscriptions s "
            "LEFT JOIN groups g ON s.group_name = g.name "
            "LEFT JOIN groups pg ON g.parent_name = pg.name "
            "WHERE s.enabled = 1 AND (s.group_name IS NULL OR "
            "(g.enabled = 1 AND (g.parent_name IS NULL OR pg.enabled = 1)))"
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def set_last_polled(self, account_id: str, ts: datetime) -> None:
        await self._conn.execute(
            "UPDATE subscriptions SET last_polled = ? WHERE account_id = ?",
            (ts.isoformat(), account_id),
        )
        await self._conn.commit()

    async def set_running(
        self, account_id: str, running: bool, since: datetime | None = None
    ) -> None:
        ts = (since or datetime.now(UTC)).isoformat() if running else None
        await self._conn.execute(
            "UPDATE subscriptions SET running = ?, running_since = ? WHERE account_id = ?",
            (1 if running else 0, ts, account_id),
        )
        await self._conn.commit()

    async def clear_all_running(self) -> None:
        """清零所有订阅的 running 标记(关闭应用时清理"采集中"孤儿状态)。"""
        await self._conn.execute("UPDATE subscriptions SET running = 0, running_since = NULL")
        await self._conn.commit()
