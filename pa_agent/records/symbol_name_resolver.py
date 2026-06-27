"""Resolve a stock symbol to a human-readable name for the export report.

Best-effort: A-shares are looked up via the East Money single-stock spot
endpoint (lightweight, returns the 股票简称); HK/US tickers fall back to a
local alias reverse-lookup. Any failure returns ``""`` so the report exporter
degrades gracefully to a code-only header — name resolution must never break
report export.
"""
from __future__ import annotations

import logging
import time

from pa_agent.data.tv_symbol_lookup import lookup_name_by_symbol

logger = logging.getLogger(__name__)

# Process-level cache: {symbol: (name, expire_ts)}; TTL avoids repeated lookups.
_CACHE_TTL_S = 600  # 10 minutes
_cache: dict[str, tuple[str, float]] = {}


def _is_ashare_code(symbol: str) -> bool:
    """True for a 6-digit A-share code (沪深京 A 股，含 0/3/6/8/4 开头)."""
    s = (symbol or "").strip()
    return s.isdigit() and len(s) == 6


def _query_ashare_name(symbol: str) -> str:
    """Fetch the 股票简称 for an A-share code via the East Money quote endpoint.

    ``/api/qt/stock/get`` returns ``f58`` = 股票简称 for any 沪深 A 股 code.
    Falls back to the spot row's ``name`` field if the quote payload is empty.
    """
    try:
        from pa_agent.data.eastmoney_client import (
            fetch_stock_quote_payload,
            fetch_stock_spot_row,
        )

        payload = fetch_stock_quote_payload(symbol)
        if payload:
            name = str(payload.get("f58") or "").strip()
            if name and name != symbol:
                return name
        # Fallback: clist spot row
        row = fetch_stock_spot_row(symbol)
        if row:
            name = str(row.get("name") or "").strip()
            if name and name != symbol:
                return name
    except Exception as exc:  # noqa: BLE001
        logger.debug("A-share name lookup failed for %s: %s", symbol, exc)
    return ""


def resolve_stock_name(symbol: str) -> str:
    """Return a display name for *symbol*, or ``""`` if unknown.

    Uses an in-process cache (TTL ``_CACHE_TTL_S``). A-shares hit the East Money
    spot endpoint; other tickers use the local TradingView alias table. All
    failures resolve to ``""`` (callers show the bare code).
    """
    sym = (symbol or "").strip()
    if not sym:
        return ""

    now = time.monotonic()
    cached = _cache.get(sym)
    if cached is not None:
        name, expires = cached
        if now < expires:
            return name

    if _is_ashare_code(sym):
        name = _query_ashare_name(sym)
    else:
        # HK / US / index / forex — reverse-lookup the local alias table
        try:
            hit = lookup_name_by_symbol(sym)
            name = hit or ""
        except Exception as exc:  # noqa: BLE001
            logger.debug("alias reverse-lookup failed for %s: %s", sym, exc)
            name = ""

    _cache[sym] = (name, now + _CACHE_TTL_S)
    return name


def clear_cache() -> None:
    """Drop the name cache (used by tests)."""
    _cache.clear()
