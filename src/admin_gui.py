"""Tkinter admin GUI for managing all config settings.

Run independently from the bot:
    .venv/Scripts/python -m src.admin_gui
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

# Allow running as script directly: python src/admin_gui.py
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

import asyncio as _asyncio

from src.config import AppConfig, load_config, save_config
from src.database import Database

CONFIG_PATH = Path("config.yaml")
EXAMPLE_PATH = Path("config.example.yaml")


# =============================================================================
# Config helpers
# =============================================================================


def _ensure_config() -> AppConfig | None:
    """Ensure config.yaml exists, offer to create from example."""
    if not CONFIG_PATH.exists():
        root = tk.Tk()
        root.withdraw()
        if EXAMPLE_PATH.exists() and messagebox.askyesno(
            "Config not found",
            "config.yaml not found. Create from config.example.yaml?",
        ):
            shutil.copy(EXAMPLE_PATH, CONFIG_PATH)
            messagebox.showinfo("Done", "config.yaml created. Please fill in your settings.")
        root.destroy()
    if not CONFIG_PATH.exists():
        messagebox.showerror("Error", "config.yaml not found.")
        return None
    try:
        return load_config()
    except Exception as e:
        messagebox.showerror("Error", f"Failed to load config:\n{e}")
        return None


# =============================================================================
# Subscriptions tab
# =============================================================================


class SubscriptionDialog(tk.Toplevel):
    """Dialog for adding or editing a subscription."""

    def __init__(
        self, parent: tk.Tk, title: str = "Add Subscription", data: dict | None = None
    ) -> None:
        super().__init__(parent)
        self.title(title)
        self.result: dict | None = None
        self.geometry("400x350")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        # Username
        ttk.Label(self, text="Account ID (不带 @):").pack(padx=10, pady=(10, 0), anchor="w")
        self.account_var = tk.StringVar(value=data["account_id"] if data else "")
        ttk.Entry(self, textvariable=self.account_var, width=45).pack(padx=10, fill="x")

        # Sync mode
        ttk.Label(self, text="Sync Mode:").pack(padx=10, pady=(10, 0), anchor="w")
        self._mode_var = tk.StringVar(
            value=data.get("sync_mode", "media_only") if data else "media_only"
        )
        mode_combo = ttk.Combobox(
            self,
            textvariable=self._mode_var,
            values=["media_only", "all"],
            state="readonly",
            width=20,
        )
        mode_combo.pack(padx=10, anchor="w", pady=2)
        ttk.Label(self, text="media_only: 仅媒体帖 / all: 所有推文（含纯文字）", font=("", 8)).pack(
            padx=10, anchor="w"
        )

        # Name
        ttk.Label(self, text="Name:").pack(padx=10, pady=(10, 0), anchor="w")
        self._remark_var = tk.StringVar(value=data.get("remark", "") if data else "")
        ttk.Entry(self, textvariable=self._remark_var, width=45).pack(padx=10, fill="x")

        # Initialize (only editable on Add, not Edit)
        is_edit = data is not None
        init_frame = ttk.Frame(self)
        init_frame.pack(padx=10, fill="x", pady=(6, 0))
        self._init_var = tk.BooleanVar(value=data.get("initialize", True) if data else True)
        init_cb = ttk.Checkbutton(
            init_frame,
            text="初始化（勾选=全量同步 RSS 能拿到的所有媒体帖）",
            variable=self._init_var,
        )
        init_cb.pack(anchor="w")
        if is_edit:
            init_cb.configure(state="disabled")

        # Poll interval
        ttk.Label(self, text="Poll Interval (min, 默认60=1h, 1440=1d):").pack(
            padx=10, pady=(10, 0), anchor="w"
        )
        self.poll_var = tk.StringVar(
            value=str(data.get("poll_interval_minutes", 60)) if data else "60"
        )
        ttk.Entry(self, textvariable=self.poll_var, width=15).pack(padx=10, anchor="w")

        # Buttons
        btn_frame = ttk.Frame(self)
        btn_frame.pack(pady=15)
        ttk.Button(btn_frame, text="Save", command=self._on_save).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="Cancel", command=self.destroy).pack(side="left", padx=5)

        self.bind("<Return>", lambda _: self._on_save())
        self.bind("<Escape>", lambda _: self.destroy())

    def _on_save(self) -> None:
        account_id = self.account_var.get().strip()
        if not account_id:
            messagebox.showerror("Error", "Username is required.", parent=self)
            return

        self.result = {
            "account_id": account_id,
            "sync_mode": self._mode_var.get(),
            "initialize": self._init_var.get(),
            "remark": self._remark_var.get().strip(),
        }
        poll_str = self.poll_var.get().strip()
        if poll_str:
            try:
                self.result["poll_interval_minutes"] = int(poll_str)
            except ValueError:
                messagebox.showerror("Error", "Poll interval must be a number.", parent=self)
                return
        self.destroy()


class SubscriptionsTab(ttk.Frame):
    """Subscriptions management tab with add/edit/delete/test."""

    def __init__(self, parent: ttk.Notebook, db: Database) -> None:
        super().__init__(parent)
        self._db = db
        self._build_ui()
        self._refresh()
        self._auto_refresh()

    def _auto_refresh(self) -> None:
        self._refresh()
        self.after(30000, self._auto_refresh)  # every 30s

    def _build_ui(self) -> None:
        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", padx=10, pady=5)
        ttk.Button(toolbar, text="+ Add", command=self._add).pack(side="left", padx=2)
        ttk.Button(toolbar, text="Edit", command=self._edit).pack(side="left", padx=2)
        ttk.Button(toolbar, text="Delete", command=self._delete).pack(side="left", padx=2)
        ttk.Button(toolbar, text="▶ Test", command=self._test).pack(side="left", padx=2)
        ttk.Button(toolbar, text="⏯ Toggle", command=self._toggle).pack(side="left", padx=2)
        ttk.Separator(self, orient="horizontal").pack(fill="x")

        cols = ("name", "account_id", "sync_mode", "poll", "watermark", "on")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", selectmode="browse")
        self.tree.heading("name", text="Name")
        self.tree.heading("account_id", text="Account ID")
        self.tree.heading("sync_mode", text="Mode")
        self.tree.heading("poll", text="Poll(min)")
        self.tree.heading("watermark", text="Watermark")
        self.tree.heading("on", text="On")
        self.tree.column("name", width=100)
        self.tree.column("account_id", width=120)
        self.tree.column("sync_mode", width=70)
        self.tree.column("poll", width=65)
        self.tree.column("watermark", width=140)
        self.tree.column("on", width=35)
        self.tree.pack(fill="both", expand=True, padx=10, pady=5)
        self.tree.bind("<Double-1>", lambda _: self._edit())

    # -- data --

    def _refresh(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)

        def _run():
            return _asyncio.run(self._db.get_subscriptions())

        subs = _run()
        for s in subs:
            poll = s.get("poll_interval_minutes", "")
            wm = s.get("last_post_at") or "-"
            remark = s.get("remark", "")
            on = "✓" if s.get("enabled", 1) else "✗"
            self.tree.insert(
                "",
                "end",
                values=(remark, s["account_id"], s.get("sync_mode", ""), poll, wm, on),
            )

    def _save(self) -> None:
        self._refresh()

    # -- actions --

    def _add(self) -> None:
        dlg = SubscriptionDialog(self, title="Add Subscription")
        self.wait_window(dlg)
        if dlg.result is None:
            return
        # Check duplicates
        existing = _asyncio.run(self._db.get_subscriptions())
        if any(s["account_id"] == dlg.result["account_id"] for s in existing):
            messagebox.showerror("Error", f"@{dlg.result['account_id']} already exists.")
            return
        _asyncio.run(
            self._db.upsert_subscription(
                account_id=dlg.result["account_id"],
                sync_mode=dlg.result["sync_mode"],
                sync_retweets=False,
                initialize=dlg.result.get("initialize", True),
                remark=dlg.result.get("remark", ""),
                poll_interval_minutes=dlg.result.get("poll_interval_minutes"),
            )
        )
        self._save()

    def _edit(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return messagebox.showinfo("Info", "Select a subscription first.")
        uname = self.tree.item(sel[0])["values"][1]  # account_id is col 1
        existing = _asyncio.run(self._db.get_subscriptions())
        target = next((s for s in existing if s["account_id"] == uname), None)
        if not target:
            return
        dlg = SubscriptionDialog(
            self,
            title=f"Edit @{uname}",
            data={
                "account_id": target["account_id"],
                "sync_mode": target.get("sync_mode", "media_only"),
                "initialize": bool(target.get("initialize", 1)),
                "remark": target.get("remark", ""),
                "poll_interval_minutes": target.get("poll_interval_minutes"),
            },
        )
        self.wait_window(dlg)
        if dlg.result is None:
            return
        # Check duplicates if account_id changed
        if dlg.result["account_id"] != uname and any(
            s["account_id"] == dlg.result["account_id"] for s in existing
        ):
            messagebox.showerror("Error", f"@{dlg.result['account_id']} already exists.")
            return
        _asyncio.run(
            self._db.upsert_subscription(
                account_id=dlg.result["account_id"],
                sync_mode=dlg.result["sync_mode"],
                sync_retweets=False,
                initialize=dlg.result.get("initialize", True),
                remark=dlg.result.get("remark", ""),
                poll_interval_minutes=dlg.result.get("poll_interval_minutes"),
            )
        )
        if dlg.result["account_id"] != uname:
            _asyncio.run(self._db.delete_subscription(uname))
        self._save()

    def _delete(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return messagebox.showinfo("Info", "Select a subscription first.")
        uname = self.tree.item(sel[0])["values"][1]  # account_id is col 1
        if not messagebox.askyesno("Confirm", f"Delete subscription @{uname}?"):
            return
        _asyncio.run(self._db.delete_subscription(uname))
        self._save()

    # -- test --

    def _toggle(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return messagebox.showinfo("Info", "Select a subscription first.")
        uname = self.tree.item(sel[0])["values"][1]
        _asyncio.run(self._db.toggle_enabled(uname))
        self._refresh()

    def _test(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return messagebox.showinfo("Info", "Select a subscription to test.")

        uname = self.tree.item(sel[0])["values"][1]  # account_id is col 1
        fetcher_cfg = self._config.fetcher

        dlg = TestDialog(self, username=uname, fetcher_cfg=fetcher_cfg)
        self.wait_window(dlg)


class TestDialog(tk.Toplevel):
    """Dialog that runs a test: fetches the latest media post from an X account."""

    def __init__(self, parent: tk.Widget, username: str, fetcher_cfg) -> None:
        super().__init__(parent)
        self.title(f"Test @{username}")
        self.geometry("500x400")
        self.transient(parent)
        self.grab_set()

        self._username = username
        self._fetcher_cfg = fetcher_cfg

        # Status area
        self.status_label = ttk.Label(self, text=f"Testing @{username}...")
        self.status_label.pack(padx=10, pady=10)

        self.progress = ttk.Progressbar(self, mode="indeterminate")
        self.progress.pack(fill="x", padx=10)
        self.progress.start()

        # Result text
        text_frame = ttk.Frame(self)
        text_frame.pack(fill="both", expand=True, padx=10, pady=5)
        self.result_text = tk.Text(text_frame, wrap="word", height=15, state="disabled")
        scrollbar = ttk.Scrollbar(text_frame, command=self.result_text.yview)
        self.result_text.configure(yscrollcommand=scrollbar.set)
        self.result_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # Close button
        ttk.Button(self, text="Close", command=self.destroy).pack(pady=10)

        # Run test in background thread
        threading.Thread(target=self._run_test, daemon=True).start()

    def _log(self, msg: str) -> None:
        self.after(0, lambda: self._append_text(msg))

    def _append_text(self, msg: str) -> None:
        self.result_text.configure(state="normal")
        self.result_text.insert("end", msg + "\n")
        self.result_text.see("end")
        self.result_text.configure(state="disabled")

    def _run_test(self) -> None:
        try:
            asyncio.run(self._async_test())
        except Exception as e:
            self._log(f"ERROR: {e}")
        finally:
            self.after(0, self.progress.stop)
            self.after(0, lambda: self.status_label.configure(text="Test complete."))

    async def _async_test(self) -> None:
        from tempfile import TemporaryDirectory

        from src.downloader import MediaDownloader
        from src.fetcher import create_fetcher

        self._log(f"=== Testing @{self._username} ===\n")
        self._log(f"Fetcher: {self._fetcher_cfg.type}")

        # 1. Create fetcher
        self._log("Launching browser...")
        fetcher = create_fetcher(self._fetcher_cfg)
        try:
            # 2. Fetch latest posts
            self._log(f"Fetching posts from @{self._username}...")
            posts = await fetcher.fetch_recent_posts(self._username)
            self._log(f"Fetched {len(posts)} posts total.")

            # 3. Find first post with media
            media_post = None
            for p in posts:
                if p.media:
                    media_post = p
                    break

            if not media_post:
                self._log("Result: No media posts found in latest batch.")
                return

            self._log(f"\nPost ID: {media_post.post_id}")
            self._log(f"Time: {media_post.timestamp}")
            self._log(f"Text: {media_post.text[:100]}...")
            self._log(f"URL: {media_post.url}")
            self._log(f"Media items: {len(media_post.media)}")

            for i, m in enumerate(media_post.media):
                self._log(f"  [{i + 1}] {m.type}: {m.url[:80]}...")

            # 4. Download media
            self._log("\nDownloading media...")
            with TemporaryDirectory() as tmpdir:
                dl = MediaDownloader(cache_dir=tmpdir)
                try:
                    paths = await dl.download_post_media(media_post)
                    for fp in paths:
                        size = fp.stat().st_size
                        self._log(f"  Downloaded: {fp.name} ({_format_size(size)})")
                finally:
                    await dl.close()

            self._log("\n✓ Test passed!")

        finally:
            self._log("Closing browser...")
            await fetcher.close()


def _format_size(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    elif size >= 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size} B"


# =============================================================================
# Settings tabs (Telegram / Storage / Scheduler)
# =============================================================================


class TelegramTab(ttk.Frame):
    """Telegram settings tab."""

    def __init__(self, parent: ttk.Notebook, config: AppConfig) -> None:
        super().__init__(parent)
        self._config = config
        ttk.Label(self, text="Bot Token:", font=("", 10, "bold")).pack(
            padx=10, pady=(15, 0), anchor="w"
        )
        ttk.Label(self, text="从 @BotFather 获取").pack(padx=10, anchor="w")
        self._token_var = tk.StringVar(value=config.telegram.bot_token)
        ttk.Entry(self, textvariable=self._token_var, width=60).pack(padx=10, fill="x", pady=2)

        ttk.Label(self, text="Chat ID:", font=("", 10, "bold")).pack(
            padx=10, pady=(10, 0), anchor="w"
        )
        ttk.Label(self, text="目标频道/群组 ID（所有订阅共用），用 @getidsbot 查询").pack(
            padx=10, anchor="w"
        )
        self._chat_var = tk.StringVar(value=config.telegram.chat_id)
        ttk.Entry(self, textvariable=self._chat_var, width=60).pack(padx=10, fill="x", pady=2)

        ttk.Button(self, text="Save", command=self._save).pack(padx=10, pady=15, anchor="w")

    def _save(self) -> None:
        self._config.telegram.bot_token = self._token_var.get().strip()
        self._config.telegram.chat_id = self._chat_var.get().strip()
        save_config(self._config)
        messagebox.showinfo("Saved", "Telegram settings saved.")


class StorageTab(ttk.Frame):
    """Storage settings tab."""

    def __init__(self, parent: ttk.Notebook, config: AppConfig) -> None:
        super().__init__(parent)
        self._config = config

        ttk.Label(self, text="Cache Directory:", font=("", 10, "bold")).pack(
            padx=10, pady=(15, 0), anchor="w"
        )
        self._cache_var = tk.StringVar(value=config.storage.cache_dir)
        ttk.Entry(self, textvariable=self._cache_var, width=60).pack(padx=10, fill="x", pady=2)

        ttk.Label(self, text="Database Path:", font=("", 10, "bold")).pack(
            padx=10, pady=(10, 0), anchor="w"
        )
        self._db_var = tk.StringVar(value=config.storage.db_path)
        ttk.Entry(self, textvariable=self._db_var, width=60).pack(padx=10, fill="x", pady=2)

        ttk.Label(self, text="Cache TTL (days, -1=永不删除):", font=("", 10, "bold")).pack(
            padx=10, pady=(10, 0), anchor="w"
        )
        self._ttl_var = tk.StringVar(value=str(config.storage.cache_ttl_days))
        ttk.Entry(self, textvariable=self._ttl_var, width=15).pack(padx=10, anchor="w", pady=2)

        ttk.Button(self, text="Save", command=self._save).pack(padx=10, pady=15, anchor="w")

    def _save(self) -> None:
        self._config.storage.cache_dir = self._cache_var.get().strip()
        self._config.storage.db_path = self._db_var.get().strip()
        try:
            self._config.storage.cache_ttl_days = int(self._ttl_var.get().strip())
        except ValueError:
            messagebox.showerror("Error", "TTL must be a number.")
            return
        save_config(self._config)
        messagebox.showinfo("Saved", "Storage settings saved.")


class SchedulerTab(ttk.Frame):
    """Scheduler settings tab."""

    def __init__(self, parent: ttk.Notebook, config: AppConfig) -> None:
        super().__init__(parent)
        self._config = config

        ttk.Label(self, text="Loop Interval (seconds):", font=("", 10, "bold")).pack(
            padx=10, pady=(15, 0), anchor="w"
        )
        ttk.Label(self, text="主循环轮询间隔，默认 120 秒").pack(padx=10, anchor="w")
        self._interval_var = tk.StringVar(value=str(config.scheduler.loop_interval_seconds))
        ttk.Entry(self, textvariable=self._interval_var, width=15).pack(padx=10, anchor="w", pady=2)

        ttk.Button(self, text="Save", command=self._save).pack(padx=10, pady=15, anchor="w")

    def _save(self) -> None:
        try:
            self._config.scheduler.loop_interval_seconds = int(self._interval_var.get().strip())
        except ValueError:
            messagebox.showerror("Error", "Interval must be a number.")
            return
        save_config(self._config)
        messagebox.showinfo("Saved", "Scheduler settings saved.")


class FetcherTab(ttk.Frame):
    """Fetcher backend settings tab."""

    def __init__(self, parent: ttk.Notebook, config: AppConfig) -> None:
        super().__init__(parent)
        self._config = config

        frame = ttk.Frame(self)
        frame.pack(padx=10, fill="x", pady=10)

        ttk.Label(frame, text="Nitter + fxTwitter", font=("", 10, "bold")).pack(anchor="w")
        ttk.Label(
            frame,
            text="Nitter RSS 发现新帖 → fxTwitter API 解析媒体，无需登录 X",
            font=("", 8),
        ).pack(anchor="w")

        ttk.Label(frame, text="Nitter 实例 URL:").pack(pady=(8, 0), anchor="w")
        self._nitter_var = tk.StringVar(value=config.fetcher.nitter_instance)
        ttk.Entry(frame, textvariable=self._nitter_var, width=45).pack(fill="x")

        ttk.Button(self, text="Save", command=self._save).pack(padx=10, pady=15, anchor="w")

    def _save(self) -> None:
        self._config.fetcher.nitter_instance = self._nitter_var.get().strip()
        save_config(self._config)
        messagebox.showinfo("Saved", "Fetcher settings saved.")


# =============================================================================
# Main App
# =============================================================================


class AdminApp:
    """Main admin application window with tabbed interface."""

    def __init__(self, config: AppConfig, db: Database) -> None:
        self._config = config
        self._db = db
        self.root = tk.Tk()
        self.root.title("x-monitor-bot — Manager")
        self.root.geometry("680x520")
        self.root.minsize(550, 400)

        self._build_ui()
        self.root.mainloop()

    def _build_ui(self) -> None:
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=5, pady=5)

        notebook.add(SubscriptionsTab(notebook, self._db), text="Subscriptions")
        notebook.add(TelegramTab(notebook, self._config), text="Telegram")
        notebook.add(StorageTab(notebook, self._config), text="Storage")
        notebook.add(FetcherTab(notebook, self._config), text="Fetcher")
        notebook.add(SchedulerTab(notebook, self._config), text="Scheduler")


def main() -> None:
    """Launch the admin GUI."""
    config = _ensure_config()
    if config is None:
        return
    db = Database(config.storage.db_path)
    _asyncio.run(db.init())
    try:
        AdminApp(config, db)
    finally:
        _asyncio.run(db.close())


if __name__ == "__main__":
    main()
