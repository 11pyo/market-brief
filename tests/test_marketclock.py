"""장 시계 테스트 — 시제 오인(휴장일에 '어젯밤 마감') 회귀를 고정한다."""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from mbrief import marketclock as mc

KST = ZoneInfo("Asia/Seoul")
ET = ZoneInfo("America/New_York")


def _candles_for(days: list[str], hour_et: int = 12) -> list[dict]:
    """주어진 ET 날짜들에 캔들이 하나씩 있는 차트 데이터를 만든다."""
    out = []
    for day in days:
        d = dt.date.fromisoformat(day)
        ts = int(dt.datetime.combine(d, dt.time(hour_et), tzinfo=ET).timestamp())
        out.append({"time": ts, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0})
    return out


def _kst(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text).replace(tzinfo=KST)


# ===== 세션 날짜 추출 =====
def test_session_dates_are_unique_and_sorted():
    candles = _candles_for(["2026-09-03", "2026-09-04"]) + _candles_for(["2026-09-04"], hour_et=14)
    assert mc.session_dates(candles) == [dt.date(2026, 9, 3), dt.date(2026, 9, 4)]


def test_session_dates_ignores_malformed_entries():
    bad = [{"time": None}, {"time": "x"}, {}, {"time": 1_0**20}, "not a dict"]
    assert mc.session_dates(bad) == []


# ===== 실제로 겪은 사례: 노동절 휴장 =====
def test_labor_day_is_detected_as_holiday():
    """2026-09-07은 미국 노동절. 당일 캔들이 없고 평일 오전이 지났으면 휴장으로 판정한다."""
    candles = _candles_for(["2026-09-02", "2026-09-03", "2026-09-04"])
    clock = mc.build_clock(candles, now=_kst("2026-09-07T23:40:00"))   # = 10:40 ET 월요일

    assert clock.state == "holiday"
    assert clock.last_session == dt.date(2026, 9, 4)
    assert clock.quotes_are_live is False

    text = mc.format_for_llm(clock)
    assert "NO US session today" in text
    assert "2026-09-04" in text
    assert "CLOSING values" in text


def test_holiday_briefing_tells_model_not_to_say_last_night():
    candles = _candles_for(["2026-09-04"])
    clock = mc.build_clock(candles, now=_kst("2026-09-07T23:40:00"))
    assert "Do NOT write 'last night the market closed'" in mc.format_for_llm(clock)


# ===== 상태 분류 =====
def test_regular_session_in_progress():
    candles = _candles_for(["2026-09-08"])
    clock = mc.build_clock(candles, now=_kst("2026-09-09T00:30:00"))   # = 11:30 ET 화요일
    assert clock.state == "open"
    assert clock.quotes_are_live is True
    assert "IN PROGRESS" in mc.format_for_llm(clock)


def test_pre_market_before_open():
    candles = _candles_for(["2026-09-08"])
    clock = mc.build_clock(candles, now=_kst("2026-09-09T20:00:00"))   # = 07:00 ET 수요일
    assert clock.state == "pre_market"


def test_after_hours_once_regular_session_closed():
    candles = _candles_for(["2026-09-08"])
    clock = mc.build_clock(candles, now=_kst("2026-09-09T06:00:00"))   # = 17:00 ET 화요일
    assert clock.state == "after_hours"
    assert "already CLOSED" in mc.format_for_llm(clock)


def test_weekend_is_closed():
    candles = _candles_for(["2026-09-04"])
    clock = mc.build_clock(candles, now=_kst("2026-09-06T12:00:00"))   # 일요일
    assert clock.state == "weekend"
    assert clock.quotes_are_live is False


def test_early_morning_et_is_closed_not_holiday():
    """평일 새벽 03시 ET는 아직 프리마켓 전. 당일 캔들이 없다고 휴장으로 단정하지 않는다."""
    candles = _candles_for(["2026-09-08"])
    clock = mc.build_clock(candles, now=_kst("2026-09-09T16:00:00"))   # = 03:00 ET 수요일
    assert clock.state == "closed"


# ===== 데이터가 없을 때 =====
def test_no_candles_still_produces_a_clock():
    clock = mc.build_clock([], now=_kst("2026-09-08T08:00:00"))
    assert clock.last_session is None
    text = mc.format_for_llm(clock)
    assert "Now:" in text and "US market:" in text


def test_format_always_includes_framing_instruction():
    for when in ("2026-09-07T23:40:00", "2026-09-09T00:30:00", "2026-09-06T12:00:00"):
        clock = mc.build_clock(_candles_for(["2026-09-04"]), now=_kst(when))
        assert "FRAMING:" in mc.format_for_llm(clock)


# ===== 장중 갱신 창 =====
def test_parse_window_accepts_valid_and_rejects_junk():
    assert mc.parse_window("09:00-15:30") == (dt.time(9, 0), dt.time(15, 30))
    for bad in ("", "09:00", "9-15", "25:00-15:30", "09:60-15:30", "a:b-c:d", "09:00-15:30-16:00"):
        assert mc.parse_window(bad) is None, bad


def test_intraday_window_covers_krx_session_on_weekdays():
    assert mc.in_intraday_window(_kst("2026-09-08T09:00:00")) is True    # 화요일 개장
    assert mc.in_intraday_window(_kst("2026-09-08T12:55:00")) is True
    assert mc.in_intraday_window(_kst("2026-09-08T15:30:00")) is True    # 마감 시각 포함
    assert mc.in_intraday_window(_kst("2026-09-08T08:59:00")) is False
    assert mc.in_intraday_window(_kst("2026-09-08T15:31:00")) is False


def test_intraday_window_is_false_on_weekend():
    assert mc.in_intraday_window(_kst("2026-09-05T12:00:00")) is False   # 토요일
    assert mc.in_intraday_window(_kst("2026-09-06T12:00:00")) is False   # 일요일


def test_intraday_window_accepts_custom_range():
    win = (dt.time(22, 30), dt.time(23, 59))
    assert mc.in_intraday_window(_kst("2026-09-08T23:00:00"), win) is True
    assert mc.in_intraday_window(_kst("2026-09-08T12:00:00"), win) is False
