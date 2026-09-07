"""방향성 스코어카드 테스트 — 합성 데이터, 네트워크 없음."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from mbrief import scorecard, store
from mbrief.schemas import (
    BriefingRecord,
    Direction,
    MarketBundle,
    Quote,
    Scorecard,
    StructuredBriefing,
)
from mbrief.timeutil import today_date

# 2026-09 달력 (요일 고정):
#   09-01 화, 09-02 수, 09-03 목, 09-04 금, 09-05 토, 09-06 일, 09-07 월
# 채점은 "평일 브리핑 → 그 다음 세션" 이므로 기준일도 반드시 실제 요일을 맞춰야 한다.
THURSDAY = "2026-09-03"
FRIDAY = "2026-09-04"
SATURDAY = "2026-09-05"
SUNDAY = "2026-09-06"
MONDAY = "2026-09-07"

TODAY = date(2026, 9, 4)   # 금요일 — 목요일 브리핑을 채점하는 기준일


def _record(
    record_id: str = "2026-09-03-ko-aaaaaa",
    day: str = THURSDAY,
    lang: str = "ko",
    up: int = 50,
    flat: int = 30,
    down: int = 20,
    structured: bool = True,
) -> BriefingRecord:
    body = None
    if structured:
        body = StructuredBriefing(
            headline="테스트 헤드라인",
            overnight="본문",
            direction=Direction(up=up, flat=flat, down=down),
        )
    return BriefingRecord(
        id=record_id,
        date=day,
        lang=lang,
        generatedAt=f"{day}T07:00:00+09:00",
        headline="테스트 헤드라인",
        structured=body,
    )


def _market(
    change_pct: float | None,
    price: float | None = 7700.0,
    change: float | None = 10.0,
    updated_on: str = FRIDAY,
) -> MarketBundle:
    """updated_on = 스냅샷 기준일. 채점 대상 브리핑의 '다음 세션'을 담고 있어야 한다."""
    quotes = {}
    if price is not None:
        quotes["S&P 500"] = Quote(price=price, change=change, changePercent=change_pct)
    return MarketBundle(updatedAt=f"{updated_on}T07:00:00+09:00", quotes=quotes)


# ===== classify / brier =====
@pytest.mark.parametrize("pct,expected", [
    (1.2, "up"), (0.3, "up"), (0.29, "flat"), (0.0, "flat"),
    (-0.29, "flat"), (-0.3, "down"), (-2.0, "down"),
])
def test_classify_thresholds(pct, expected):
    assert scorecard.classify(pct) == expected


def test_brier_perfect_prediction_is_zero():
    assert scorecard.brier_score(Direction(up=100, flat=0, down=0), "up") == 0.0


def test_brier_worst_prediction_is_two():
    assert scorecard.brier_score(Direction(up=100, flat=0, down=0), "down") == 2.0


def test_brier_near_uniform_prediction():
    # (0.34-1)^2 + 0.33^2 + 0.33^2 = 0.4356 + 0.1089 + 0.1089 = 0.6534
    value = scorecard.brier_score(Direction(up=34, flat=33, down=33), "up")
    assert value == pytest.approx(0.6534)


# ===== compute_scorecard =====
def test_scores_yesterday_record_as_hit():
    sc = scorecard.compute_scorecard([_record(up=60, flat=25, down=15)], _market(1.1), TODAY)
    assert len(sc.entries) == 1
    entry = sc.entries[0]
    assert entry.briefingId == "2026-09-03-ko-aaaaaa"
    assert entry.predicted == "up"
    assert entry.actual == "up"
    assert entry.hit is True
    assert entry.spxChangePercent == 1.1
    assert sc.hitRate == 1.0
    assert sc.sampleSize == 1


def test_scores_miss():
    sc = scorecard.compute_scorecard([_record(up=60, flat=25, down=15)], _market(-1.5), TODAY)
    entry = sc.entries[0]
    assert entry.actual == "down"
    assert entry.hit is False
    assert sc.hitRate == 0.0


def test_flat_actual_when_change_is_small():
    sc = scorecard.compute_scorecard([_record(up=20, flat=60, down=20)], _market(0.1), TODAY)
    assert sc.entries[0].actual == "flat"
    assert sc.entries[0].hit is True


def test_skips_today_record():
    today_record = _record(record_id="2026-09-04-ko-bbbbbb", day=FRIDAY)
    sc = scorecard.compute_scorecard([today_record], _market(1.0), TODAY)
    assert sc.entries == []


def test_skips_record_older_than_lookback():
    old = _record(record_id="2026-08-28-ko-cccccc", day="2026-08-28")  # 금요일, TODAY-7
    sc = scorecard.compute_scorecard([old], _market(1.0), TODAY)
    assert sc.entries == []


def test_skips_non_primary_language():
    en = _record(record_id="2026-09-03-en-dddddd", lang="en")
    sc = scorecard.compute_scorecard([en], _market(1.0), TODAY)
    assert sc.entries == []


def test_skips_text_fallback_record():
    plain = _record(structured=False)
    sc = scorecard.compute_scorecard([plain], _market(1.0), TODAY)
    assert sc.entries == []


def test_skips_when_quote_missing():
    sc = scorecard.compute_scorecard([_record()], _market(None, price=None), TODAY)
    assert sc.entries == []


def test_skips_when_market_is_none():
    sc = scorecard.compute_scorecard([_record()], None, TODAY)
    assert sc.entries == []


def test_skips_when_no_trading_change_zero():
    """price == previous close (change == 0) 이면 휴장으로 보고 채점하지 않는다."""
    sc = scorecard.compute_scorecard([_record()], _market(0.0, change=0.0), TODAY)
    assert sc.entries == []


def test_does_not_rescore_same_briefing():
    first = scorecard.compute_scorecard([_record()], _market(1.0), TODAY)
    second = scorecard.compute_scorecard([_record()], _market(1.0), TODAY, first)
    assert len(second.entries) == 1


def test_picks_newest_unscored_record():
    older = _record(record_id="2026-09-01-ko-aaaaaa", day="2026-09-01")   # 화
    newer = _record(record_id="2026-09-03-ko-bbbbbb", day=THURSDAY)       # 목
    sc = scorecard.compute_scorecard([older, newer], _market(1.0), TODAY)
    assert len(sc.entries) == 1
    assert sc.entries[0].briefingId == newer.id


def test_does_not_mutate_input_scorecard():
    current = Scorecard(updatedAt="x", sampleWindow=30)
    scorecard.compute_scorecard([_record()], _market(1.0), TODAY, current)
    assert current.entries == []
    assert current.updatedAt == "x"


def test_stats_use_sample_window():
    current = Scorecard(updatedAt="x", sampleWindow=2)
    sc = current
    days = [THURSDAY, "2026-09-02", "2026-09-01"]  # 목, 수, 화 — 모두 평일
    percents = [1.0, -1.0, 1.0]  # hit, miss, hit  (모두 up 예측)
    for index, (day, pct) in enumerate(zip(days, percents)):
        record = _record(record_id=f"{day}-ko-{index:06d}", day=day, up=60, flat=25, down=15)
        sc = scorecard.compute_scorecard([record], _market(pct), TODAY, sc)
    assert len(sc.entries) == 3
    assert sc.sampleSize == 2  # 최근 2건만 통계에 사용
    assert sc.hitRate == pytest.approx(0.5)  # 최신 2건: 2026-09-01(hit) + 2026-09-02(miss)


def test_entries_capped_at_max():
    """스코어카드가 무한히 커지지 않는다 (오래된 항목부터 버린다)."""
    sc = Scorecard(updatedAt="x", sampleWindow=30)
    sc.entries = [
        scorecard.ScoreEntry(
            date=THURSDAY, briefingId=f"2026-09-03-ko-{i:06d}", predicted="up",
            probs=Direction(up=60, flat=25, down=15), actual="up",
            spxChangePercent=1.0, scoredAt="2026-09-04T07:00:00+09:00", hit=True, brier=0.1,
        )
        for i in range(scorecard.MAX_ENTRIES + 20)
    ]
    updated = scorecard.compute_scorecard([_record()], _market(1.0), TODAY, sc)
    assert len(updated.entries) == scorecard.MAX_ENTRIES
    assert updated.entries[0].briefingId == "2026-09-03-ko-aaaaaa"  # 새 항목이 맨 앞


def test_bad_date_is_skipped():
    broken = _record(record_id="2026-09-03-ko-eeeeee")
    broken.date = "not-a-date"
    sc = scorecard.compute_scorecard([broken], _market(1.0), TODAY)
    assert sc.entries == []


def test_skips_when_market_snapshot_date_is_unparsable():
    market = _market(1.0)
    market.updatedAt = "not-a-timestamp"
    sc = scorecard.compute_scorecard([_record()], market, TODAY)
    assert sc.entries == []


# ===== 주말 look-ahead 방지 =====
def test_saturday_record_is_never_scored():
    """토요일 브리핑은 예측 대상 세션이 없다. 월요일 스냅샷으로 채점하면 안 된다."""
    saturday_record = _record(record_id="2026-09-05-ko-a1a1a1", day=SATURDAY)
    sc = scorecard.compute_scorecard(
        [saturday_record], _market(1.0, updated_on=MONDAY), date.fromisoformat(MONDAY)
    )
    assert sc.entries == []


def test_sunday_record_is_never_scored():
    sunday_record = _record(record_id="2026-09-06-ko-b2b2b2", day=SUNDAY)
    sc = scorecard.compute_scorecard(
        [sunday_record], _market(1.0, updated_on=MONDAY), date.fromisoformat(MONDAY)
    )
    assert sc.entries == []


def test_thursday_record_is_scored_on_friday():
    thursday_record = _record(record_id="2026-09-03-ko-c3c3c3", day=THURSDAY)
    sc = scorecard.compute_scorecard(
        [thursday_record], _market(1.0, updated_on=FRIDAY), date.fromisoformat(FRIDAY)
    )
    assert [e.briefingId for e in sc.entries] == ["2026-09-03-ko-c3c3c3"]
    assert sc.entries[0].date == THURSDAY


def test_friday_record_scored_saturday_and_not_rescored_monday():
    """토요일 스냅샷은 금요일 종가다 → 금요일 브리핑 채점에 쓸 수 있고, 월요일에 재채점하지 않는다."""
    friday_record = _record(record_id="2026-09-04-ko-d4d4d4", day=FRIDAY)
    saturday_record = _record(record_id="2026-09-05-ko-e5e5e5", day=SATURDAY)
    records = [friday_record, saturday_record]

    # 토요일 실행: 금요일 브리핑을 금요일 세션으로 채점
    saturday_run = scorecard.compute_scorecard(
        records, _market(1.2, updated_on=SATURDAY), date.fromisoformat(SATURDAY)
    )
    assert [e.briefingId for e in saturday_run.entries] == ["2026-09-04-ko-d4d4d4"]
    assert saturday_run.entries[0].spxChangePercent == 1.2

    # 월요일 실행: 이미 채점된 금요일 건은 다시 채점하지 않고, 토요일 건도 채점하지 않는다
    monday_run = scorecard.compute_scorecard(
        records, _market(-2.0, updated_on=MONDAY), date.fromisoformat(MONDAY), saturday_run
    )
    assert len(monday_run.entries) == 1
    assert monday_run.entries[0].spxChangePercent == 1.2  # 월요일 등락률로 덮어쓰지 않았다


def test_older_unscored_record_is_dropped_not_scored_later():
    """가장 최신 후보만 채점하고, 뒤로 물러나 오래된 브리핑을 엉뚱한 세션으로 채점하지 않는다."""
    thursday_record = _record(record_id="2026-09-03-ko-f6f6f6", day=THURSDAY)
    friday_record = _record(record_id="2026-09-04-ko-a7a7a7", day=FRIDAY)
    records = [thursday_record, friday_record]

    saturday_run = scorecard.compute_scorecard(
        records, _market(1.0, updated_on=SATURDAY), date.fromisoformat(SATURDAY)
    )
    assert [e.briefingId for e in saturday_run.entries] == ["2026-09-04-ko-a7a7a7"]

    # 월요일: 목요일 건이 아직 미채점이지만 월요일 세션으로 채점되면 안 된다 (look-ahead 오류)
    monday_run = scorecard.compute_scorecard(
        records, _market(-1.5, updated_on=MONDAY), date.fromisoformat(MONDAY), saturday_run
    )
    assert [e.briefingId for e in monday_run.entries] == ["2026-09-04-ko-a7a7a7"]


def test_skips_when_snapshot_is_not_after_record_date():
    """스냅샷이 브리핑과 같은 날이면 예측 대상 세션이 아직 확정되지 않았다."""
    sc = scorecard.compute_scorecard(
        [_record()], _market(1.0, updated_on=THURSDAY), TODAY
    )
    assert sc.entries == []


def test_skips_when_snapshot_is_too_far_after_record_date():
    """스냅샷이 브리핑보다 5일 이상 뒤면 다른 세션이므로 채점하지 않는다."""
    old = _record(record_id="2026-09-01-ko-b8b8b8", day="2026-09-01")  # 화, TODAY(금) 기준 lookback 안
    sc = scorecard.compute_scorecard(
        [old], _market(1.0, updated_on=MONDAY), TODAY  # 09-01 + 4일 = 09-05 < 09-07
    )
    assert sc.entries == []


def test_empty_records_returns_empty_stats():
    sc = scorecard.compute_scorecard([], _market(1.0), TODAY)
    assert sc.entries == []
    assert sc.hitRate is None
    assert sc.avgBrier is None
    assert sc.sampleSize == 0


# ===== 저장 래퍼 =====
def test_update_scorecard_persists():
    updated = scorecard.update_scorecard([_record()], _market(1.0), TODAY)
    assert len(updated.entries) == 1
    reloaded = store.load_scorecard()
    assert reloaded is not None
    assert reloaded.entries[0].briefingId == updated.entries[0].briefingId


def test_run_uses_store():
    """run()은 실제 오늘 날짜를 쓰므로 대상 날짜도 오늘 기준으로 만든다 (달력 고정 금지)."""
    today = today_date()
    target = today - timedelta(days=1)
    while target.weekday() >= scorecard.SATURDAY:  # 직전 평일로 이동
        target -= timedelta(days=1)
    day = target.isoformat()

    record = _record(record_id=f"{day}-ko-aaaaaa", day=day)
    store.save_record(record)
    store.save_market(_market(1.0, updated_on=today.isoformat()))
    sc = scorecard.run()
    assert len(sc.entries) == 1
    assert sc.entries[0].briefingId == record.id


# --------------------------------------------------
# Security Checklist
# Applied:
#   - Improper Input for Security Decision: 잘못된 날짜 문자열·시세 스냅샷 시각이 채점에서 제외되는지 검증
#   - Null Pointer Dereference: market/quote/structured가 None인 경우 예외 없이 건너뛰는지 검증
#   - Private Array Returned: 입력 스코어카드가 변형되지 않는지 검증 (deep copy 동작)
#   - Infinite Loop: sampleWindow 상한과 entries 최대 개수(MAX_ENTRIES) 상한이 적용되는지 검증
#   - 데이터 무결성(look-ahead 방지): 주말 브리핑 미채점, 스냅샷 기준일이 브리핑 이후여야 함,
#     이미 채점된 건 재채점 금지, 오래된 미채점 건이 나중 세션으로 채점되지 않음
# Not Applied:
#   - [WARN] 해당 없음 - 순수 함수 + tmp 디렉터리 저장 테스트, 외부 입출력 없음
# --------------------------------------------------
