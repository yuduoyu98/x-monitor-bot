#!/usr/bin/env python3
"""Scweet 探针:给定 auth_token + 账号,看 Scweet 实际返回什么。

用法(国内需代理):
    SCWEET_AUTH_TOKEN=<专用号auth_token> python scripts/probe_scweet.py <账号> \\
        [--proxy http://127.0.0.1:7890] [--limit 5]

每条推打印:原始字典(JSON)+ 解析后的 DiscoveredTweet(post_id / 时间 / 文本 / 是否转推 / 媒体URL)。
不下载媒体,只看数据。用于排查:能不能取到 / 取到什么 / 媒体解析对不对 / 锁推号行为 等。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from src.source.scweet import _detect_proxy, parse_tweet  # 复用项目的代理探测 + 解析器


async def main() -> None:
    ap = argparse.ArgumentParser(description="Scweet 探针:看给定的 token+账号能返回什么")
    ap.add_argument("account", help="X 账号(不带 @)")
    ap.add_argument("--limit", type=int, default=5, help="取多少条(默认 5)")
    ap.add_argument("--proxy", default=None, help="代理 URL(默认走 env/系统代理)")
    ap.add_argument(
        "--db-path",
        default=str(_root / "probe_scweet.db"),
        help="Scweet 状态 db(默认独立 probe_scweet.db,不污染生产库)",
    )
    args = ap.parse_args()

    auth_token = os.environ.get("SCWEET_AUTH_TOKEN")
    if not auth_token:
        print("缺少 SCWEET_AUTH_TOKEN 环境变量(专用号 auth_token cookie)", file=sys.stderr)
        sys.exit(2)

    proxy = args.proxy or _detect_proxy()
    account = args.account.lstrip("@")

    from Scweet import Scweet

    print(f"[init] auth_token=****{auth_token[-4:]}  proxy={proxy!r}  db={args.db_path}")
    client = Scweet(auth_token=auth_token, proxy=proxy, db_path=args.db_path)

    eligible = client.db.list_accounts(eligible_only=True)
    print(
        f"[auth] db 里 eligible 账号: {eligible}  (空=fresh db 或 token 失效;以下面 fetch 结果为准)"
    )

    print(f"\n[fetch] aget_profile_tweets(['{account}'], limit={args.limit}) ...")
    try:
        raw_list = await client.aget_profile_tweets([account], limit=args.limit)
    except Exception as e:  # noqa: BLE001
        print(f"[fetch] 抛异常: {e!r}", file=sys.stderr)
        await _close(client)
        sys.exit(1)

    print(f"[fetch] 返回 {len(raw_list)} 条\n")
    if not raw_list:
        print(
            "  0 条 —— 可能:token 失效 / 代理不通 / 账号不存在 / "
            "锁推且专用号未批准关注 / 该号确实没推"
        )

    for i, raw in enumerate(raw_list, 1):
        print(f"━━━━━━━━ #{i} 原始字典 ━━━━━━━━")
        print(json.dumps(raw, ensure_ascii=False, indent=2, default=str))
        parsed = parse_tweet(raw)
        print(f"━━━━━━━━ #{i} 解析(parse_tweet) ━━━━━━━━")
        if parsed is None:
            print("  -> None(解析失败,缺 tweet_id?)")
        else:
            text = (parsed.text or "").replace("\n", " ")
            print(f"  post_id    : {parsed.post_id}")
            print(f"  timestamp  : {parsed.timestamp}")
            print(f"  is_retweet : {parsed.is_retweet}")
            print(f"  display    : {parsed.display_name!r}")
            print(f"  text       : {text[:100]!r}")
            print(f"  media ({len(parsed.media)}):")
            for m in parsed.media:
                print(f"    - {m.type:13s} {m.url}")
        print()

    await _close(client)


async def _close(client) -> None:
    aclose = getattr(client, "aclose", None)
    if aclose is not None:
        await aclose()


if __name__ == "__main__":
    asyncio.run(main())
