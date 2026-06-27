"""Tencent Finance direct HTTP A-share data source.

Zero-dependency (only ``requests``), independent of AkShare library.

K-line:  web.ifzq.gtimg.cn/appstock/app/fqkline/get
Quote:   qt.gtimg.cn/q=  (for forming bar during trading session)

Advantages:
- Fastest HTTP quote (~0.18s measured)
- Independent from AkShare / East Money push2his
- No IP ban risk
- Provides PE/PB/market cap in quote data
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, time as time_cls
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

_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
_QUOTE_URL = "https://qt.gtimg.cn/q="
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

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

# Tencent kline type mapping
# day/qfqday=前复权日线, week/qfqweek=前复权周线, month/qfqmonth=前复权月线
# For minute data, Tencent uses different endpoint
_TF_KLINE_TYPE: dict[str, str] = {
    "1d": "day",
    "1w": "week",
    "1M": "month",
}

# Minute kline types (different URL pattern)
_TF_MINUTE_TYPE: dict[str, str] = {
    "1m": "1",
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "1h": "60",
}


def _tencent_prefix(code: str) -> str:
    """6-digit code → tencent market prefix (sh/sz/bj)."""
    if code.startswith(("6", "9")):
        return "sh"
    if code.startswith("8"):
        return "bj"
    return "sz"


def _tencent_symbol(code: str) -> str:
    """6-digit code → prefixed symbol (sh600519)."""
    return f"{_tencent_prefix(code)}{code}"


def _kline_parse_date(s: str) -> int:
    """Parse '2026-06-27' or '2026-06-27 14:30:00' to ts_open in ms (CN timezone)."""
    s = str(s).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s[:len(fmt)], fmt).replace(tzinfo=_CN_TZ)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    return int(_cn_now().timestamp() * 1000)


class TencentSource(DataSource):
    """A-share K-line + real-time quote via Tencent Finance direct HTTP.

    Independent of AkShare. Uses two endpoints:
    - K-line history: web.ifzq.gtimg.cn (daily/weekly/monthly + minute)
    - Real-time quote: qt.gtimg.cn (for forming bar during session)
    """

    def __init__(self) -> None:
        self._symbol: str = ""
        self._timeframe: str = ""
        self._connected: bool = False
        self._snap_cache_n: int = 0
        self._snap_cache_ts: float = 0.0
        self._snap_cache_bars: list[KlineBar] = []

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def connect(self) -> None:
        try:
            import requests  # noqa: F401
        except ImportError as exc:
            raise DataSourceTransientError(
                "未安装 requests，请执行: pip install requests"
            ) from exc
        self._connected = True
        logger.info("TencentSource connected (direct HTTP, no AkShare dependency)")

    def disconnect(self) -> None:
        self._connected = False
        logger.info("TencentSource disconnected")

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
        self._symbol = code
        self._timeframe = timeframe
        logger.info("TencentSource subscribed: %s %s", code, timeframe)

    def unsubscribe(self) -> None:
        self._symbol = ""
        self._timeframe = ""
        logger.info("TencentSource unsubscribed")

    # ── Data fetch ────────────────────────────────────────────────────────────

    def latest_snapshot(self, n: int) -> list[KlineBar]:
        """Return *n* bars newest-first; bars[0] is the forming (unclosed) bar."""
        if not self._connected:
            raise DataSourceTransientError("Tencent 未连接")
        if not self._symbol or not self._timeframe:
            raise DataSourceTransientError("Tencent 未订阅品种/周期")

        fetch_n = max(n + 5, 30)
        try:
            if self._timeframe in _TF_KLINE_TYPE:
                rows_asc = self._fetch_daily_kline(self._symbol, self._timeframe, fetch_n)
            elif self._timeframe in _TF_MINUTE_TYPE:
                rows_asc = self._fetch_minute_kline(self._symbol, self._timeframe, fetch_n)
            else:
                raise DataSourceTransientError(f"Unsupported timeframe: {self._timeframe}")
        except DataSourceTransientError:
            raise
        except Exception as exc:
            logger.warning("Tencent fetch failed: %s", exc)
            raise DataSourceTransientError(f"Tencent 拉取失败: {exc}") from exc

        if not rows_asc:
            raise DataSourceTransientError(
                f"Tencent 未返回数据: {self._symbol} {self._timeframe}"
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

    @staticmethod
    def _http_get(url: str, params: dict[str, str], *, timeout: int = 15) -> Any:
        import requests

        resp = requests.get(
            url,
            params=params,
            headers={"User-Agent": _UA},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def _fetch_daily_kline(
        self, symbol: str, timeframe: str, n: int
    ) -> list[dict[str, Any]]:
        """Fetch daily/weekly/monthly K-line from Tencent web API.

        Endpoint: web.ifzq.gtimg.cn/appstock/app/fqkline/get
        Returns: [date, open, close, high, low, volume, amount, ...]
        """
        code = normalize_ashare_symbol(symbol)

        # Index symbols need special handling
        if is_index_symbol(symbol):
            idx_sym = _index_symbol_for_api(symbol)
            # Index uses 'day' (no qfq for index)
            ktype = _TF_KLINE_TYPE.get(timeframe, "day")
            params = {"param": f"{idx_sym},{ktype},,,{n},"}
            try:
                data = self._http_get(_KLINE_URL, params)
            except Exception as exc:
                raise DataSourceTransientError(f"Tencent index kline failed: {exc}") from exc

            rows = self._parse_index_kline(data, idx_sym, ktype)
            return rows[-(n + 5):]

        # Individual stock — use qfq (前复权)
        ktype = _TF_KLINE_TYPE.get(timeframe, "day")
        qfq_type = f"qfq{ktype}"
        tc_sym = _tencent_symbol(code)
        params = {"param": f"{tc_sym},{ktype},,,{n},qfq"}

        try:
            data = self._http_get(_KLINE_URL, params)
        except Exception as exc:
            raise DataSourceTransientError(f"Tencent kline failed: {exc}") from exc

        rows = self._parse_stock_kline(data, tc_sym, ktype, qfq_type)
        return rows[-(n + 5):]

    def _fetch_minute_kline(
        self, symbol: str, timeframe: str, n: int
    ) -> list[dict[str, Any]]:
        """Fetch minute-level K-line from Tencent.

        Uses: web.ifzq.gtimg.cn/appstock/app/kline/mkline
        """
        import requests

        code = normalize_ashare_symbol(symbol)
        if is_index_symbol(symbol):
            raise DataSourceTransientError(
                f"Tencent minute kline not supported for index: {symbol}"
            )

        tc_sym = _tencent_symbol(code)
        mtype = _TF_MINUTE_TYPE.get(timeframe, "60")
        url = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"
        params = {"param": f"{tc_sym},m{mtype},,{n}"}

        try:
            resp = requests.get(
                url, params=params, headers={"User-Agent": _UA}, timeout=15
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            raise DataSourceTransientError(
                f"Tencent minute kline failed: {exc}"
            ) from exc

        # Parse: data.data.{tc_sym}.m{mtype} = [[date_time, open, close, high, low, volume], ...]
        stock_data = data.get("data", {}).get(tc_sym, {})
        key = f"m{mtype}"
        raw_klines = stock_data.get(key, [])
        if not raw_klines:
            # Try without 'm' prefix
            raw_klines = stock_data.get(mtype, [])

        rows: list[dict[str, Any]] = []
        for item in raw_klines:
            if len(item) < 6:
                continue
            ts = _kline_parse_date(item[0])
            rows.append({
                "ts_open": ts,
                "open": float(item[1]),
                "high": float(item[3]),
                "low": float(item[4]),
                "close": float(item[2]),
                "volume": float(item[5]),
                "amount": float(item[6]) if len(item) > 6 and item[6] else 0.0,
                "pct_chg": None,
            })

        # Fill pct_chg
        for i in range(1, len(rows)):
            prev_c = float(rows[i - 1]["close"])
            if prev_c > 0:
                rows[i]["pct_chg"] = (float(rows[i]["close"]) - prev_c) / prev_c * 100.0

        return rows

    @staticmethod
    def _parse_stock_kline(
        data: dict[str, Any], tc_sym: str, ktype: str, qfq_type: str
    ) -> list[dict[str, Any]]:
        """Parse Tencent stock daily kline response.

        Response structure:
        data.data.{tc_sym}.qfq{type} = [[date, open, close, high, low, volume, ...], ...]
        or data.data.{tc_sym}.{type} = [[...], ...] (unadjusted)

        Note: Tencent fields are strings: [date, open, close, high, low, volume]
        Amount is not always present in daily data.
        """
        stock_data = data.get("data", {}).get(tc_sym, {})

        # Try qfq first, then plain
        raw_klines = stock_data.get(qfq_type) or stock_data.get(ktype) or []

        def _safe_float_at(item: list | tuple, idx: int) -> float:
            if idx >= len(item):
                return 0.0
            v = item[idx]
            if isinstance(v, (int, float)):
                return float(v)
            if isinstance(v, str) and v:
                try:
                    return float(v)
                except ValueError:
                    return 0.0
            return 0.0

        rows: list[dict[str, Any]] = []
        for item in raw_klines:
            if not isinstance(item, (list, tuple)) or len(item) < 6:
                continue
            ts = _kline_parse_date(item[0])
            # Tencent order: [date, open, close, high, low, volume]
            rows.append({
                "ts_open": ts,
                "open": _safe_float_at(item, 1),
                "high": _safe_float_at(item, 3),
                "low": _safe_float_at(item, 4),
                "close": _safe_float_at(item, 2),
                "volume": _safe_float_at(item, 5),
                "amount": _safe_float_at(item, 6),
                "pct_chg": None,
            })

        # Fill pct_chg
        for i in range(1, len(rows)):
            prev_c = float(rows[i - 1]["close"])
            if prev_c > 0:
                rows[i]["pct_chg"] = (float(rows[i]["close"]) - prev_c) / prev_c * 100.0

        return rows

    @staticmethod
    def _parse_index_kline(
        data: dict[str, Any], idx_sym: str, ktype: str
    ) -> list[dict[str, Any]]:
        """Parse Tencent index daily kline response.

        Index response structure:
        data.data.{idx_sym}.{type} = [[date, open, close, high, low, volume, ...], ...]
        """
        stock_data = data.get("data", {}).get(idx_sym, {})
        raw_klines = stock_data.get(ktype, [])

        rows: list[dict[str, Any]] = []
        for item in raw_klines:
            if len(item) < 6:
                continue
            ts = _kline_parse_date(item[0])
            rows.append({
                "ts_open": ts,
                "open": float(item[1]),
                "high": float(item[3]),
                "low": float(item[4]),
                "close": float(item[2]),
                "volume": float(item[5]) if len(item) > 5 else 0.0,
                "amount": 0.0,
                "pct_chg": None,
            })

        for i in range(1, len(rows)):
            prev_c = float(rows[i - 1]["close"])
            if prev_c > 0:
                rows[i]["pct_chg"] = (float(rows[i]["close"]) - prev_c) / prev_c * 100.0

        return rows

    # ── Real-time quote for forming bar ───────────────────────────────────────

    def _apply_quote_to_forming(self, rows_asc: list[dict[str, Any]]) -> None:
        """Fetch real-time quote and update the forming bar's close price."""
        if not rows_asc or not self._symbol:
            return

        try:
            quote = self._fetch_realtime_quote(self._symbol)
        except Exception as exc:
            logger.debug("Tencent quote fetch failed: %s", exc)
            return

        if not quote:
            return

        code = normalize_ashare_symbol(self._symbol)
        if is_index_symbol(self._symbol):
            # For index, just update close
            rows_asc[-1]["close"] = quote.get("price", rows_asc[-1]["close"])
            return

        # Update forming bar with session data
        price = quote.get("price", 0)
        if price <= 0:
            return

        from pa_agent.data.ashare_common import apply_session_quote_to_forming_row

        apply_session_quote_to_forming_row(
            rows_asc[-1],
            price=price,
            open_=quote.get("open", 0),
            high=quote.get("high", 0),
            low=quote.get("low", 0),
            volume=quote.get("vol", 0) * 100,  # 手 → 股
            amount=quote.get("amount", 0),
            prev_close=quote.get("last_close", 0),
            daily=(self._timeframe in ("1d", "1w", "1M")),
            volume_lots=False,
            symbol=self._symbol,
        )

    def _fetch_realtime_quote(self, symbol: str) -> dict[str, Any]:
        """Fetch real-time quote from qt.gtimg.cn.

        Returns dict with: name, price, last_close, open, high, low,
                           vol (手), amount (万), pe_ttm, pb, mcap_yi, limit_up, limit_down
        """
        import requests

        code = normalize_ashare_symbol(symbol)
        if is_index_symbol(symbol):
            tc_sym = _index_symbol_for_api(symbol)
        else:
            tc_sym = _tencent_symbol(code)

        url = f"{_QUOTE_URL}{tc_sym}"
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": _UA},
                timeout=10,
            )
            resp.encoding = "gbk"
            text = resp.text
        except Exception as exc:
            raise DataSourceTransientError(
                f"Tencent quote HTTP failed: {exc}"
            ) from exc

        # Parse: v_sh600519="1~贵州茅台~600519~1700.00~1695.00~..."
        if "=" not in text or '"' not in text:
            return {}

        vals = text.split('"')[1].split("~")
        if len(vals) < 50:
            return {}

        def _safe_float(v: str, idx: int) -> float:
            try:
                return float(v) if v else 0.0
            except (ValueError, IndexError):
                return 0.0

        return {
            "name": vals[1],
            "code": code,
            "price": _safe_float(vals[3], 3),
            "last_close": _safe_float(vals[4], 4),
            "open": _safe_float(vals[5], 5),
            "vol": _safe_float(vals[6], 6),       # 成交量(手)
            "high": _safe_float(vals[33], 33),
            "low": _safe_float(vals[34], 34),
            "amount": _safe_float(vals[37], 37),   # 成交额(万)
            "pe_ttm": _safe_float(vals[39], 39),
            "mcap_yi": _safe_float(vals[44], 44),  # 总市值(亿)
            "float_mcap_yi": _safe_float(vals[45], 45),
            "pb": _safe_float(vals[46], 46),
            "limit_up": _safe_float(vals[47], 47),
            "limit_down": _safe_float(vals[48], 48),
            "vol_ratio": _safe_float(vals[49], 49),
        }
