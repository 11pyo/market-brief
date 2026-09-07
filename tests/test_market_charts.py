"""차트 수집(get_all_charts)의 부분 결과 보존 테스트 — 네트워크 호출 없음.

회귀로 고정하는 것: 전체 타임아웃이 걸려도 **이미 끝난 차트는 버리지 않는다.**
(gather를 asyncio.timeout으로 감싸면 완료분까지 전부 폐기되어 사이트 차트가 통째로 비었다.)
"""
from __future__ import annotations

import asyncio
import time

import pytest

from mbrief.sources import market_data

CANDLES = [{"time": 1, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5}]


@pytest.fixture
def fast_timeout(monkeypatch):
    monkeypatch.setattr(market_data, "_CHART_TOTAL_TIMEOUT", 0.6)
    monkeypatch.setattr(market_data, "_CHART_MAX_WORKERS", 8)
    return None


def test_all_charts_collected_when_every_fetch_succeeds(monkeypatch):
    monkeypatch.setattr(market_data, "_fetch_chart_sync", lambda symbol, period: list(CANDLES))
    charts = asyncio.run(market_data.get_all_charts())
    expected = len(market_data.SYMBOLS) * len(market_data.PERIODS)
    assert len(charts) == expected
    assert charts["sp500_1d"] == CANDLES


def test_individual_failures_do_not_stop_collection(monkeypatch):
    def fetch(symbol, period):
        if symbol == "^GSPC":
            raise RuntimeError("yfinance 오류")
        return list(CANDLES)

    monkeypatch.setattr(market_data, "_fetch_chart_sync", fetch)
    charts = asyncio.run(market_data.get_all_charts())
    assert "sp500_1d" not in charts
    assert charts["nasdaq_1d"] == CANDLES


def test_empty_results_are_not_stored(monkeypatch):
    monkeypatch.setattr(market_data, "_fetch_chart_sync", lambda symbol, period: [])
    assert asyncio.run(market_data.get_all_charts()) == {}


def test_timeout_keeps_completed_charts(monkeypatch, fast_timeout):
    """느린 심볼 하나 때문에 나머지 완료분을 통째로 버리면 안 된다."""
    def fetch(symbol, period):
        if symbol == "^GSPC":
            time.sleep(5)  # 전체 타임아웃(0.6s)을 넘긴다
        return list(CANDLES)

    monkeypatch.setattr(market_data, "_fetch_chart_sync", fetch)
    charts = asyncio.run(market_data.get_all_charts())

    assert charts, "타임아웃이 걸려도 완료된 차트는 남아야 한다"
    assert "nasdaq_1d" in charts
    assert not any(key.startswith("sp500_") for key in charts)


# --------------------------------------------------
# Security Checklist
# Applied:
#   - Infinite Loop: 전체 수집 타임아웃이 실제로 동작하고, 초과 시 미완 작업만 취소되는지 검증
#   - Missing Error Handling: 개별 심볼 실패가 전체 수집을 중단시키지 않는지 검증
#   - Improper Exception Handling: 실패한 작업이 조용히 성공으로 집계되지 않는지 검증
# Not Applied:
#   - [WARN] 실제 yfinance 호출은 하지 않는다 (네트워크 의존). _fetch_chart_sync를 대체해 검증한다.
# --------------------------------------------------
