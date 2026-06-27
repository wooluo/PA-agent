"""Tests for symbol_name_resolver — name lookup with caching and graceful failure.

All network access is monkeypatched so tests stay offline and deterministic.
"""
from __future__ import annotations

import pa_agent.records.symbol_name_resolver as mod
from pa_agent.records.symbol_name_resolver import resolve_stock_name


def setup_function() -> None:
    mod.clear_cache()


def test_ashare_name_from_quote_payload(monkeypatch):
    """A-share code resolved via East Money quote payload f58."""

    def fake_quote(symbol):
        return {"f58": "川润股份"}

    monkeypatch.setattr(
        "pa_agent.data.eastmoney_client.fetch_stock_quote_payload", fake_quote
    )
    assert resolve_stock_name("002272") == "川润股份"


def test_ashare_falls_back_to_spot_row(monkeypatch):
    """When quote payload is empty, the spot row's name is used."""

    def fake_quote(symbol):
        return None

    def fake_spot(symbol):
        return {"name": "赛力斯"}

    monkeypatch.setattr(
        "pa_agent.data.eastmoney_client.fetch_stock_quote_payload", fake_quote
    )
    monkeypatch.setattr(
        "pa_agent.data.eastmoney_client.fetch_stock_spot_row", fake_spot
    )
    assert resolve_stock_name("601127") == "赛力斯"


def test_empty_string_for_unknown_symbol(monkeypatch):
    monkeypatch.setattr(
        "pa_agent.data.eastmoney_client.fetch_stock_quote_payload", lambda s: None
    )
    monkeypatch.setattr(
        "pa_agent.data.eastmoney_client.fetch_stock_spot_row", lambda s: None
    )
    assert resolve_stock_name("999999") == ""


def test_network_exception_returns_empty(monkeypatch):
    def boom(symbol):
        raise RuntimeError("network down")

    monkeypatch.setattr(
        "pa_agent.data.eastmoney_client.fetch_stock_quote_payload", boom
    )
    monkeypatch.setattr(
        "pa_agent.data.eastmoney_client.fetch_stock_spot_row", boom
    )
    assert resolve_stock_name("002272") == ""


def test_cache_hits_avoid_second_network_call(monkeypatch):
    calls = {"n": 0}

    def fake_quote(symbol):
        calls["n"] += 1
        return {"f58": "四方达"}

    monkeypatch.setattr(
        "pa_agent.data.eastmoney_client.fetch_stock_quote_payload", fake_quote
    )
    assert resolve_stock_name("300179") == "四方达"
    assert resolve_stock_name("300179") == "四方达"  # cached
    assert calls["n"] == 1


def test_empty_symbol_returns_empty():
    assert resolve_stock_name("") == ""


def test_hk_symbol_uses_alias_reverse_lookup():
    # HK code 1810 → 小米 via the local alias table (no network)
    assert resolve_stock_name("1810") == "小米"
