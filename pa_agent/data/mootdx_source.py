"""mootdx (通达信 TCP) A-share data source.

Uses mootdx library to connect directly to TDX quote servers (TCP port 7709).
No HTTP involved — completely independent from web APIs (East Money / Tencent / Sina).

Advantages:
- Most stable: TCP protocol, no IP ban, no rate limit issues
- Real-time quotes: 46 fields including 5-level order book
- Full K-line range: day/week/month/1min/5min/15min/30min/60min
- Tick data (逐笔成交) available during session
- Financial snapshot + F10 text data
- Not affected by HTTP API outages

Requirements: pip install mootdx
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from pa_agent.data.ashare_common import (
    PRESET_SYMBOLS as _PRESET_SYMBOLS,
    ashare_head_bar_live as _ashare_head_bar_live,
    ashare_session_open as _ashare_session_open,
    ashare_trading_day as _ashare_trading_day,
    cn_now as _cn_now,
    index_symbol_for_api as _index_symbol_for_api,
    is_index_symbol,
    merge_ohlcv as _merge_ohlcv,
    normalize_ashare_symbol,
    resample_rows_to_4h as _resample_rows_to_4h,
    row_time_to_ts_ms as _row_time_to_ts_ms,
    rows_to_kline_bars as _rows_to_kline_bars,
)
from pa_agent.data.base import DataSource, DataSourceTransientError, KlineBar

logger = logging.getLogger(__name__)

_CN_TZ = ZoneInfo("Asia/Shanghai")

# mootdx category mapping
# 0=5分钟, 1=15分钟, 2=30分钟, 3=60分钟, 4=日线, 5=周线, 6=月线, 7=1分钟, 8=1分钟, 9=日线, 10=30分钟, 11=60分钟
# Correct TDX category codes:
_TF_CATEGORY: dict[str, int] = {
    "1m": 7,    # 1分钟K线 (some servers use 8)
    "5m": 0,    # 5分钟K线
    "15m": 1,   # 15分钟K线
    "30m": 2,   # 30分钟K线
    "1h": 3,    # 60分钟K线
    "1d": 4,    # 日K线
    "1w": 5,    # 周K线
    "1M": 6,    # 月K线
}

_SUPPORTED_TIMEFRAMES: tuple[str, ...] = (
    "1m",
    "5m",
    "15m",
    "30m",
    "1h",
    "4h",
    "1d",
    "1w",
    "1M",
)

# Throttle to be gentle with TDX servers
_MOOTDX_MIN_INTERVAL_S = 0.3
_last_mootdx_mono: float = 0.0


def _throttle_mootdx() -> None:
    global _last_mootdx_mono
    now = time.monotonic()
    wait = _MOOTDX_MIN_INTERVAL_S - (now - _last_mootdx_mono)
    if wait > 0:
        time.sleep(wait)
    _last_mootdx_mono = time.monotonic()


def _mootdx_market(code: str) -> int:
    """6-digit code → mootdx market id (0=深圳, 1=上海, 2=北证)."""
    if code.startswith(("6", "9")):
        return 1  # 上海
    if code.startswith(("8", "4")):
        return 2  # 北证（mootdx可能不完全支持）
    return 0      # 深圳


def _bars_to_rows(klines: list[dict[str, Any]], *, is_minute: bool) -> list[dict[str, Any]]:
    """Convert mootdx kline list to our internal rows format (ascending)."""
    rows: list[dict[str, Any]] = []
    for k in klines:
        # mootdx returns datetime field
        dt = k.get("datetime")
        if isinstance(dt, datetime):
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_CN_TZ)
            ts_ms = int(dt.timestamp() * 1000)
        elif dt is not None:
            # May be string or float timestamp
            ts_ms = _row_time_to_ts_ms(dt)
        else:
            ts_ms = int(_cn_now().timestamp() * 1000)

        rows.append({
            "ts_open": ts_ms,
            "open": float(k.get("open", 0)),
            "high": float(k.get("high", 0)),
            "low": float(k.get("low", 0)),
            "close": float(k.get("close", 0)),
            "volume": float(k.get("vol", 0) or k.get("volume", 0) or 0),
            "amount": float(k.get("amount", 0) or 0),
            "pct_chg": None,
        })

    # Fill pct_chg
    for i in range(1, len(rows)):
        prev_c = float(rows[i - 1]["close"])
        if prev_c > 0:
            rows[i]["pct_chg"] = (float(rows[i]["close"]) - prev_c) / prev_c * 100.0

    return rows


class MootdxSource(DataSource):
    """A-share K-line + real-time quote via mootdx (TDX TCP protocol).

    Completely independent of HTTP APIs. Connects directly to TDX servers.
    Most stable option for users on networks where East Money/Tencent is blocked.
    """

    def __init__(self) -> None:
        self._symbol: str = ""
        self._timeframe: str = ""
        self._connected: bool = False
        self._client: Any = None  # mootdx Quotes client

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def connect(self) -> None:
        try:
            from mootdx.quotes import Quotes
        except ImportError as exc:
            raise DataSourceTransientError(
                "未安装 mootdx，请执行: pip install mootdx"
            ) from exc

        try:
            self._client = Quotes.factory(market="std")
            # Test connection with a simple query
            self._client.bars(symbol="000001", category=4, offset=1)
        except Exception as exc:
            raise DataSourceTransientError(
                f"mootdx TCP 连接失败（可能是海外网络无法直连通达信服务器）: {exc}"
            ) from exc

        self._connected = True
        logger.info("MootdxSource connected (TDX TCP, market=std)")

    def disconnect(self) -> None:
        # mootdx doesn't have explicit disconnect, but we clean up
        self._client = None
        self._connected = False
        logger.info("MootdxSource disconnected")

    # ── Discovery ─────────────────────────────────────────────────────────────

    def list_symbols(self) -> list[str]:
        return list(_PRESET_SYMBOLS)

    def supported_timeframes(self) -> list[str]:
        return list(_SUPPORTED_TIMEFRAMES)

    # ── Subscription ──────────────────────────────────────────────────────────

    def subscribe(self, symbol: str, timeframe: str) -> None:
        if timeframe not in _SUPPORTED_TIMEFRAMES:
            raise ValueError(
                f"Unsupported timeframe: {timeframe!r}. "
                f"Use one of {list(_SUPPORTED_TIMEFRAMES)}"
            )
        code = normalize_ashare_symbol(symbol)
        if not code:
            raise ValueError("A股代码无效，请输入 6 位数字（如 600519）或指数 sh000300")
        if is_index_symbol(symbol):
            raise ValueError(
                "mootdx 暂不支持指数代码，请使用个股代码（如 600519）"
            )
        self._symbol = code
        self._timeframe = timeframe
        logger.info("MootdxSource subscribed: %s %s", code, timeframe)

    def unsubscribe(self) -> None:
        self._symbol = ""
        self._timeframe = ""
        logger.info("MootdxSource unsubscribed")

    # ── Data fetch ────────────────────────────────────────────────────────────

    def latest_snapshot(self, n: int) -> list[KlineBar]:
        """Return *n* bars newest-first; bars[0] is the forming (unclosed) bar."""
        if not self._connected:
            raise DataSourceTransientError("mootdx 未连接")
        if not self._symbol or not self._timeframe:
            raise DataSourceTransientError("mootdx 未订阅品种/周期")

        fetch_n = max(n + 5, 30)
        try:
            rows_asc = self._fetch_klines(self._symbol, self._timeframe, fetch_n)
        except DataSourceTransientError:
            raise
        except Exception as exc:
            logger.warning("mootdx fetch failed: %s", exc)
            raise DataSourceTransientError(f"mootdx 拉取失败: {exc}") from exc

        if not rows_asc:
            raise DataSourceTransientError(
                f"mootdx 未返回数据: {self._symbol} {self._timeframe}"
            )

        # 4h: resample from 1h
        if self._timeframe == "4h":
            rows_asc = _resample_rows_to_4h(rows_asc)[-fetch_n:]

        # Apply real-time quote to forming bar during session
        if _ashare_session_open():
            self._apply_quote_to_forming(rows_asc)

        rows_newest = list(reversed(rows_asc[-fetch_n:]))
        for i, row in enumerate(rows_newest):
            row["closed"] = not (i == 0 and _ashare_session_open())

        return _rows_to_kline_bars(rows_newest, n)

    # ── K-line fetch ──────────────────────────────────────────────────────────

    def _fetch_klines(
        self, symbol: str, timeframe: str, n: int
    ) -> list[dict[str, Any]]:
        """Fetch K-line data from mootdx."""
        _throttle_mootdx()

        code = normalize_ashare_symbol(symbol)
        market = _mootdx_market(code)
        category = _TF_CATEGORY.get(timeframe, 4)

        is_minute = timeframe in ("1m", "5m", "15m", "30m", "1h")

        try:
            # mootdx returns newest-first by default, we need oldest-first
            # offset controls how many bars to fetch
            df = self._client.bars(
                symbol=code,
                category=category,
                offset=n + 10,
            )
        except Exception as exc:
            raise DataSourceTransientError(
                f"mootdx bars() failed for {code}: {exc}"
            ) from exc

        if df is None:
            return []

        # mootdx returns a DataFrame (newest first) or list of dicts
        # Convert to our format (ascending = oldest first)
        if hasattr(df, "to_dict"):
            # DataFrame
            records = df.to_dict("records")
        elif isinstance(df, list):
            records = df
        else:
            records = []

        if not records:
            return []

        # mootdx returns newest-first; reverse to ascending
        records = list(reversed(records))
        rows = _bars_to_rows(records, is_minute=is_minute)

        return rows[-(n + 5):]

    # ── Real-time quote for forming bar ───────────────────────────────────────

    def _apply_quote_to_forming(self, rows_asc: list[dict[str, Any]]) -> None:
        """Fetch real-time quote via mootdx and update the forming bar."""
        if not rows_asc or not self._symbol:
            return

        try:
            _throttle_mootdx()
            quotes = self._client.quotes(symbol=[self._symbol])
        except Exception as exc:
            logger.debug("mootdx quote fetch failed: %s", exc)
            return

        if not quotes:
            return

        q = quotes[0] if isinstance(quotes, list) else quotes
        if not isinstance(q, dict):
            return

        price = float(q.get("price", 0) or q.get("last_price", 0))
        if price <= 0:
            return

        from pa_agent.data.ashare_common import apply_session_quote_to_forming_row

        apply_session_quote_to_forming_row(
            rows_asc[-1],
            price=price,
            open_=float(q.get("open", 0) or 0),
            high=float(q.get("high", 0) or 0),
            low=float(q.get("low", 0) or 0),
            volume=float(q.get("vol", 0) or q.get("volume", 0) or 0),
            amount=float(q.get("amount", 0) or 0),
            prev_close=float(q.get("last_close", 0) or q.get("pre_close", 0) or 0),
            daily=(self._timeframe in ("1d", "1w", "1M")),
            volume_lots=False,
            symbol=self._symbol,
        )
