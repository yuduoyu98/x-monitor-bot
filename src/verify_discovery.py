"""Live discovery verification.

Discovers recent tweets for the given X accounts, checks discovery invariants
(no dups, ordered, valid IDs), and dumps a table to compare against the X
profile by eye — i.e. the "不重不漏" ground truth that only a human can confirm.
Does NOT call any resolver.

Backends:
  nitter  — Nitter RSS (default; no auth, fragile single point of failure)
  scweet  — X GraphQL via Scweet (robust; needs SCWEET_AUTH_TOKEN env + proxy in CN)

Usage:
    python -m src.verify_discovery <account_id> [<account_id> ...] \
        [--backend nitter|scweet] [--nitter-instance URL] [--proxy URL] [--limit N]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

# Allow running as a script directly: python src/verify_discovery.py ...
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from src import setup_logging
from src.discovery import (
    DEFAULT_NITTER_INSTANCE,
    NitterDiscoverer,
    ScweetDiscoverer,
    check_discovery,
    format_report,
)


def _default_nitter_instance() -> str:
    """Default Nitter instance: the one in config.yaml, else the built-in default."""
    try:
        from src.config import load_config

        return load_config("config.yaml").fetcher.nitter_instance
    except Exception:
        return DEFAULT_NITTER_INSTANCE


def _detect_proxy(explicit: str | None) -> str | None:
    """Resolve a proxy URL: explicit flag > env vars > Windows system proxy registry."""
    if explicit:
        return explicit
    for var in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy"):
        if os.environ.get(var):
            return os.environ[var]
    try:
        import urllib.request

        return urllib.request.getproxies().get("https")
    except Exception:
        return None


def _build_discoverer(args: argparse.Namespace):
    if args.backend == "nitter":
        return NitterDiscoverer(nitter_instance=args.nitter_instance)
    if args.backend == "scweet":
        auth_token = os.environ.get("SCWEET_AUTH_TOKEN")
        if not auth_token:
            sys.exit("scweet backend needs SCWEET_AUTH_TOKEN env var (auth_token cookie).")
        proxy = _detect_proxy(args.proxy)
        if not proxy:
            sys.exit(
                "scweet backend needs a proxy to reach x.com; pass --proxy URL "
                "(e.g. http://127.0.0.1:7890) or set HTTPS_PROXY."
            )
        print(f"[scweet] proxy={proxy}")
        return ScweetDiscoverer(auth_token=auth_token, proxy=proxy)
    sys.exit(f"unknown backend: {args.backend}")


def main() -> None:
    # Windows consoles default to a locale codepage (e.g. GBK) that can't encode the
    # emoji/CJK in tweet text — force UTF-8 so printing the report never crashes.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Verify X discovery for given accounts, no-dup / no-miss."
    )
    parser.add_argument(
        "accounts", nargs="+", metavar="account_id", help="X account id(s), without @"
    )
    parser.add_argument(
        "--backend", choices=("nitter", "scweet"), default="nitter", help="Discovery source"
    )
    parser.add_argument(
        "--nitter-instance",
        default=_default_nitter_instance(),
        help="Nitter instance URL (nitter backend only; default: config.yaml or nitter.net)",
    )
    parser.add_argument(
        "--proxy",
        default=None,
        help="Proxy for scweet backend (default: auto-detect env/Windows system proxy)",
    )
    parser.add_argument("--limit", type=int, default=20, help="Max tweets per account (default 20)")
    args = parser.parse_args()

    # Keep library log noise down — the printed report is the signal.
    setup_logging(logging.WARNING)

    discoverer = _build_discoverer(args)

    async def run() -> None:
        try:
            reports = await check_discovery(discoverer, args.accounts, limit=args.limit)
            print(format_report(reports))
        finally:
            await discoverer.close()

    asyncio.run(run())


if __name__ == "__main__":
    main()
