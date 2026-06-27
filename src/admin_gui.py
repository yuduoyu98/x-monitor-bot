"""Tkinter 管理 GUI:订阅分组管理 + 配置弹窗。

独立于 bot 运行:
    python -m src.admin_gui

DB 操作用一个持久后台 event loop(避免 asyncio.run 每次 new loop 导致 aiosqlite 连接失效)。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import queue
import sys
import threading
import tkinter as tk
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from tkinter import messagebox, ttk
from tkinter.font import Font

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from src import CN_TZ, setup_logging
from src.config import AppConfig, TokenEntry, load_config, save_config
from src.database import Database

logger = logging.getLogger(__name__)

DB_PATH = "state.db"
CONFIG_PATH = "config.yaml"

_POLL_UNITS = ["分钟", "小时", "天", "周"]
_POLL_MULTIPLIERS = {"分钟": 60, "小时": 3600, "天": 86400, "周": 604800}


# ─── 持久 event loop(解决 aiosqlite 跨 loop 问题)─────────────────────────────


class _LoopThread:
    """后台 event loop,所有 DB 操作 submit 到这里(同 loop → 连接不失效)。"""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    def call(self, coro, timeout=None):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=timeout)

    def spawn(self, coro):
        """提交协程到后台 loop,不阻塞(手动采集等耗时任务;完成后自行 root.after 更新 UI)。"""
        asyncio.run_coroutine_threadsafe(coro, self._loop)

    def call_soon(self, func):
        """跨线程往后台 loop 投递同步回调(如停止追踪时 set event),线程安全。"""
        self._loop.call_soon_threadsafe(func)


_loop = _LoopThread()


def _db_call(coro):
    """同步调用 async DB 方法(提交到后台 loop)。"""
    return _loop.call(coro)


# ─── helpers ───────────────────────────────────────────────────────────────────


def _seconds_to_pair(seconds: int) -> tuple[int, str]:
    for unit in reversed(_POLL_UNITS):
        m = _POLL_MULTIPLIERS[unit]
        if seconds % m == 0:
            return seconds // m, unit
    return seconds, "分钟"


def _style_tree(tree: ttk.Treeview) -> None:
    f = Font(font="TkDefaultFont")
    f.configure(size=10)
    tree.tag_configure("group", font=(f.cget("family"), 10, "bold"))
    tree.tag_configure("subgroup", font=(f.cget("family"), 10))  # 小组不加粗
    tree.tag_configure("off", foreground="#999")


def _is_group(tags: tuple) -> bool:
    """是否分组节点(顶级 group 或小组 subgroup)。"""
    return "group" in tags or "subgroup" in tags


class _TkLogHandler(logging.Handler):
    """把日志记录塞进队列,由 GUI 主线程定期抽到日志窗口(跨线程安全)。"""

    def __init__(self, q: queue.Queue[str]) -> None:
        super().__init__()
        self._q = q
        self.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%H:%M:%S")
        )

    def emit(self, record: logging.LogRecord) -> None:
        with contextlib.suppress(Exception):
            self._q.put_nowait(self.format(record))


# ─── Tooltip + helpers ────────────────────────────────────────────────────────


class _Tooltip:
    """悬浮提示:鼠标进入 widget 显示,离开销毁。"""

    def __init__(self, widget: tk.Widget, text: str) -> None:
        self._widget = widget
        self._text = text
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)

    def _show(self, _event=None) -> None:
        if self._tip:
            return
        x = self._widget.winfo_rootx() + 20
        y = self._widget.winfo_rooty() + self._widget.winfo_height() + 4
        self._tip = tk.Toplevel(self._widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{x}+{y}")
        ttk.Label(
            self._tip,
            text=self._text,
            background="#ffffe0",
            relief="solid",
            borderwidth=1,
            padding=(6, 3),
        ).pack()

    def _hide(self, _event=None) -> None:
        if self._tip:
            self._tip.destroy()
            self._tip = None


def _add_label_entry(
    frame: ttk.Frame,
    row: int,
    label: str,
    var: tk.StringVar,
    *,
    width: int | None = None,
    tooltip: str = "",
    prefix: str = "",
) -> ttk.Entry:
    """一行:label + 可选 prefix(@) + entry + 可选 tooltip(?)。返回 entry。"""
    ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=8, pady=4)
    inner = ttk.Frame(frame)
    inner.grid(row=row, column=1, sticky="ew", padx=4, pady=4)
    entry = ttk.Entry(inner, textvariable=var, width=width or 30)
    if prefix:
        ttk.Label(inner, text=prefix).pack(side="left", padx=(0, 1))
    entry.pack(side="left", fill="x", expand=True)
    if tooltip:
        q = ttk.Label(inner, text="?", foreground="#0066cc", cursor="question_arrow")
        q.pack(side="left", padx=(4, 0))
        _Tooltip(q, tooltip)
    return entry


# ─── SubscriptionDialog ────────────────────────────────────────────────────────


class SubscriptionDialog(tk.Toplevel):
    def __init__(self, parent, db: Database, data: dict | None = None) -> None:
        super().__init__(parent)
        is_edit = data is not None
        self.title("编辑订阅" if is_edit else "添加订阅")
        self.transient(parent)
        self.grab_set()
        self._db = db
        self.result: dict | None = None

        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        row = 0

        # ID — label "ID" + @ prefix
        self._acct = tk.StringVar(value=data["account_id"] if data else "")
        acct_entry = _add_label_entry(frame, row, "ID", self._acct, prefix="@")
        if is_edit:
            acct_entry.config(state="readonly")
        row += 1

        # 分组
        ttk.Label(frame, text="分组").grid(row=row, column=0, sticky="w", padx=8, pady=4)
        groups = [g["name"] for g in _db_call(db.get_groups())]
        cur_group = data.get("group_name") if data else None
        self._group = tk.StringVar(value=cur_group if cur_group else "(未分组)")
        ttk.Combobox(
            frame, textvariable=self._group, values=["(未分组)"] + groups, state="readonly"
        ).grid(row=row, column=1, sticky="ew", padx=4, pady=4)
        row += 1

        # 备注
        self._remark = tk.StringVar(value=data.get("remark", "") if data else "")
        _add_label_entry(frame, row, "备注", self._remark)
        row += 1

        # 模式
        ttk.Label(frame, text="模式").grid(row=row, column=0, sticky="w", padx=8, pady=4)
        self._mode = tk.StringVar(
            value=data.get("sync_mode", "media_only") if data else "media_only"
        )
        ttk.Combobox(
            frame, textvariable=self._mode, values=["media_only", "all"], state="readonly"
        ).grid(row=row, column=1, sticky="w", padx=4, pady=4)
        row += 1

        # 轮询(数字 + 单位)
        ttk.Label(frame, text="轮询").grid(row=row, column=0, sticky="w", padx=8, pady=4)
        poll_inner = ttk.Frame(frame)
        poll_inner.grid(row=row, column=1, sticky="w", padx=4, pady=4)
        raw_poll = data.get("poll_interval", 86400) if data else 86400
        val, unit = _seconds_to_pair(raw_poll)
        self._poll_val = tk.StringVar(value=str(val))
        self._poll_unit = tk.StringVar(value=unit)
        ttk.Entry(poll_inner, textvariable=self._poll_val, width=6).pack(side="left", padx=(0, 4))
        ttk.Combobox(
            poll_inner,
            textvariable=self._poll_unit,
            values=_POLL_UNITS,
            state="readonly",
            width=6,
        ).pack(side="left")
        row += 1

        # 批大小(entry + ? 悬浮说明)
        self._flimit = tk.StringVar(value=str(data.get("fetch_limit", 5)) if data else "5")
        _add_label_entry(
            frame,
            row,
            "批大小",
            self._flimit,
            width=10,
            tooltip="每次从 X 取几条来比对 watermark。默认 5;取太大(>5)易被 X 限流返回空。",
        )
        row += 1

        # skip_retweets
        self._skip_rt = tk.BooleanVar(value=bool(data.get("skip_retweets", 1)) if data else True)
        ttk.Checkbutton(frame, text="跳过转推/引用", variable=self._skip_rt).grid(
            row=row, column=0, columnspan=2, sticky="w", padx=8, pady=4
        )
        row += 1

        # 水位线(6 框:年月日时分秒,北京时间;默认当前时间。改过去=从该点回灌/重采;随时可改)
        if data and data.get("watermark"):
            _dt = datetime.fromisoformat(data["watermark"]).astimezone(CN_TZ)
        else:
            _dt = datetime.now(CN_TZ)
        self._wm_vars = [
            tk.StringVar(value=f"{_dt:%Y}"),
            tk.StringVar(value=f"{_dt:%m}"),
            tk.StringVar(value=f"{_dt:%d}"),
            tk.StringVar(value=f"{_dt:%H}"),
            tk.StringVar(value=f"{_dt:%M}"),
            tk.StringVar(value=f"{_dt:%S}"),
        ]
        ttk.Label(frame, text="水位线").grid(row=row, column=0, sticky="w", padx=8, pady=4)
        wm_inner = ttk.Frame(frame)
        wm_inner.grid(row=row, column=1, sticky="w", padx=4, pady=4)
        for var, unit, width in zip(
            self._wm_vars, ("年", "月", "日", "时", "分", "秒"), (5, 3, 3, 3, 3, 3), strict=True
        ):
            ttk.Entry(wm_inner, textvariable=var, width=width).pack(side="left")
            ttk.Label(wm_inner, text=unit).pack(side="left", padx=(1, 4))
        q = ttk.Label(wm_inner, text="?", foreground="#0066cc", cursor="question_arrow")
        q.pack(side="left", padx=(4, 0))
        _Tooltip(
            q,
            "北京时间(东八区)。默认当前时间=不回灌;改成过去时间=从该点回灌/重采。保存即写入水位线,后续采集从它开始。",
        )
        row += 1

        # 按钮(右对齐,保存在取消左边)
        btn = ttk.Frame(self, padding=(16, 0, 16, 16))
        btn.pack(fill="x")
        ttk.Button(btn, text="取消", command=self.destroy).pack(side="right", padx=4)
        ttk.Button(btn, text="保存", command=self._save).pack(side="right", padx=4)

        self.update_idletasks()
        w = max(self.winfo_reqwidth(), 380)
        h = self.winfo_reqheight()
        self.geometry(f"{w}x{h}")
        self.bind("<Escape>", lambda _: self.destroy())

    def _save(self) -> None:
        acct = self._acct.get().strip().lstrip("@")
        if not acct:
            messagebox.showerror("错误", "Account 不能为空", parent=self)
            return
        grp = self._group.get()
        if grp == "(未分组)":
            grp = None
        try:
            val = int(self._poll_val.get().strip() or "5")
            unit = self._poll_unit.get()
            poll = val * _POLL_MULTIPLIERS.get(unit, 60)
            flimit = int(self._flimit.get().strip() or "5")
            y, mo, d, h, mi, se = (int(v.get().strip()) for v in self._wm_vars)
            wm_iso = datetime(y, mo, d, h, mi, se, tzinfo=CN_TZ).astimezone(UTC).isoformat()
        except ValueError:
            messagebox.showerror(
                "错误",
                "轮询 / 批大小 必须是数字;水位线 6 个框需为有效日期时间",
                parent=self,
            )
            return
        self.result = {
            "account_id": acct,
            "group_name": grp,
            "remark": self._remark.get().strip(),
            "sync_mode": self._mode.get(),
            "poll_interval": poll,
            "fetch_limit": flimit,
            "skip_retweets": self._skip_rt.get(),
            "watermark": wm_iso,
        }
        self.destroy()


# ─── GroupDialog ──────────────────────────────────────────────────────────────


class GroupDialog(tk.Toplevel):
    def __init__(self, parent, title="添加分组", default="") -> None:
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.grab_set()
        self.dlg_result: str | None = None

        f = ttk.Frame(self, padding=16)
        f.pack()
        ttk.Label(f, text="分组名:").pack(anchor="w", pady=(0, 4))
        self._entry_var = tk.StringVar(value=default)
        ttk.Entry(f, textvariable=self._entry_var, width=28).pack()
        b = ttk.Frame(f)
        b.pack(fill="x", pady=10)
        ttk.Button(b, text="取消", command=self.destroy).pack(side="right", padx=4)
        ttk.Button(b, text="确定", command=self._ok).pack(side="right", padx=4)
        self.bind("<Return>", lambda _: self._ok())
        self.bind("<Escape>", lambda _: self.destroy())

    def _ok(self) -> None:
        name = self._entry_var.get().strip()
        if name:
            self.dlg_result = name
            self.destroy()


# ─── LinkSendDialog ───────────────────────────────────────────────────────────


class LinkSendDialog(tk.Toplevel):
    """链接 → TG:输入推文链接,确认后把 URL 放到 .url(取消/空 → None)。"""

    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.title("链接 → TG")
        self.transient(parent)
        self.grab_set()
        self.url: str | None = None
        f = ttk.Frame(self, padding=16)
        f.pack()
        ttk.Label(f, text="推文链接(https://x.com/…/status/…):").pack(anchor="w", pady=(0, 4))
        self._var = tk.StringVar()
        entry = ttk.Entry(f, textvariable=self._var, width=46)
        entry.pack()
        entry.focus_set()
        b = ttk.Frame(f)
        b.pack(fill="x", pady=10)
        ttk.Button(b, text="取消", command=self.destroy).pack(side="right", padx=4)
        ttk.Button(b, text="发送", command=self._ok).pack(side="right", padx=4)
        self.bind("<Return>", lambda _: self._ok())
        self.bind("<Escape>", lambda _: self.destroy())

    def _ok(self) -> None:
        u = self._var.get().strip()
        if u:
            self.url = u
            self.destroy()


# ─── TokenDialog ──────────────────────────────────────────────────────────────


class TokenDialog(tk.Toplevel):
    """新增/编辑一个 auth token(备注 + token);确认后结果放 .result,取消 → None。"""

    def __init__(self, parent, title="新增 token", label="", token="") -> None:
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.grab_set()
        self.result: TokenEntry | None = None
        f = ttk.Frame(self, padding=16)
        f.pack()
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="备注:").grid(row=0, column=0, sticky="w", pady=3)
        self._label = tk.StringVar(value=label)
        ttk.Entry(f, textvariable=self._label, width=36).grid(row=0, column=1, pady=3)
        ttk.Label(f, text="Token:").grid(row=1, column=0, sticky="w", pady=3)
        self._token = tk.StringVar(value=token)
        ttk.Entry(f, textvariable=self._token, width=36).grid(row=1, column=1, pady=3)
        b = ttk.Frame(f)
        b.grid(row=2, column=0, columnspan=2, pady=(10, 0))
        ttk.Button(b, text="取消", command=self.destroy).pack(side="right", padx=4)
        ttk.Button(b, text="确定", command=self._ok).pack(side="right", padx=4)
        self.bind("<Return>", lambda _: self._ok())
        self.bind("<Escape>", lambda _: self.destroy())

    def _ok(self) -> None:
        tok = self._token.get().strip()
        if not tok:
            messagebox.showerror("错误", "Token 不能为空", parent=self)
            return
        self.result = TokenEntry(label=self._label.get().strip(), token=tok)
        self.destroy()


# ─── NewSubgroupDialog ────────────────────────────────────────────────────────


class NewSubgroupDialog(tk.Toplevel):
    """批量「添加小组」:选父分组(顶级)+ 填小组名;.result=(parent, name),取消 → None。"""

    def __init__(self, parent, top_groups: list[str], default_parent: str | None = None) -> None:
        super().__init__(parent)
        self.title("添加小组")
        self.transient(parent)
        self.grab_set()
        self.result: tuple[str, str] | None = None
        f = ttk.Frame(self, padding=16)
        f.pack()
        ttk.Label(f, text="父分组:").grid(row=0, column=0, sticky="w", pady=3)
        init = (
            default_parent
            if default_parent in top_groups
            else (top_groups[0] if top_groups else "")
        )
        self._parent = tk.StringVar(value=init)
        ttk.Combobox(
            f, textvariable=self._parent, values=top_groups, state="readonly", width=24
        ).grid(row=0, column=1, pady=3)
        ttk.Label(f, text="小组名:").grid(row=1, column=0, sticky="w", pady=3)
        self._sub_name = tk.StringVar()
        name_entry = ttk.Entry(f, textvariable=self._sub_name, width=26)
        name_entry.grid(row=1, column=1, pady=3)
        name_entry.focus_set()
        b = ttk.Frame(f)
        b.grid(row=2, column=0, columnspan=2, pady=(10, 0))
        ttk.Button(b, text="取消", command=self.destroy).pack(side="right", padx=4)
        ttk.Button(b, text="确定", command=self._ok).pack(side="right", padx=4)
        self.bind("<Return>", lambda _: self._ok())
        self.bind("<Escape>", lambda _: self.destroy())

    def _ok(self) -> None:
        parent = self._parent.get().strip()
        name = self._sub_name.get().strip()
        if not parent:
            messagebox.showerror("错误", "请选择父分组", parent=self)
            return
        if not name:
            messagebox.showerror("错误", "小组名不能为空", parent=self)
            return
        self.result = (parent, name)
        self.destroy()


# ─── ConfigDialog ────────────────────────────────────────────────────────────


class ConfigDialog(tk.Toplevel):
    """配置弹窗,按数据流向排序:Source → Sink → Sync → Storage。

    Source tab: Scweet(proxy + auth 说明)+ Nitter(实例 URL),各可勾选启用。
    Sink tab: Telegram(bot_token + chat_id),可勾选启用。
    """

    def __init__(self, parent, config: AppConfig) -> None:
        super().__init__(parent)
        self.title("⚙ 配置")
        self.geometry("540x440")
        self.transient(parent)
        self.grab_set()
        self._config = config
        self._vars: dict[str, tk.StringVar] = {}
        self._entries: dict[str, ttk.Entry] = {}

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=8)

        self._build_source_tab(nb)
        self._build_sink_tab(nb)
        self._build_sync_tab(nb)
        self._build_storage_tab(nb)

        # 按钮(右对齐,和 SubscriptionDialog / GroupDialog 统一)
        btn = ttk.Frame(self, padding=(8, 0, 8, 8))
        btn.pack(fill="x")
        ttk.Button(btn, text="保存", command=self._save).pack(side="right", padx=4)

    def _add_field(self, parent, label, key, value, width=36, row=0, tooltip=""):
        """在 parent 里加一行:label + entry + 可选 ? tooltip。返回 (entry, next_row)。"""
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=8, pady=3)
        inner = ttk.Frame(parent)
        inner.grid(row=row, column=1, sticky="ew", padx=4, pady=3)
        v = tk.StringVar(value=str(value))
        self._vars[key] = v
        entry = ttk.Entry(inner, textvariable=v, width=width)
        entry.pack(side="left", fill="x", expand=True)
        self._entries[key] = entry
        if tooltip:
            q = ttk.Label(inner, text="?", foreground="#0066cc", cursor="question_arrow")
            q.pack(side="left", padx=(4, 0))
            _Tooltip(q, tooltip)
        return entry, row + 1

    def _build_source_tab(self, nb: ttk.Notebook) -> None:
        tab = ttk.Frame(nb)
        nb.add(tab, text="Source")
        tab.columnconfigure(1, weight=1)
        row = 0

        # 单选:选哪个 source
        self._source_type = tk.StringVar(value=self._config.source_type)
        radio_frame = ttk.Frame(tab)
        radio_frame.grid(row=row, column=0, columnspan=2, sticky="w", padx=8, pady=(8, 4))
        ttk.Radiobutton(
            radio_frame,
            text="Scweet(逆向)",
            variable=self._source_type,
            value="scweet",
        ).pack(side="left", padx=(0, 12))
        ttk.Radiobutton(
            radio_frame,
            text="Nitter(回溯推文条数有限制)",
            variable=self._source_type,
            value="nitter",
        ).pack(side="left")
        row += 1

        ttk.Separator(tab, orient="horizontal").grid(
            row=row, column=0, columnspan=2, sticky="ew", padx=8, pady=4
        )
        row += 1

        # ── Scweet 配置 ──
        ttk.Label(tab, text="Scweet", font=("", 10, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", padx=8
        )
        row += 1

        # proxy 默认填系统代理
        if not self._config.scweet.proxy:
            try:
                import urllib.request

                self._config.scweet.proxy = urllib.request.getproxies().get("https", "")
            except Exception:
                pass

        # Auth Token:列表 + 新增/编辑/删除/设为当前(备注在新增/编辑弹窗里填)
        label_inner = ttk.Frame(tab)
        label_inner.grid(row=row, column=0, sticky="nw", padx=8, pady=3)
        ttk.Label(label_inner, text="Auth Token:").pack(side="left")
        q = ttk.Label(label_inner, text="?", foreground="#0066cc", cursor="question_arrow")
        q.pack(side="left", padx=(2, 0))
        _Tooltip(
            q,
            "专用 X 账号的 auth_token cookie(x.com → F12 → Application → Cookies → auth_token)。"
            "新增/编辑时填备注(如「主号」)方便辨认;● 标记当前生效的 token。",
        )
        auth_frame = ttk.Frame(tab)
        auth_frame.grid(row=row, column=1, sticky="ew", padx=4, pady=3)
        self._token_tree = ttk.Treeview(
            auth_frame, columns=("label", "head"), show="headings", height=4, selectmode="browse"
        )
        self._token_tree.heading("label", text="备注")
        self._token_tree.heading("head", text="token")
        self._token_tree.column("label", width=120, anchor="w")
        self._token_tree.column("head", width=190, anchor="w")
        self._token_tree.pack(side="left", fill="both", expand=True)
        btn_col = ttk.Frame(auth_frame)
        btn_col.pack(side="right", padx=(8, 0))
        for _text, _cmd in (
            ("新增", self._token_add),
            ("编辑", self._token_edit),
            ("删除", self._token_delete),
            ("设为当前", self._token_set_active),
        ):
            ttk.Button(btn_col, text=_text, command=_cmd).pack(fill="x", pady=1)
        # 初始化:确保当前 auth_token 在列表里
        self._tokens: list[TokenEntry] = list(self._config.scweet.token_history)
        _active = self._config.scweet.auth_token
        if _active and _active not in {e.token for e in self._tokens}:
            self._tokens.insert(0, TokenEntry(label="", token=_active))
        self._active_token = _active
        self._refresh_token_tree()
        row += 1
        _, row = self._add_field(
            tab,
            "Proxy:",
            "scweet_proxy",
            self._config.scweet.proxy,
            width=40,
            row=row,
            tooltip="HTTP 代理 URL(国内必须)。如 Clash: http://127.0.0.1:7890",
        )

        ttk.Separator(tab, orient="horizontal").grid(
            row=row, column=0, columnspan=2, sticky="ew", padx=8, pady=4
        )
        row += 1

        # ── Nitter 配置 ──
        ttk.Label(tab, text="Nitter", font=("", 10, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", padx=8
        )
        row += 1
        self._add_field(
            tab,
            "Nitter 实例:",
            "nitter_instance",
            self._config.fetcher.nitter_instance,
            width=40,
            row=row,
            tooltip="Nitter RSS 实例 URL。默认 nitter.net;实例不稳定时可换。",
        )

    def _build_sink_tab(self, nb: ttk.Notebook) -> None:
        tab = ttk.Frame(nb)
        nb.add(tab, text="Sink")
        tab.columnconfigure(1, weight=1)
        row = 0

        ttk.Label(tab, text="Telegram", font=("", 10, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", padx=8, pady=(8, 0)
        )
        row += 1
        c = self._config.telegram
        _, row = self._add_field(
            tab,
            "Bot Token:",
            "tg_token",
            c.bot_token,
            width=40,
            row=row,
            tooltip="@BotFather 创建的 Bot Token。",
        )
        self._add_field(
            tab,
            "Chat ID:",
            "tg_chat",
            c.chat_id,
            width=40,
            row=row,
            tooltip="目标频道/群组 ID。频道发条消息 → 转发给 @getidsbot 查询。",
        )

    def _build_sync_tab(self, nb: ttk.Notebook) -> None:
        tab = ttk.Frame(nb)
        nb.add(tab, text="Sync")
        tab.columnconfigure(1, weight=1)
        row = 0

        ttk.Label(tab, text="循环间隔").grid(row=row, column=0, sticky="w", padx=8, pady=4)
        interval_inner = ttk.Frame(tab)
        interval_inner.grid(row=row, column=1, sticky="w", padx=4, pady=4)
        raw = self._config.scheduler.loop_interval_seconds
        val, unit = _seconds_to_pair(raw)
        self._interval_val = tk.StringVar(value=str(val))
        self._interval_unit = tk.StringVar(value=unit)
        ttk.Entry(interval_inner, textvariable=self._interval_val, width=6).pack(
            side="left", padx=(0, 4)
        )
        ttk.Combobox(
            interval_inner,
            textvariable=self._interval_unit,
            values=_POLL_UNITS,
            state="readonly",
            width=6,
        ).pack(side="left")
        q = ttk.Label(interval_inner, text="?", foreground="#0066cc", cursor="question_arrow")
        q.pack(side="left", padx=(4, 0))
        _Tooltip(q, "主循环多久醒一次检查各订阅是否到轮询时间。是粒度,不是每账号的实际间隔。")

    def _build_storage_tab(self, nb: ttk.Notebook) -> None:
        tab = ttk.Frame(nb)
        nb.add(tab, text="Storage")
        tab.columnconfigure(1, weight=1)
        _, row = self._add_field(
            tab, "Cache 目录:", "cache_dir", self._config.storage.cache_dir, row=0
        )
        _, row = self._add_field(tab, "DB 路径:", "db_path", self._config.storage.db_path, row=row)
        self._add_field(
            tab,
            "Cache TTL(天,-1=永不):",
            "ttl",
            self._config.storage.cache_ttl_days,
            width=10,
            row=row,
        )

    # ── Auth Token 列表:新增/编辑/删除/设为当前 ──

    def _refresh_token_tree(self) -> None:
        for i in self._token_tree.get_children():
            self._token_tree.delete(i)
        for idx, e in enumerate(self._tokens):
            mark = "● " if e.token == self._active_token else "  "
            label = mark + (e.label or "(未命名)")
            head = e.token[:8] if e.token else ""
            self._token_tree.insert("", "end", iid=str(idx), values=(label, head))

    def _token_add(self) -> None:
        dlg = TokenDialog(self, title="新增 token")
        self.wait_window(dlg)
        if dlg.result is None:
            return
        for e in self._tokens:  # 同 token → 只改备注
            if e.token == dlg.result.token:
                e.label = dlg.result.label
                break
        else:
            self._tokens.append(dlg.result)
        self._active_token = dlg.result.token  # 新增即设为当前
        self._refresh_token_tree()

    def _token_edit(self) -> None:
        sel = self._token_tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        e = self._tokens[idx]
        dlg = TokenDialog(self, title="编辑 token", label=e.label, token=e.token)
        self.wait_window(dlg)
        if dlg.result is None:
            return
        was_active = e.token == self._active_token
        self._tokens[idx] = dlg.result
        if was_active:
            self._active_token = dlg.result.token
        self._refresh_token_tree()

    def _token_delete(self) -> None:
        sel = self._token_tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        e = self._tokens[idx]
        if not messagebox.askyesno(
            "确认", f"删除 token「{e.label or e.token[:8] + '…'}」?", parent=self
        ):
            return
        del self._tokens[idx]
        if e.token == self._active_token:
            self._active_token = self._tokens[0].token if self._tokens else ""
        self._refresh_token_tree()

    def _token_set_active(self) -> None:
        sel = self._token_tree.selection()
        if not sel:
            return
        self._active_token = self._tokens[int(sel[0])].token
        self._refresh_token_tree()

    def _save(self) -> None:
        c = self._config
        c.telegram.bot_token = self._vars["tg_token"].get().strip()
        c.telegram.chat_id = self._vars["tg_chat"].get().strip()
        c.fetcher.nitter_instance = self._vars["nitter_instance"].get().strip()
        c.scweet.auth_token = self._active_token
        c.scweet.token_history = list(self._tokens)
        c.scweet.proxy = self._vars["scweet_proxy"].get().strip()
        c.source_type = self._source_type.get()
        c.scheduler.loop_interval_seconds = int(
            self._interval_val.get().strip() or "5"
        ) * _POLL_MULTIPLIERS.get(self._interval_unit.get(), 60)
        c.storage.cache_dir = self._vars["cache_dir"].get().strip()
        c.storage.db_path = self._vars["db_path"].get().strip()
        c.storage.cache_ttl_days = int(self._vars["ttl"].get().strip() or "-1")
        save_config(c, CONFIG_PATH)
        messagebox.showinfo("已保存", "配置已写入 config.yaml", parent=self)
        self.destroy()


# ─── main app ─────────────────────────────────────────────────────────────────


class AdminApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("X订阅管理")
        self.root.geometry("780x500")
        self.root.minsize(650, 350)
        # 日志窗口:队列(线程间)+ 历史;先挂 handler 再 init DB,捕获启动日志
        self._log_q: queue.Queue[str] = queue.Queue()
        self._log_lines: deque[str] = deque(maxlen=2000)
        self._log_text: tk.Text | None = None
        self._log_win: tk.Toplevel | None = None
        logging.getLogger().addHandler(_TkLogHandler(self._log_q))
        self._db = Database(DB_PATH)
        _db_call(self._db.init())
        self._pipeline = None
        self._sink = None
        self._tracking = False
        self._track_stop: asyncio.Event | None = None
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._refresh()
        self.root.after(300, self._drain_logs)

    def _build_ui(self) -> None:
        bar = ttk.Frame(self.root)
        bar.pack(fill="x", padx=6, pady=4)
        ttk.Button(bar, text="+ 订阅", command=self._add_sub).pack(side="left", padx=2)
        ttk.Button(bar, text="+ 分组", command=self._add_group).pack(side="left", padx=2)
        ttk.Button(bar, text="⚙ 配置", command=self._config).pack(side="left", padx=2)
        ttk.Button(bar, text="🔗 链接发送", command=self._link_send).pack(side="left", padx=2)
        self._track_btn = ttk.Button(bar, text="▶ 开始追踪", command=self._toggle_tracking)
        self._track_btn.pack(side="right", padx=2)
        ttk.Button(bar, text="📋 日志", command=self._show_logs).pack(side="right", padx=2)
        # 搜索框:按 account_id 或 备注 过滤下方列表
        search_row = ttk.Frame(self.root)
        search_row.pack(fill="x", padx=6)
        ttk.Label(search_row, text="查找:").pack(side="left")
        self._search = tk.StringVar()
        search_entry = ttk.Entry(search_row, textvariable=self._search)
        search_entry.pack(side="left", fill="x", expand=True, padx=(4, 6))
        search_entry.bind("<KeyRelease>", lambda _: self._refresh())
        ttk.Separator(self.root, orient="horizontal").pack(fill="x")

        cols = ("mode", "poll", "watermark", "status")
        self.tree = ttk.Treeview(
            self.root, columns=cols, show="tree headings", selectmode="extended"
        )
        self.tree.heading("#0", text="分组 / 订阅")
        self.tree.heading("mode", text="模式")
        self.tree.heading("poll", text="间隔")
        self.tree.heading("watermark", text="水位线")
        self.tree.heading("status", text="状态")
        self.tree.column("#0", width=240)
        self.tree.column("mode", width=80)
        self.tree.column("poll", width=70)
        self.tree.column("watermark", width=150)
        self.tree.column("status", width=55)
        bottom = ttk.Frame(self.root)
        bottom.pack(side="bottom", fill="x")
        self._status = tk.Label(bottom, text="就绪", anchor="w", relief="sunken", padx=6)
        self._status.pack(side="left", fill="x", expand=True)
        self._progress = ttk.Progressbar(bottom, mode="indeterminate", length=120)
        self._progress.pack(side="right", padx=4, pady=2)
        self.tree.pack(fill="both", expand=True, padx=6, pady=4)
        _style_tree(self.tree)

        self.tree.bind("<Double-1>", lambda _: self._on_double())
        self.tree.bind("<Button-3>", self._on_right_click)

        # 订阅右键菜单
        self._ctx = tk.Menu(self.root, tearoff=0)
        self._ctx.add_command(label="立即采集", command=self._collect_now)
        self._ctx.add_command(label="查看 dead_letter", command=self._show_dead_letter)
        self._ctx.add_separator()
        self._ctx.add_command(label="编辑", command=self._edit_sub)
        self._move_menu = tk.Menu(self._ctx, tearoff=0)  # 「移动到」:右键时动态填充分组
        self._ctx.add_cascade(label="移动到", menu=self._move_menu)
        self._ctx.add_command(label="切换开关", command=self._toggle_sub)
        self._ctx.add_command(label="删除", command=self._delete_sub)
        # 分组右键菜单(「添加小组」仅顶级分组可用,右键时按层级开关)
        self._group_ctx = tk.Menu(self.root, tearoff=0)
        self._group_ctx.add_command(label="添加小组", command=self._add_subgroup)
        self._group_ctx.add_command(label="切换开关", command=self._toggle_group)
        self._group_ctx.add_separator()
        self._group_ctx.add_command(label="重命名", command=self._rename_group)
        self._group_ctx.add_command(label="删除", command=self._delete_group)
        # 多选账号的批量菜单
        self._bulk_ctx = tk.Menu(self.root, tearoff=0)
        self._bulk_move_menu = tk.Menu(self._bulk_ctx, tearoff=0)
        self._bulk_ctx.add_cascade(label="移动到", menu=self._bulk_move_menu)
        self._bulk_ctx.add_command(label="添加小组…", command=self._bulk_new_subgroup)

    def _refresh(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)

        kw = self._search.get().strip().lower()
        subs = _db_call(self._db.get_subscriptions())
        if kw:  # 按 account_id 或 备注 过滤(大小写不敏感,子串匹配)
            subs = [
                s
                for s in subs
                if kw in (s.get("account_id") or "").lower()
                or kw in (s.get("remark") or "").lower()
            ]
        groups = _db_call(self._db.get_groups())

        by_group: dict[str | None, list] = {}
        for s in subs:
            by_group.setdefault(s.get("group_name"), []).append(s)

        # 顶级分组 → 小组 → 订阅
        for g in groups:
            if g.get("parent_name"):
                continue  # 小组在父分组节点下渲染
            gname = g["name"]
            genabled = bool(g["enabled"])
            direct = by_group.pop(gname, [])
            sub_groups = [sg for sg in groups if sg.get("parent_name") == gname]
            # 搜索:本组无直挂命中 且 无命中小组 → 跳过
            if kw and not direct and not [sg for sg in sub_groups if by_group.get(sg["name"])]:
                continue
            total = len(direct) + sum(len(by_group.get(sg["name"], [])) for sg in sub_groups)
            mark = "●" if genabled else "○"
            tags = () if genabled else ("off",)
            node = self.tree.insert(
                "",
                "end",
                text=f"{mark} {gname} ({total})",
                values=("", "", "", "开" if genabled else "关"),
                open=True,
                tags=("group",) + tags,
            )
            for s in direct:  # 直挂顶级分组的订阅
                self._insert_sub(node, s, group_enabled=genabled)
            for sg in sub_groups:  # 小组
                sgname = sg["name"]
                sgenabled = bool(sg["enabled"])
                sg_subs = by_group.pop(sgname, [])
                if kw and not sg_subs:
                    continue  # 搜索:空小组不显示
                sgmark = "●" if sgenabled else "○"
                sgtags = () if sgenabled else ("off",)
                sg_node = self.tree.insert(
                    node,
                    "end",
                    text=f"{sgmark} {sgname} ({len(sg_subs)})",
                    values=("", "", "", "开" if sgenabled else "关"),
                    open=True,
                    tags=("subgroup",) + sgtags,
                )
                for s in sg_subs:
                    self._insert_sub(sg_node, s, group_enabled=genabled and sgenabled)

        ungrouped = by_group.pop(None, [])
        if ungrouped:
            node = self.tree.insert(
                "",
                "end",
                text=f"○ 未分组 ({len(ungrouped)})",
                values=("", "", "", "—"),
                open=True,
                tags=("group",),
            )
            for s in ungrouped:
                self._insert_sub(node, s)

    def _insert_sub(self, parent, s: dict, group_enabled: bool = True) -> None:
        enabled = s.get("enabled", 1)
        mark = "✓" if enabled else "✗"
        grey = (not enabled) or (not group_enabled)  # 自身关 或 所属分组关 → 置灰(enabled 不变)
        tags = ("off",) if grey else ()
        label = f"  {mark} @{s['account_id']}"
        if s.get("remark"):
            label += f" ({s['remark']})"

        poll = s.get("poll_interval", 300)
        val, unit = _seconds_to_pair(poll)
        poll_str = f"{val}{unit}"

        wm = s.get("watermark")
        wm_str = "—"
        if wm:
            try:
                wm_str = datetime.fromisoformat(wm).astimezone(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                wm_str = "—"

        self.tree.insert(
            parent,
            "end",
            text=label,
            values=(s.get("sync_mode", ""), poll_str, wm_str, "开" if enabled else "关"),
            tags=tags,
            iid=s["account_id"],
        )

    # ── actions ──

    def _add_sub(self) -> None:
        dlg = SubscriptionDialog(self.root, self._db)
        self.root.wait_window(dlg)
        if not dlg.result:
            return
        r = dlg.result
        _db_call(
            self._db.upsert_subscription(
                r["account_id"],
                sync_mode=r["sync_mode"],
                remark=r["remark"],
                poll_interval=r["poll_interval"],
                fetch_limit=r["fetch_limit"],
                skip_retweets=r["skip_retweets"],
                group_name=r["group_name"],
            )
        )
        _db_call(self._db.set_watermark(r["account_id"], datetime.fromisoformat(r["watermark"])))
        self._refresh()

    def _add_group(self) -> None:
        dlg = GroupDialog(self.root)
        self.root.wait_window(dlg)
        if dlg.dlg_result:
            _db_call(self._db.upsert_group(dlg.dlg_result))
            self._refresh()

    def _config(self) -> None:
        cfg = load_config(CONFIG_PATH)
        ConfigDialog(self.root, cfg)
        self._refresh()

    def _on_double(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        if _is_group(self.tree.item(sel[0], "tags")):
            self._toggle_group()
        else:
            self._edit_sub()

    def _on_right_click(self, event) -> None:
        item = self.tree.identify_row(event.y)
        if not item:
            return
        sel = self.tree.selection()
        if item not in sel:  # 右键未选中项 → 只选它(标准多选 UX)
            self.tree.selection_clear()
            self.tree.selection_set(item)
            sel = self.tree.selection()
        if _is_group(self.tree.item(item, "tags")):
            if self._selected_group_name() is None:
                return  # 未分组:不是真实分组,无右键菜单
            self._setup_group_ctx(item)  # 顶级分组才开「添加小组」
            self._group_ctx.tk_popup(event.x_root, event.y_root)
            return
        # 账号:多选 → 批量菜单;单选 → 单个菜单
        accounts = [s for s in sel if not _is_group(self.tree.item(s, "tags"))]
        if len(accounts) > 1:
            self._rebuild_bulk_move_menu(accounts)
            self._bulk_ctx.tk_popup(event.x_root, event.y_root)
        else:
            self._rebuild_move_menu(item)  # 单个订阅:动态填充「移动到」
            self._ctx.tk_popup(event.x_root, event.y_root)

    def _setup_group_ctx(self, item) -> None:
        """「添加小组」仅对真实顶级分组启用(无父节点 且 非未分组)。"""
        is_top = not self.tree.parent(item) and self._selected_group_name() is not None
        self._group_ctx.entryconfig("添加小组", state="normal" if is_top else "disabled")

    def _add_subgroup(self) -> None:
        parent = self._selected_group_name()
        if not parent:
            return
        dlg = GroupDialog(self.root, title="添加小组")
        self.root.wait_window(dlg)
        if dlg.dlg_result:
            _db_call(self._db.upsert_group(dlg.dlg_result, parent_name=parent))
            self._refresh()

    def _rebuild_move_menu(self, account: str) -> None:
        """右键「移动到」:未分组 + 各顶级分组(下挂其小组,缩进),当前所在打 ✓ 并禁用。"""
        self._move_menu.delete(0, "end")
        groups = _db_call(self._db.get_groups())
        sub = next(
            (s for s in _db_call(self._db.get_subscriptions()) if s["account_id"] == account),
            None,
        )
        current = sub.get("group_name") if sub else None
        tops = [g for g in groups if not g.get("parent_name")]

        def add(label: str, target: str | None, is_current: bool) -> None:
            self._move_menu.add_command(
                label=label + ("  ✓" if is_current else ""),
                command=lambda t=target: self._move_sub(account, t),
                state="disabled" if is_current else "normal",
            )

        add("(未分组)", None, current is None)
        for g in tops:
            add(g["name"], g["name"], g["name"] == current)
            for sg in groups:
                if sg.get("parent_name") == g["name"]:
                    add("  └ " + sg["name"], sg["name"], sg["name"] == current)

    def _move_sub(self, account: str, group_name: str | None) -> None:
        _db_call(self._db.set_group(account, group_name))
        self._refresh()

    def _rebuild_bulk_move_menu(self, accounts: list[str]) -> None:
        """多选「移动到」:未分组 + 顶级分组(下挂小组缩进);选中 → 全部 set_group。"""
        self._bulk_move_menu.delete(0, "end")
        groups = _db_call(self._db.get_groups())
        tops = [g for g in groups if not g.get("parent_name")]

        def add(label: str, target: str | None) -> None:
            self._bulk_move_menu.add_command(
                label=label, command=lambda t=target: self._bulk_move(accounts, t)
            )

        add("(未分组)", None)
        for g in tops:
            add(g["name"], g["name"])
            for sg in groups:
                if sg.get("parent_name") == g["name"]:
                    add("  └ " + sg["name"], sg["name"])

    def _bulk_move(self, accounts: list[str], group_name: str | None) -> None:
        for acct in accounts:
            _db_call(self._db.set_group(acct, group_name))
        self._refresh()

    def _bulk_new_subgroup(self) -> None:
        accounts = [s for s in self.tree.selection() if not _is_group(self.tree.item(s, "tags"))]
        if len(accounts) < 2:
            return
        groups = _db_call(self._db.get_groups())
        gmap = {g["name"]: g for g in groups}
        top_groups = [g["name"] for g in groups if not g.get("parent_name")]
        if not top_groups:
            messagebox.showerror("错误", "先建一个顶级分组,再在里面添加小组")
            return
        # 默认父分组:选中账号同属一个顶级分组 → 用它(否则用第一个)
        by_acct = {s["account_id"]: s for s in _db_call(self._db.get_subscriptions())}
        tops: set[str] = set()
        for acct in accounts:
            gname = (by_acct.get(acct) or {}).get("group_name")
            if not gname:
                continue
            g = gmap.get(gname)
            if not g:
                continue
            tops.add(gname if not g.get("parent_name") else g.get("parent_name"))
        default_parent = next(iter(tops)) if len(tops) == 1 else None
        dlg = NewSubgroupDialog(self.root, top_groups, default_parent=default_parent)
        self.root.wait_window(dlg)
        if not dlg.result:
            return
        parent, name = dlg.result
        _db_call(self._db.upsert_group(name, parent_name=parent))
        for acct in accounts:
            _db_call(self._db.set_group(acct, name))
        self._refresh()

    def _edit_sub(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        acct = sel[0]
        subs = _db_call(self._db.get_subscriptions())
        target = next((s for s in subs if s["account_id"] == acct), None)
        if not target:
            return
        dlg = SubscriptionDialog(self.root, self._db, target)
        self.root.wait_window(dlg)
        if not dlg.result:
            return
        r = dlg.result
        _db_call(
            self._db.upsert_subscription(
                r["account_id"],
                sync_mode=r["sync_mode"],
                remark=r["remark"],
                poll_interval=r["poll_interval"],
                fetch_limit=r["fetch_limit"],
                skip_retweets=r["skip_retweets"],
                group_name=r["group_name"],
            )
        )
        _db_call(self._db.set_watermark(r["account_id"], datetime.fromisoformat(r["watermark"])))
        self._refresh()

    def _toggle_sub(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        acct = sel[0]
        _db_call(self._db.toggle_enabled(acct))
        self._refresh()

    def _delete_sub(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        acct = sel[0]
        if messagebox.askyesno("确认", f"删除 @{acct}?"):
            _db_call(self._db.delete_subscription(acct))
            self._refresh()

    # ── 手动采集 / dead_letter / 分组管理 ──

    def _set_status(self, text: str) -> None:
        self._status.config(text=text)

    def _finish_collect(self, msg: str) -> None:
        """采集完成后(回主线程):停进度条 + 更新状态栏 + 弹结果框 + 刷新列表。"""
        self._progress.stop()
        self._set_status(msg)
        if "采集失败" in msg:
            messagebox.showerror("采集结果", msg)
        else:
            messagebox.showinfo("采集结果", msg)
        self._refresh()

    # ── 链接 → TG(一次性:fxTwitter 取推 → sink 发,不碰 DB / cache / watermark)──

    def _link_send(self) -> None:
        """按钮 → 弹链接框 → 后台跑 send_tweet_by_url → 完成回主线程弹结果。"""
        dlg = LinkSendDialog(self.root)
        self.root.wait_window(dlg)
        if not dlg.url:
            return
        try:
            sink = self._ensure_sink()
        except Exception as e:
            messagebox.showerror(
                "配置错误", f"无法初始化发送(检查 config.yaml 的 bot_token/chat_id):\n{e}"
            )
            return
        self._set_status("链接发送中…")
        self._progress.start()
        _loop.spawn(self._run_link_send(dlg.url, sink))

    async def _run_link_send(self, url: str, sink) -> None:
        from src.oneshot import OneShotResult, send_tweet_by_url

        try:
            result = await send_tweet_by_url(url, sink)
        except Exception as e:
            result = OneShotResult(ok=False, message=f"发送失败: {e}")
        self.root.after(0, lambda: self._finish_link_send(result))

    def _finish_link_send(self, result) -> None:
        self._progress.stop()
        self._set_status(result.message)
        if result.ok:
            messagebox.showinfo("发送完成", result.message)
        else:
            messagebox.showerror("发送失败", result.message)

    # ── 追踪(主循环:定时轮询所有启用订阅,等同 python -m src.main)──

    def _toggle_tracking(self) -> None:
        if self._tracking:
            self._stop_tracking()
        else:
            self._start_tracking()

    def _start_tracking(self) -> None:
        try:
            source, sink = self._ensure_pipeline()
        except Exception as e:
            messagebox.showerror("配置错误", f"无法初始化采集管道(检查 config.yaml):\n{e}")
            return
        loop_interval = load_config(CONFIG_PATH).scheduler.loop_interval_seconds
        logger.info("[track] 开始追踪:每 %ss 一轮", loop_interval)
        self._track_stop = asyncio.Event()
        self._tracking = True
        self._track_btn.config(text="⏹ 停止追踪")
        self._set_status(f"追踪中(每 {loop_interval}s 轮询一次)…")
        from src.sync_engine import run_loop

        _loop.spawn(
            run_loop(
                self._db,
                source,
                sink,
                loop_interval=loop_interval,
                stop_event=self._track_stop,
            )
        )
        self._schedule_refresh()

    def _stop_tracking(self) -> None:
        if self._track_stop is not None:
            _loop.call_soon(self._track_stop.set)
        self._track_stop = None
        self._tracking = False
        self._track_btn.config(text="▶ 开始追踪")
        self._set_status("已停止追踪")

    def _schedule_refresh(self) -> None:
        """追踪期间每 5s 刷新一次列表(看水位线推进)。"""
        if not self._tracking:
            return
        self._refresh()
        self.root.after(5000, self._schedule_refresh)

    def _on_close(self) -> None:
        self._stop_tracking()
        # 关闭前清掉所有 account 的 running 标记(防中途关闭留下"采集中"孤儿状态)。
        # 带 5s 超时:loop 卡住时也别让窗口关不掉。
        with contextlib.suppress(Exception):
            _loop.call(self._db.clear_all_running(), timeout=5.0)
        self.root.destroy()

    # ── 日志窗口 ──

    def _show_logs(self) -> None:
        """打开执行日志窗口(深色控制台风格;seed 历史 + 实时追加)。"""
        if self._log_win is not None and self._log_win.winfo_exists():
            self._log_win.lift()
            self._log_win.focus_force()
            return
        win = tk.Toplevel(self.root)
        win.title("执行日志")
        win.geometry("800x440")
        body = ttk.Frame(win)
        body.pack(fill="both", expand=True)
        scroll = ttk.Scrollbar(body)
        scroll.pack(side="right", fill="y")
        text = tk.Text(
            body,
            wrap="none",
            bg="#1e1e1e",
            fg="#d4d4d4",
            insertbackground="#d4d4d4",
            font=("Consolas", 9),
        )
        text.pack(side="left", fill="both", expand=True)
        scroll.config(command=text.yview)
        text.config(yscrollcommand=scroll.set)
        if self._log_lines:
            text.insert("end", "".join(f"{line}\n" for line in self._log_lines))
            text.see("end")
        self._log_text = text
        self._log_win = win
        win.protocol("WM_DELETE_WINDOW", self._close_logs)

    def _close_logs(self) -> None:
        self._log_text = None
        if self._log_win is not None:
            self._log_win.destroy()
            self._log_win = None

    def _drain_logs(self) -> None:
        """每 300ms 把队列里的日志抽到历史 deque(+ 日志窗口 Text,若开着)。"""
        appended = False
        while True:
            try:
                line = self._log_q.get_nowait()
            except queue.Empty:
                break
            self._log_lines.append(line)
            if self._log_text is not None:
                self._log_text.insert("end", f"{line}\n")
                appended = True
        if appended:
            self._trim_and_scroll()
        self.root.after(300, self._drain_logs)

    def _trim_and_scroll(self) -> None:
        t = self._log_text
        if t is None:
            return
        line_count = int(t.index("end-1c").split(".")[0])
        if line_count > 3000:  # 防止 Text 无限增长
            t.delete("1.0", f"{line_count - 2000}.0")
        t.see("end")

    def _selected_group_name(self) -> str | None:
        """当前选中的分组名(顶级或小组);未分组 / 订阅节点 → None。"""
        sel = self.tree.selection()
        if not sel or not _is_group(self.tree.item(sel[0], "tags")):
            return None
        name = self.tree.item(sel[0], "text")[2:].rsplit(" (", 1)[0]
        return None if name == "未分组" else name

    def _toggle_group(self) -> None:
        name = self._selected_group_name()
        if not name:
            return
        node = self.tree.selection()[0]
        if self.tree.parent(node):
            # 小组:开关 = 批量开/关组内账号(开→全开,关→全关+清running)
            _db_call(self._db.toggle_subgroup(name))
        else:
            # 顶级:切分组 enabled(级联:父关 → 其下全不采)
            _db_call(self._db.toggle_group(name))
        self._refresh()

    def _collect_now(self) -> None:
        """右键 → 立即采集:查 running 防并发 → 后台跑 collect_account → 完成回主线程刷新。"""
        sel = self.tree.selection()
        if not sel or not self.tree.parent(sel[0]):
            return
        acct = sel[0]
        sub = next(
            (s for s in _db_call(self._db.get_subscriptions()) if s["account_id"] == acct), None
        )
        if not sub:
            return
        if sub.get("running"):
            messagebox.showwarning("采集中", f"@{acct} 正在采集中(可能主循环在跑),请稍后再试。")
            return
        try:
            source, sink = self._ensure_pipeline()
        except Exception as e:
            messagebox.showerror("配置错误", f"无法初始化采集管道(检查 config.yaml):\n{e}")
            return
        self._set_status(f"@{acct} 采集中…")
        self._progress.start()
        _db_call(self._db.set_running(acct, True))
        _loop.spawn(self._run_collect(source, sink, sub))

    def _ensure_sink(self):
        """懒加载 TelegramSink(读 config.yaml)。链接发送只依赖它,不需要 Source 配置。"""
        if self._sink is None:
            from src.telegram_bot import TelegramSink

            cfg = load_config(CONFIG_PATH)
            self._sink = TelegramSink(cfg.telegram.bot_token, cfg.telegram.chat_id)
        return self._sink

    def _ensure_pipeline(self):
        """懒加载 Source + TelegramSink(读 config.yaml)。首次手动采集时构造,之后复用。"""
        if self._pipeline is None:
            from src.source.factory import make_source

            cfg = load_config(CONFIG_PATH)
            source = make_source(cfg)  # 缺 token/配置 → raise(由 _collect_now 捕获弹错)
            sink = self._ensure_sink()
            self._pipeline = (source, sink)
        return self._pipeline

    async def _run_collect(self, source, sink, sub: dict) -> None:
        """后台跑一次采集(非阻塞,不卡 GUI);完成后 root.after 回主线程刷新状态与列表。"""
        from src.sync_engine import collect_account

        acct = sub["account_id"]
        now = datetime.now(UTC)
        try:
            result = await collect_account(
                self._db,
                source,
                sink,
                acct,
                now=now,
                sync_mode=sub.get("sync_mode", "media_only"),
                fetch_limit=sub.get("fetch_limit", 20),
                skip_retweets=bool(sub.get("skip_retweets", 1)),
            )
            await self._db.set_last_polled(acct, now)
            extra = f",dead {len(result.dead)} 条" if result.dead else ""
            msg = f"@{acct} 采集完成:发送 {len(result.sent)} 条{extra}"
        except Exception as e:
            msg = f"@{acct} 采集失败:{e}"
        finally:
            await self._db.set_running(acct, False)
        self.root.after(0, lambda: self._finish_collect(msg))

    def _show_dead_letter(self) -> None:
        sel = self.tree.selection()
        if not sel or not self.tree.parent(sel[0]):
            return
        acct = sel[0]
        dl = _db_call(self._db.get_dead_letter(acct))
        dlg = tk.Toplevel(self.root)
        dlg.title(f"@{acct} 的 dead_letter")
        dlg.geometry("660x320")
        tree = ttk.Treeview(dlg, columns=("ts", "reason", "abandoned"), show="headings")
        tree.heading("ts", text="推文时间")
        tree.heading("reason", text="原因")
        tree.heading("abandoned", text="放弃时间")
        tree.column("ts", width=170)
        tree.column("reason", width=300)
        tree.column("abandoned", width=170)
        tree.pack(fill="both", expand=True, padx=6, pady=6)
        for d in dl:
            tree.insert(
                "",
                "end",
                values=(d.get("post_ts"), d.get("reason"), d.get("abandoned_at")),
            )
        ttk.Label(dlg, text=f"共 {len(dl)} 条(只读)").pack(anchor="w", padx=8, pady=4)

    def _rename_group(self) -> None:
        name = self._selected_group_name()
        if not name:
            return
        dlg = GroupDialog(self.root, title="重命名分组", default=name)
        self.root.wait_window(dlg)
        if dlg.dlg_result and dlg.dlg_result != name:
            _db_call(self._db.rename_group(name, dlg.dlg_result))
            self._refresh()

    def _delete_group(self) -> None:
        name = self._selected_group_name()
        if not name:
            return
        if messagebox.askyesno("确认", f"删除分组「{name}」?\n组内订阅会变为未分组。"):
            _db_call(self._db.delete_group(name))
            self._refresh()

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    setup_logging()
    app = AdminApp()
    app.run()


if __name__ == "__main__":
    main()
