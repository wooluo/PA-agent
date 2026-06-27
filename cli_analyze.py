#!/usr/bin/env python3
"""PA-agent CLI smoke test: AkShare → KlineFrame → two-stage AI analysis."""
import logging
import os
import sys

os.environ["NO_PROXY"] = "*"
os.environ.setdefault("TQDM_DISABLE", "1")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("pa_cli")

from pa_agent.config.paths import (
    SETTINGS_JSON_PATH,
    PROMPT_DIR,
    EXPERIENCE_DIR,
    RECORDS_PENDING_DIR,
)
from pa_agent.config.settings import load_settings, provider_api_key_configured
from pa_agent.data.factory import create_data_source
from pa_agent.data.snapshot import build_analysis_frame
from pa_agent.ai.deepseek_client import DeepSeekClient
from pa_agent.ai.prompt_assembler import PromptAssembler
from pa_agent.ai.router import route_strategy_files
from pa_agent.ai.json_validator import JsonValidator
from pa_agent.records.experience_reader import ExperienceReader
from pa_agent.records.pending_writer import PendingWriter
from pa_agent.util.event_bus import EventBus
from pa_agent.util.threading import CancelToken
from pa_agent.orchestrator.two_stage import TwoStageOrchestrator


def main():
    symbol = sys.argv[1] if len(sys.argv) > 1 else "600519"
    timeframe = sys.argv[2] if len(sys.argv) > 2 else "1d"
    data_source = sys.argv[3] if len(sys.argv) > 3 else "akshare"

    log.info("=== PA-agent CLI Analysis ===")
    log.info("Symbol: %s | TF: %s | Source: %s", symbol, timeframe, data_source)

    # ── Load config ──
    settings = load_settings(SETTINGS_JSON_PATH)
    if not provider_api_key_configured(settings):
        log.error("API key not configured in settings.json")
        sys.exit(1)
    log.info("Provider: model=%s base_url=%s", settings.provider.model, settings.provider.base_url)

    # ── Fetch K-line ──
    ds = create_data_source(data_source)
    ds.connect()
    ds.subscribe(symbol, timeframe)
    bars = ds.latest_snapshot(180)
    log.info("Fetched %d bars (newest close=%.2f)", len(bars), bars[0].close)

    # ── Build frame ──
    frame = build_analysis_frame(bars, settings.general.analysis_bar_count, symbol, timeframe)
    log.info("Frame: %d bars, ema20[-1]=%.2f, atr14[-1]=%.2f",
             len(frame.bars),
             frame.indicators.ema20[-1] if frame.indicators.ema20 else 0,
             frame.indicators.atr14[-1] if frame.indicators.atr14 else 0)

    # ── Assemble orchestrator ──
    exp_reader = ExperienceReader(EXPERIENCE_DIR, logger=log)
    assembler = PromptAssembler(PROMPT_DIR, exp_reader, prompt_settings=settings.prompt)
    client = DeepSeekClient(settings=settings.provider, logger_=log)
    validator = JsonValidator(settings.validation)
    pending_writer = PendingWriter(RECORDS_PENDING_DIR, EventBus(), settings.provider.api_key)

    orch = TwoStageOrchestrator(
        client, assembler, route_strategy_files,
        validator, pending_writer, exp_reader, settings,
    )

    # ── Two-stage analysis ──
    def on_event(e):
        log.info("  >> event: %s", e.name)

    log.info("=== Starting two-stage analysis ===")
    record = orch.submit(frame, CancelToken(), on_event)

    # ── Results ──
    import json as _json

    log.info("=== Results ===")

    s1 = record.stage1_diagnosis or {}
    gate = s1.get("gate_result", {})
    gate_verdict = gate.get("verdict", "?") if isinstance(gate, dict) else str(gate)
    log.info("Stage1 gate: %s", gate_verdict)
    diag = s1.get("market_diagnosis", {})
    bias = diag.get("bias", "?") if isinstance(diag, dict) else "?"
    log.info("Stage1 bias: %s", bias)

    s2 = record.stage2_decision or {}
    decision = s2.get("decision", {})
    action = decision.get("action", "?") if isinstance(decision, dict) else str(decision)
    conf = decision.get("confidence", "?") if isinstance(decision, dict) else "?"
    log.info("Stage2 action: %s | confidence: %s", action, conf)

    # Print full JSON
    print("\n" + "=" * 60)
    print("Stage1 Diagnosis:")
    print(_json.dumps(s1, indent=2, ensure_ascii=False)[:8000])
    print("\n" + "=" * 60)
    print("Stage2 Decision:")
    print(_json.dumps(s2, indent=2, ensure_ascii=False)[:8000])

    # ── Generate chart PNG ──────────────────────────────────────────────
    chart_path = None
    stock_name = ""
    try:
        from pathlib import Path
        from datetime import datetime
        from pa_agent.records.trade_logger import _render_chart, _parse_sr_price
        from pa_agent.records.symbol_name_resolver import resolve_stock_name

        # Resolve stock name first (needed for chart title)
        stock_name = ""
        try:
            stock_name = resolve_stock_name(symbol) or ""
        except Exception:
            pass

        chart_dir = Path("/tmp/pa_charts")
        chart_dir.mkdir(parents=True, exist_ok=True)
        ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        chart_file = chart_dir / f"{symbol}_{timeframe}_{ts_str}.png"

        decision = s2.get("decision", {}) if isinstance(s2, dict) else {}
        bars_list = list(getattr(frame, "bars", []))
        ema20_vals = list(getattr(frame.indicators, "ema20", []) or [])

        # Build support/resistance list from Stage1
        sr_list = []
        s1_diag = s1 if isinstance(s1, dict) else {}
        for p in s1_diag.get("support_levels", []):
            pv = _parse_sr_price(p)
            if pv:
                sr_list.append((pv, "support", "支撑"))
        for p in s1_diag.get("resistance_levels", []):
            pv = _parse_sr_price(p)
            if pv:
                sr_list.append((pv, "resistance", "阻力"))

        ok = _render_chart(
            bars_newest_first=bars_list,
            ema20_newest_first=ema20_vals,
            symbol=symbol,
            timeframe=timeframe,
            image_path=chart_file,
            entry_price=_parse_sr_price(decision.get("entry_price")),
            stop_loss_price=_parse_sr_price(decision.get("stop_loss_price")),
            take_profit_price=_parse_sr_price(decision.get("take_profit_price")),
            take_profit_price_2=_parse_sr_price(decision.get("take_profit_price_2")),
            order_direction=str(decision.get("order_direction") or ""),
            order_type=str(decision.get("order_type") or ""),
            diagnosis_confidence=str(decision.get("diagnosis_confidence") or ""),
            trade_confidence=str(decision.get("trade_confidence") or ""),
            estimated_win_rate=str(decision.get("estimated_win_rate") or ""),
            support_resistance=sr_list if sr_list else None,
            stock_name=stock_name,
        )
        if ok:
            chart_path = str(chart_file)
            # Print as machine-parseable last line
            print(f"\n[CHART]{chart_path}[/CHART]")
            log.info("Chart saved: %s", chart_path)
    except Exception as exc:
        log.warning("Chart generation failed: %s", exc)

    # ── Resolve stock name (print tag) ──────────────────────────────────
    try:
        if not stock_name:
            from pa_agent.records.symbol_name_resolver import resolve_stock_name
            stock_name = resolve_stock_name(symbol) or ""
        if stock_name:
            print(f"[NAME]{stock_name}[/NAME]")
    except Exception:
        pass

    if record.exception:
        log.warning("Exception: %s", record.exception)

    ds.disconnect()
    log.info("=== Done ===")


if __name__ == "__main__":
    main()
