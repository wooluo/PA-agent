"""Export an AnalysisRecord to a human-readable Markdown report.

报告是**纯结论报告**：分析结论（含一句决策理由）+ K 线分析图 + 推演结论 + 附录。
文字版「置信度 / 市场诊断依据 / 决策依据 / 风险与观察」等 debug 依据不再展示——
K 线图已含 Entry/TP1/TP2/SL 与支撑/阻力位全部标线，看图 + 结论表即可。

所有字段渲染均容忍缺失/None（不下单、next_bar 未启用、分析失败等情况）。
中文格式化复用 cycle_enums / prediction_format，保证与 UI 一致。
"""
from __future__ import annotations

from typing import Any

from pa_agent.ai.cycle_enums import (
    CYCLE_ORDER,
    CYCLE_POSITION_ZH,
    format_cycle_position,
    format_cycle_with_direction,
)
from pa_agent.gui.prediction_format import _format_prediction_probs_line

_DIRECTION_ZH: dict[str, str] = {
    "bullish": "上涨",
    "bearish": "下跌",
    "neutral": "震荡",
    "long": "做多",
    "short": "做空",
}

_STANCE_ZH: dict[str, str] = {
    "conservative": "保守",
    "balanced": "平衡",
    "aggressive": "激进",
    "extreme_aggressive": "极端激进",
}


def _fmt(v: Any) -> str:
    """Render a scalar as '—' for None/empty, else str(v)."""
    if v is None or v == "":
        return "—"
    return str(v)


def _direction_zh(v: Any) -> str:
    if v is None or v == "":
        return "—"
    return _DIRECTION_ZH.get(str(v).strip().lower(), str(v))


def _risk_reward_summary(decision: dict, entry: float | None) -> str:
    """算盈亏比汇总：'风险 3.10 / 回报 4.65 / RR 1.50'。方向相关。

    做多：风险=入场-止损，回报=止盈-入场；做空相反。
    任一价格缺失则返回空串。
    """
    if entry is None or entry == 0:
        return ""
    stop = decision.get("stop_loss_price")
    tp = decision.get("take_profit_price") or decision.get("take_profit_price_2")
    direction = str(decision.get("order_direction") or "").strip().lower()
    try:
        entry_f = float(entry)
        stop_f = float(stop) if stop not in (None, "") else None
        tp_f = float(tp) if tp not in (None, "") else None
    except (TypeError, ValueError):
        return ""
    if stop_f is None or tp_f is None:
        return ""
    if direction == "做空":
        risk = stop_f - entry_f
        reward = entry_f - tp_f
    else:  # 做多（默认）
        risk = entry_f - stop_f
        reward = tp_f - entry_f
    if risk <= 0 or reward <= 0:
        return ""
    rr = reward / risk
    return f"风险 {risk:.2f} / 回报 {reward:.2f} / RR {rr:.2f}"


def export_report_md(
    record: Any,
    *,
    stock_name: str | None = None,
    chart_png_name: str | None = None,
) -> str:
    """Convert an AnalysisRecord to a Markdown report string.

    ``record`` may be an AnalysisRecord (Pydantic) or a plain dict; both expose
    the same field names (meta, stage1_diagnosis, stage2_decision, usage_total,
    strategy_files_used, experience_loaded, exception, kline_data).

    Parameters
    ----------
    stock_name:
        Optional display name (e.g. "川润股份") to prepend to the header.
        When provided the header reads "名称（代码）"; otherwise just the code.
    chart_png_name:
        Optional PNG filename (same dir as the .md) to embed after the
        conclusion table. The PNG is expected to contain K-line + Entry/TP1/
        TP2/SL + support/resistance lines.
    """
    # 统一取值：支持 Pydantic model 与 dict
    def get(name: str, default: Any = None) -> Any:
        if hasattr(record, name):
            return getattr(record, name, default)
        return record.get(name, default) if isinstance(record, dict) else default

    meta = get("meta") or {}
    if hasattr(meta, "model_dump"):
        meta = meta.model_dump()
    s2 = get("stage2_decision") or {}
    if hasattr(s2, "model_dump"):
        s2 = s2.model_dump()
    usage = get("usage_total") or {}
    strategy_files = get("strategy_files_used") or []
    experience = get("experience_loaded") or []
    exception = get("exception")

    decision = s2.get("decision") or {}
    terminal = s2.get("terminal") or {}
    next_bar = s2.get("next_bar_prediction")
    next_cycle = s2.get("next_cycle_prediction")

    symbol = meta.get("symbol", "?")
    timeframe = meta.get("timeframe", "?")
    ts_iso = meta.get("timestamp_local_iso", "—")
    stance = _STANCE_ZH.get(str(meta.get("decision_stance", "")), "—")
    bar_count = meta.get("bar_count", "—")
    provider = meta.get("ai_provider") or {}
    model_name = provider.get("model", "—")

    # 当前价 = K1 收盘价（kline_data 是 newest-first）
    kline_data = get("kline_data") or []
    current_price: float | None = None
    if isinstance(kline_data, list) and kline_data:
        k1 = kline_data[0]
        if isinstance(k1, dict):
            cp = k1.get("close")
            try:
                current_price = float(cp) if cp is not None else None
            except (TypeError, ValueError):
                current_price = None

    parts: list[str] = []

    # ── 报告头 ──────────────────────────────────────────────────────────────
    title = f"{stock_name}（{symbol}）" if stock_name else symbol
    parts.append(f"# {title} {timeframe} 分析报告\n")
    price_str = f" · 当前价：{current_price}" if current_price is not None else ""
    parts.append(
        f"> 生成时间：{ts_iso} · 决策立场：{stance} · K线数：{bar_count}{price_str} · 模型：{model_name}\n"
    )

    # ── 一、分析结论 ────────────────────────────────────────────────────────
    parts.append("## 一、分析结论\n")
    order_type = _fmt(decision.get("order_type"))
    has_order = order_type not in ("—", "不下单", "无")
    if has_order:
        # 有订单：显示完整交易参数表（价格带涨跌幅，便于直观判断空间）
        def _price_with_pct(price_val: Any, base: float | None) -> str:
            """价格 + 相对入场价的涨跌幅，如 '24.15 (+23.8%)'。"""
            p = _fmt(price_val)
            if price_val in (None, "") or base is None or base == 0:
                return p
            try:
                pct = (float(price_val) - base) / base * 100.0
                sign = "+" if pct >= 0 else ""
                return f"{p} ({sign}{pct:.1f}%)"
            except (TypeError, ValueError):
                return p

        entry = decision.get("entry_price")
        entry_f: float | None = None
        try:
            entry_f = float(entry) if entry not in (None, "") else None
        except (TypeError, ValueError):
            entry_f = None

        parts.append("| 项目 | 内容 |")
        parts.append("|------|------|")
        parts.append(f"| 操作类型 | {order_type} |")
        parts.append(f"| 方向 | {_direction_zh(decision.get('order_direction'))} |")
        if current_price is not None:
            parts.append(f"| 当前价 | {current_price} |")
        parts.append(f"| 入场价 | {_fmt(entry)} |")
        parts.append(f"| 止盈价(TP1) | {_price_with_pct(decision.get('take_profit_price'), entry_f)} |")
        parts.append(f"| 止盈价(TP2) | {_price_with_pct(decision.get('take_profit_price_2'), entry_f)} |")
        parts.append(f"| 止损价 | {_price_with_pct(decision.get('stop_loss_price'), entry_f)} |")

        # 盈亏比汇总（风险/回报/RR）
        rr_line = _risk_reward_summary(decision, entry_f)
        if rr_line:
            parts.append(f"| 盈亏比 | {rr_line} |")
        parts.append("")
    else:
        # 不下单：单行结论，不渲染 6 行空表
        parts.append(f"**操作类型**：{order_type}\n")
    if decision.get("reasoning"):
        parts.append(f"**决策理由**：{decision['reasoning']}\n")
    if terminal.get("label"):
        parts.append(f"**决策终局**：{terminal.get('label')}（outcome={_fmt(terminal.get('outcome'))}）\n")

    # ── K 线分析图（含 Entry/TP1/TP2/SL + 支撑/阻力位全部标线） ───────────────
    if chart_png_name:
        parts.append(f"![K线分析图]({chart_png_name})\n")

    # ── 二、推演结论 ────────────────────────────────────────────────────────
    parts.append("## 二、推演结论\n")
    # 下一根K线推演（可能未启用 → 缺失）
    if next_bar:
        _append_next_bar(parts, next_bar)
    else:
        parts.append("_（下一根K线推演未启用）_\n")
    # 下一市场周期推演
    if next_cycle:
        _append_next_cycle(parts, next_cycle)
    else:
        parts.append("_（下一市场周期推演未生成）_\n")
    parts.append("")

    # ── 三、附录 ────────────────────────────────────────────────────────────
    parts.append("## 三、附录\n")
    prompt_t = usage.get("prompt_tokens", 0)
    completion_t = usage.get("completion_tokens", 0)
    cached_t = usage.get("cached_prompt_tokens", 0)
    total_t = usage.get("total_tokens", 0) or (prompt_t + completion_t)
    parts.append(f"- Token 用量：prompt={prompt_t:,} · completion={completion_t:,} · cached={cached_t:,} · total={total_t:,}")
    if strategy_files:
        parts.append(f"- 策略文件：{', '.join(strategy_files)}")
    parts.append(f"- 经验库条目：{len(experience)} 条")
    if exception:
        parts.append(f"- ⚠️ 异常：[{_fmt(exception.get('category'))}] {_fmt(exception.get('message'))}")
    parts.append("")
    parts.append("---")
    parts.append("_本报告由 PA Agent 自动生成，仅供研究参考，不构成投资建议。_")

    return "\n".join(parts)


def _append_next_bar(parts: list[str], nb: dict) -> None:
    """渲染下一根K线推演 section。"""
    parts.append("### 下一根K线")
    if nb.get("unpredictable"):
        parts.append("**结论**：不可预测\n")
        return
    d = nb.get("direction")
    probs = nb.get("probabilities") or {}
    if d:
        parts.append(f"**方向**：{_direction_zh(d)}")
    if probs:
        parts.append(f"**概率**：{_format_prediction_probs_line(probs)}")
    if nb.get("reasoning"):
        parts.append(f"\n**推演理由**：{nb['reasoning']}")
    parts.append("")


def _append_next_cycle(parts: list[str], nc: dict) -> None:
    """渲染下一市场周期推演 section。"""
    parts.append("### 下一市场周期")
    if nc.get("unpredictable"):
        parts.append("**结论**：不可预测\n")
        return
    cyc = nc.get("cycle")
    d = nc.get("direction")
    if cyc and d:
        parts.append(f"**周期**：{format_cycle_with_direction(cyc, d)}")
    elif cyc:
        parts.append(f"**周期**：{format_cycle_position(cyc)}")
    # 概率分布（8 项，Top-3 强调）
    probs = nc.get("probabilities") or {}
    if probs:
        items = []
        for k in CYCLE_ORDER:
            v = probs.get(k)
            if v is not None and v != "":
                items.append((k, v))
        if items:
            items.sort(key=lambda x: float(x[1]) if str(x[1]).replace(".", "").isdigit() else 0, reverse=True)
            top3 = items[:3]
            prob_str = "  ·  ".join(f"{CYCLE_POSITION_ZH.get(k, k)} {v}%" for k, v in top3)
            parts.append(f"**概率分布（Top-3）**：{prob_str}")
    if nc.get("reasoning"):
        parts.append(f"\n**推演理由**：{nc['reasoning']}")
    parts.append("")
