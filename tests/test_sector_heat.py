"""섹터 기술적 과열도 테스트 — 합성 가격 시계열만 사용한다 (네트워크 호출 없음).

고정하려는 계약:
  1. 과열도는 **가격에서 파이썬이 계산**한다. LLM이 판정하지 않는다.
  2. 라벨은 gapPercentile 하나로만 결정된다.
  3. 표본이 모자라면 라벨은 'neutral'이지만 그것은 '중립'이 아니라 '모름'이며,
     sampleDays=0이 유일한 구분자다.
  4. 심볼 하나가 실패해도 나머지 과열도는 살아남는다.
"""
from __future__ import annotations

import asyncio
import math

import pytest

from mbrief import indicators
from mbrief.sources import market_data

BENCH = market_data.SECTOR_BENCHMARK


# ===== 합성 시계열 헬퍼 =====
def flat_then(move: float, days: int = 300, tail: int = 10, base: float = 100.0) -> list[float]:
    """앞은 완전 평평, 마지막 tail일에 move(비율)만큼 직선 이동하는 종가 시계열.

    평평한 구간의 이격도는 0이므로, 마지막 급등/급락이 자기 분포의 최댓값/최솟값이 된다.
    """
    closes = [base] * (days - tail)
    for step in range(1, tail + 1):
        closes.append(base * (1.0 + move * step / tail))
    return closes


def sine_wave(days: int = 300, amp: float = 0.05, period: int = 50, base: float = 100.0) -> list[float]:
    """주기가 200일 창을 정수배로 나누는 사인파 — 이격도가 분포 한가운데 놓인다."""
    return [base * (1.0 + amp * math.sin(2 * math.pi * i / period)) for i in range(days)]


def constant(days: int = 300, base: float = 100.0) -> list[float]:
    return [base] * days


# ===== 심볼 목록 =====
def test_sector_symbols_cover_all_eleven_spdr_sectors():
    assert len(market_data.SECTOR_SYMBOLS) == 11
    assert market_data.SECTOR_SYMBOLS["Utilities"] == "XLU"
    assert len(set(market_data.SECTOR_SYMBOLS.values())) == 11


def test_benchmark_is_the_sp500_index():
    assert BENCH == "^GSPC"
    assert BENCH not in market_data.SECTOR_SYMBOLS.values()


# ===== 라벨 판정 =====
def test_strong_uptrend_is_hot():
    heat = market_data.build_sector_heat({"XLK": flat_then(0.20), BENCH: constant()})["Technology"]
    assert heat.label == "hot"
    assert heat.gapPercentile == pytest.approx(100.0)
    assert heat.gapPct > 0
    assert heat.sampleDays > 0


def test_downtrend_is_cold():
    heat = market_data.build_sector_heat({"XLE": flat_then(-0.20), BENCH: constant()})["Energy"]
    assert heat.label == "cold"
    assert heat.gapPercentile is not None and heat.gapPercentile < indicators.HEAT_THRESHOLDS[-1][1]
    assert heat.gapPct < 0


def test_oscillating_series_lands_in_the_middle():
    heat = market_data.build_sector_heat({"XLF": sine_wave(), BENCH: constant()})["Financials"]
    assert heat.label == "neutral"
    assert 35.0 <= heat.gapPercentile < 65.0
    assert heat.sampleDays > 0        # '모름'이 아니라 실제로 중립이다


def test_insufficient_history_reports_sample_days_zero_and_neutral():
    """표본이 모자라면 백분위를 채우지 않는다 — 라벨은 neutral이지만 sampleDays=0이 '모름'을 알린다."""
    short = flat_then(0.20, days=250)   # 250일 - 200일 MA → 51개 관측 (MIN_GAP_SAMPLE_DAYS 미만)
    heat = market_data.build_sector_heat({"XLK": short, BENCH: constant(250)})["Technology"]
    assert heat.gapPercentile is None
    assert heat.label == "neutral"
    assert heat.sampleDays == 0
    # 급등 자체는 계산됐다 - 모르는 것은 '분포 안에서의 위치'뿐이다.
    assert heat.gapPct == pytest.approx(19.34, abs=0.05)


def test_series_shorter_than_the_moving_average_has_no_gap_at_all():
    heat = market_data.build_sector_heat({"XLK": constant(50), BENCH: constant(50)})["Technology"]
    assert heat.gapPct is None
    assert heat.gapPercentile is None
    assert heat.label == "neutral"
    assert heat.sampleDays == 0


def test_min_gap_sample_days_is_the_threshold_that_fills_the_percentile():
    need = market_data.MIN_GAP_SAMPLE_DAYS
    just_enough = flat_then(0.20, days=indicators.MA_WINDOW + need - 1)
    one_short = flat_then(0.20, days=indicators.MA_WINDOW + need - 2)

    assert market_data.build_sector_heat({"XLK": just_enough})["Technology"].sampleDays == need
    assert market_data.build_sector_heat({"XLK": one_short})["Technology"].sampleDays == 0


# ===== 개별 수치 =====
def test_sample_days_equals_the_number_of_gap_observations():
    closes = flat_then(0.20, days=300)
    heat = market_data.build_sector_heat({"XLK": closes})["Technology"]
    assert heat.sampleDays == len(indicators.ma_gap_series(closes, indicators.MA_WINDOW))
    assert heat.sampleDays == 101   # 300 - 200 + 1


def test_rsi_is_taken_from_daily_closes():
    heat = market_data.build_sector_heat({"XLK": flat_then(0.20)})["Technology"]
    assert heat.rsi14 == pytest.approx(100.0)   # 마지막 구간이 전부 상승


def test_rel3m_is_the_excess_return_over_the_benchmark_in_points():
    lookback = market_data.REL_LOOKBACK_DAYS
    sector = [100.0] * 300
    bench = [100.0] * 300
    sector[-1] = 110.0   # 63거래일 전 대비 +10%
    bench[-1] = 104.0    # 기준 지수는 +4%
    # pct_return은 lookback일 전 종가와 비교한다 - 위 두 값이 그 구간의 시작/끝이다.
    assert indicators.pct_return(sector, lookback) == pytest.approx(10.0)

    heat = market_data.build_sector_heat({"XLK": sector, BENCH: bench})["Technology"]
    assert heat.rel3m == pytest.approx(6.0, abs=0.01)


def test_rel3m_is_none_without_a_benchmark_series():
    heat = market_data.build_sector_heat({"XLK": flat_then(0.20)})["Technology"]
    assert heat.rel3m is None
    assert heat.label == "hot"        # 상대강도가 없어도 판정은 이격도만으로 성립한다


def test_label_never_depends_on_rsi_or_relative_strength():
    """RSI 100 + 상대강도 압도적이어도, 이격도 백분위가 중간이면 라벨은 neutral이다."""
    closes = sine_wave()
    heat_alone = market_data.build_sector_heat({"XLF": closes})["Financials"]
    heat_with_weak_bench = market_data.build_sector_heat(
        {"XLF": closes, BENCH: [100.0] * 299 + [50.0]},   # 기준 지수 반토막 → rel3m 급등
    )["Financials"]
    assert heat_alone.label == heat_with_weak_bench.label == "neutral"
    assert heat_with_weak_bench.rel3m > 40


# ===== 결측·부분 결과 =====
def test_missing_symbols_are_omitted_but_the_rest_survive():
    heat = market_data.build_sector_heat({"XLK": flat_then(0.20), BENCH: constant()})
    assert set(heat) == {"Technology"}
    assert "Energy" not in heat


def test_empty_history_returns_an_empty_mapping():
    assert market_data.build_sector_heat({}) == {}
    assert market_data.build_sector_heat(None) == {}


def test_all_eleven_sectors_are_built_when_every_series_is_present():
    history = {symbol: flat_then(0.05) for symbol in market_data.SECTOR_SYMBOLS.values()}
    history[BENCH] = constant()
    heat = market_data.build_sector_heat(history)
    assert set(heat) == set(market_data.SECTOR_SYMBOLS)
    assert all(item.sampleDays == 101 for item in heat.values())


def test_non_finite_closes_do_not_break_the_calculation():
    closes = flat_then(0.20)
    dirty = closes[:150] + [float("nan"), None] + closes[150:]   # type: ignore[list-item]
    heat = market_data.build_sector_heat({"XLK": dirty, BENCH: constant()})["Technology"]
    assert heat.label == "hot"
    assert math.isfinite(heat.gapPct)


# ===== 수집 (get_sector_history) =====
def test_get_sector_history_fetches_every_sector_plus_the_benchmark(monkeypatch):
    seen: list[tuple[str, str]] = []

    def fetch(symbol, period):
        seen.append((symbol, period))
        return [1.0, 2.0]

    monkeypatch.setattr(market_data, "_fetch_history_sync", fetch)
    history = asyncio.run(market_data.get_sector_history())

    assert set(history) == set(market_data.SECTOR_SYMBOLS.values()) | {BENCH}
    assert len(seen) == 12                       # 11 섹터 + ^GSPC, 중복 호출 없음
    assert {period for _, period in seen} == {market_data.HISTORY_PERIOD}


def test_get_sector_history_isolates_individual_failures(monkeypatch):
    def fetch(symbol, period):
        if symbol == "XLK":
            raise RuntimeError("yfinance 오류")
        return [1.0, 2.0]

    monkeypatch.setattr(market_data, "_fetch_history_sync", fetch)
    history = asyncio.run(market_data.get_sector_history())

    assert "XLK" not in history                  # 실패한 심볼만 빠진다
    assert BENCH in history and "XLE" in history
    assert len(history) == 11


def test_get_sector_history_drops_empty_series(monkeypatch):
    monkeypatch.setattr(market_data, "_fetch_history_sync", lambda symbol, period: [])
    assert asyncio.run(market_data.get_sector_history()) == {}


def test_get_sector_history_rejects_an_unlisted_period(monkeypatch):
    """[SECURE] 화이트리스트 밖 기간은 외부 API로 전달되지 않고 기본값으로 대체된다."""
    used: list[str] = []
    monkeypatch.setattr(market_data, "_fetch_history_sync",
                        lambda symbol, period: used.append(period) or [1.0])
    asyncio.run(market_data.get_sector_history(period="../../etc/passwd"))
    assert set(used) == {market_data.HISTORY_PERIOD}


def test_get_sector_history_keeps_completed_symbols_on_timeout(monkeypatch):
    """느린 심볼 하나 때문에 완료분을 통째로 버리면 과열도가 통째로 빈다."""
    import time as _time

    def fetch(symbol, period):
        if symbol == BENCH:
            _time.sleep(5)
        return [1.0, 2.0]

    monkeypatch.setattr(market_data, "_HISTORY_TOTAL_TIMEOUT", 0.6)
    monkeypatch.setattr(market_data, "_HISTORY_MAX_WORKERS", 12)
    monkeypatch.setattr(market_data, "_fetch_history_sync", fetch)

    history = asyncio.run(market_data.get_sector_history())
    assert history, "타임아웃이 걸려도 완료된 시계열은 남아야 한다"
    assert BENCH not in history


# ===== 번들 통합 =====
def test_to_bundle_carries_sector_heat():
    heat = market_data.build_sector_heat({"XLK": flat_then(0.20), BENCH: constant()})
    bundle = market_data.to_bundle({}, {}, None, {}, sector_heat=heat)
    assert bundle.sectorHeat["Technology"].label == "hot"


def test_to_bundle_copies_the_heat_mapping():
    """[SECURE] 내부 상태가 호출자 dict와 공유되면 나중 변경이 스냅샷을 오염시킨다 (Category 6)."""
    heat = market_data.build_sector_heat({"XLK": flat_then(0.20)})
    bundle = market_data.to_bundle({}, {}, None, {}, sector_heat=heat)
    heat.clear()
    assert "Technology" in bundle.sectorHeat


def test_to_bundle_defaults_to_empty_sector_fields():
    bundle = market_data.to_bundle({}, {}, None, {})
    assert bundle.sectorHeat == {}
    assert bundle.sectorValuation == {}


def test_build_market_bundle_populates_sector_heat(monkeypatch):
    """refresh 경로가 실제로 sectorHeat를 채우는지 - 네트워크 호출은 전부 대체한다."""
    async def fake_history(period=market_data.HISTORY_PERIOD):
        return {"XLK": flat_then(0.20), BENCH: constant()}

    async def empty_dict():
        return {}

    async def none_value():
        return None

    monkeypatch.setattr(market_data, "get_sector_history", fake_history)
    monkeypatch.setattr(market_data, "get_snapshot", empty_dict)
    monkeypatch.setattr(market_data, "get_sector_snapshot", empty_dict)
    monkeypatch.setattr(market_data, "get_cnn_fear_greed", none_value)

    from mbrief.sources import calendar_service
    monkeypatch.setattr(calendar_service, "get_calendar", empty_dict)

    bundle = asyncio.run(market_data.build_market_bundle())
    assert bundle.sectorHeat["Technology"].label == "hot"
    assert bundle.sectorValuation == {}   # 밸류에이션은 파일 상태를 아는 pipeline.refresh가 채운다


def test_build_market_bundle_survives_a_history_failure(monkeypatch):
    async def boom(period=market_data.HISTORY_PERIOD):
        raise RuntimeError("시계열 수집 실패")

    async def empty_dict():
        return {}

    async def none_value():
        return None

    monkeypatch.setattr(market_data, "get_sector_history", boom)
    monkeypatch.setattr(market_data, "get_snapshot", empty_dict)
    monkeypatch.setattr(market_data, "get_sector_snapshot", empty_dict)
    monkeypatch.setattr(market_data, "get_cnn_fear_greed", none_value)

    from mbrief.sources import calendar_service
    monkeypatch.setattr(calendar_service, "get_calendar", empty_dict)

    bundle = asyncio.run(market_data.build_market_bundle())
    assert bundle.sectorHeat == {}        # 과열도만 비고 번들은 정상 반환된다
    assert bundle.updatedAt


# --------------------------------------------------
# Security Checklist
# Applied:
#   - Improper Input Validation: 기간 문자열 화이트리스트(HISTORY_PERIODS) 우회 시도가
#     외부 API 인자로 전달되지 않고 기본값으로 대체되는지 회귀로 고정
#   - Missing Error Handling: 개별 심볼 실패·타임아웃·전체 수집 실패에서 부분 결과가 보존되는지 검증
#   - Improper Input for Security Decision: 라벨이 gapPercentile 외의 값(RSI·상대강도)에
#     영향받지 않는지, 표본 부족이 sampleDays=0으로 드러나는지 고정
#   - Private Array Returned: to_bundle이 과열도 dict를 복사해 보관하는지 검증 (Category 6)
#   - 네트워크 미접촉: 모든 수집 함수를 monkeypatch로 대체해 실제 외부 요청이 발생하지 않는다
# Not Applied:
#   - [WARN] 해당 없음 - 합성 데이터 기반 테스트로 자격증명·외부 입출력이 없다
# --------------------------------------------------
