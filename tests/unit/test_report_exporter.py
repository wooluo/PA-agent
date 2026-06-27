"""Tests for the (slimmed-down, conclusion-only) report exporter.

Covers: header shows stock name when given; K-line image reference inserted only
when chart_png_name is passed; the removed evidence sections (置信度/市场诊断
依据/决策依据/风险与观察) no longer appear; conclusion table + decision reason +
推演结论/附录 are preserved. No network — exporter never resolves names itself.
"""
from __future__ import annotations

from pa_agent.records.report_exporter import export_report_md
from pa_agent.records.schema import AnalysisRecord, RecordMeta


def _record(*, has_order: bool = True, reasoning: str = "看多突破") -> AnalysisRecord:
    decision = (
        {
            "order_type": "限价单",
            "order_direction": "做多",
            "entry_price": 10.0,
            "stop_loss_price": 9.0,
            "take_profit_price": 12.0,
            "take_profit_price_2": 13.0,
            "diagnosis_confidence": 70,
            "trade_confidence": 60,
            "estimated_win_rate": 55,
            "reasoning": reasoning,
            "key_factors": ["因子A"],
            "risk_assessment": "高风险",
        }
        if has_order
        else {"order_type": "不下单", "reasoning": reasoning}
    )
    return AnalysisRecord(
        meta=RecordMeta(
            timestamp_local_iso="2026-06-27T10:00:00.000",
            timestamp_local_ms=1_750_000_000_000,
            symbol="002272",
            timeframe="1d",
            bar_count=3,
            ai_provider={"model": "test-model"},
            decision_stance="balanced",
        ),
        kline_data=[
            {"seq": 0, "ts_open": 1, "open": 10, "high": 11, "low": 9.5,
             "close": 10.5, "volume": 1, "closed": True}
        ],
        htf_text="",
        stage1_messages=[],
        stage1_response=None,
        stage1_diagnosis={"support_levels": ["9.0"], "resistance_levels": ["12.0"]},
        stage2_messages=[],
        stage2_response=None,
        stage2_decision={
            "decision": decision,
            "next_bar_prediction": {"direction": "bullish", "reasoning": "r"},
            "next_cycle_prediction": {"cycle": "normal_channel", "direction": "bullish"},
        },
        strategy_files_used=["s1.txt"],
        experience_loaded=[{"f": "e"}],
        exception=None,
        usage_total={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    )


def test_header_shows_name_and_code_when_name_given():
    md = export_report_md(_record(), stock_name="川润股份")
    assert "# 川润股份（002272） 1d 分析报告" in md


def test_header_shows_code_only_when_name_missing():
    md = export_report_md(_record(), stock_name=None)
    assert "# 002272 1d 分析报告" in md
    assert "（002272）" not in md


def test_chart_reference_inserted_when_png_name_given():
    md = export_report_md(_record(), chart_png_name="report.png")
    assert "![K线分析图](report.png)" in md


def test_chart_reference_absent_when_png_name_omitted():
    md = export_report_md(_record())
    assert "K线分析图" not in md


def test_conclusion_table_and_reasoning_present_when_order():
    md = export_report_md(_record(), stock_name="川润股份")
    assert "| 操作类型 | 限价单 |" in md
    assert "| 止盈价(TP1)" in md
    assert "| 止损价" in md
    assert "| 盈亏比 |" in md
    assert "**决策理由**：看多突破" in md


def test_no_order_shows_concise_conclusion_without_table():
    md = export_report_md(_record(has_order=False), stock_name="川润股份")
    assert "**操作类型**：不下单" in md
    # No empty 6-row table when not ordering
    assert "| 操作类型 |" not in md


def test_removed_evidence_sections_are_gone():
    md = export_report_md(_record())
    assert "## 置信度" not in md
    assert "## 市场诊断依据" not in md
    assert "## 决策依据" not in md
    assert "## 风险与观察" not in md
    # key_factors / risk_assessment are evidence — must not leak into the report
    assert "因子A" not in md
    assert "高风险" not in md


def test_kept_sections_present_and_renumbered():
    md = export_report_md(_record())
    assert "## 一、分析结论" in md
    assert "## 二、推演结论" in md
    assert "## 三、附录" in md
    assert "### 下一根K线" in md
    assert "### 下一市场周期" in md
    assert "Token 用量" in md
